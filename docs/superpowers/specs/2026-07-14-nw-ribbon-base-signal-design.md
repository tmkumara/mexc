# Pluggable Base Signal: `nw_ribbon` / `ema_confluence` — Design Spec

## Context

`architecture.txt` (the original spec for the Liquidation-Aware Scalp strategy, v14) called for a
pluggable base-signal trigger — `BASE_SIGNAL=nw_ribbon` (Nadaraya-Watson kernel slope-turn, gated by
an EMA 20/50/100/200 ribbon) or `BASE_SIGNAL=ema_confluence` (EMA 9/21/50 + VWAP + RSI + volume) —
sharing the same downstream liquidation-cluster filter, so the two could be A/B'd via `.env` without
a redeploy. What actually got built and merged to `main` only implements `ema_confluence`, hardcoded,
on 1m bars (a deliberate deviation from the original 5m spec — 1m is what's live today and already
verified: 5 signals observed firing end-to-end in a local run against live MEXC data, 2026-07-14).

The `nw_ribbon` reference implementation exists only in `~/liqbot-poc/nw_kernel.py` (a standalone
sanity-tested prototype, not wired into this repo) and has never run inside this bot's arm/monitor
lifecycle. This spec ports it in as a second selectable trigger, alongside the existing one.

## Goals

- Add `nw_kernel.py` (ported from `~liqbot-poc/nw_kernel.py`) as a new pure-math module.
- Make the base-signal trigger selectable via `BASE_SIGNAL` in `config.py`/`.env`, `"ema_confluence"`
  (default) or `"nw_ribbon"`, dispatched through a dict — not scattered `if/else`.
- Keep `nw_ribbon` on the same 1m `SCALP_TF` and the same arm/monitor/liquidity-filter/OI-poll
  infrastructure as `ema_confluence` — a true apples-to-apples swap of only the trigger function.
- Surface which base signal produced a fired trade in the Telegram alert.
- Unit-test the new kernel module the same way `liq_estimator.py` and the existing base signal are
  tested.

## Non-Goals

- No 5m timeframe, no second kline-fetch cadence, no separate `armed_setups` aging unit — everything
  stays in 1m bars as already live.
- No new OI/ticker plumbing — `LiqEstimator`, `update_ticker_cache`, and `main.py`'s OI poll loop are
  already symbol-keyed and shared; `nw_ribbon` reuses them unchanged.
- `BASE_SIGNAL` default stays `"ema_confluence"` — merging this to `main` (which auto-deploys) must
  not change the server's live trigger behavior. Switching to `nw_ribbon` is a manual `.env` change
  the user makes later, once ready to A/B it live.
- No DB schema changes, no new `Signal` dataclass field for the trigger name — see "Telegram message"
  below for how that's surfaced without one.
- No change to the liquidity filter (`_evaluate_liquidity`), `main.py`'s firing-budget/gap/cooldown
  logic, or `bot.py`'s message layout.

## `nw_kernel.py` (new module, project root — alongside `liq_estimator.py`)

Direct, self-contained port of `~liqbot-poc/nw_kernel.py`: its own `ema()` helper, no imports from
`strategy.py` or `config.py` — matches the existing pattern of `liq_estimator.py` being independently
testable with no cross-module coupling.

```python
def rq_weights(n: int, h: float, r: float) -> np.ndarray: ...
def nw_estimate(closes: np.ndarray, h: float = 8.0, r: float = 8.0) -> float: ...
def nw_series(closes: np.ndarray, h: float = 8.0, r: float = 8.0, tail: int = 6) -> np.ndarray: ...
def nw_signal(closes: np.ndarray, h: float = 8.0, r: float = 8.0, lag: int = 2,
              smooth: bool = False) -> str | None: ...          # "bullish_change" | "bearish_change" | None
def ema(arr, n) -> np.ndarray: ...
def ema_ribbon_bias(closes: np.ndarray, fast: int = 20, mid: int = 50,
                     slow: int = 100, trend: int = 200) -> str: ...   # "long" | "short" | "neutral"
def base_signal_nw(closes: np.ndarray, h: float = 8.0, r: float = 8.0, lag: int = 2,
                    smooth: bool = False, fast: int = 20, mid: int = 50,
                    slow: int = 100, trend: int = 200) -> str | None: ...   # "long" | "short" | None
```

One change from the poc version: `ema_ribbon_bias`/`base_signal_nw` take the ribbon periods as
parameters instead of hardcoding `20/50/100/200`, so `config.py` controls them. Return values stay
lowercase (`"long"/"short"`) exactly as the poc — `strategy.py`'s wrapper uppercases to match this
repo's `"LONG"/"SHORT"` convention.

## `config.py` additions

```python
# ── Base signal selection ────────────────────────────────────────────
BASE_SIGNAL: str = os.getenv("BASE_SIGNAL", "ema_confluence")   # "ema_confluence" | "nw_ribbon"

# ── Nadaraya-Watson kernel + EMA ribbon (only used when BASE_SIGNAL=nw_ribbon) ──
NW_H: float            = float(os.getenv("NW_H", "8.0"))
NW_R: float            = float(os.getenv("NW_R", "8.0"))
NW_LAG: int            = int(os.getenv("NW_LAG", "2"))
NW_SMOOTH: bool        = os.getenv("NW_SMOOTH", "false").lower() == "true"
EMA_RIBBON_FAST: int   = int(os.getenv("EMA_RIBBON_FAST", "20"))
EMA_RIBBON_MID: int    = int(os.getenv("EMA_RIBBON_MID", "50"))
EMA_RIBBON_SLOW: int   = int(os.getenv("EMA_RIBBON_SLOW", "100"))
EMA_RIBBON_TREND: int  = int(os.getenv("EMA_RIBBON_TREND", "200"))
NW_KLINE_COUNT: int    = int(os.getenv("NW_KLINE_COUNT", "260"))
```

Add this block to `.env.example` (commented as "only used when `BASE_SIGNAL=nw_ribbon`"), matching
how the existing v14 tuning block is documented there. `.env` itself is not touched — the default
(`ema_confluence`) requires no new env vars to keep working exactly as today.

An invalid `BASE_SIGNAL` value fails fast: the dispatch dict lookup in `strategy.py` raises `KeyError`
at import time rather than silently falling back to a default — a typo'd `.env` value surfaces
immediately (bot won't start) instead of quietly running an unintended strategy.

### Why `NW_KLINE_COUNT` is separate from `SCALP_KLINE_COUNT`

The EMA ribbon needs ~200-bar warmup; `ema_confluence` only needs `EMA_SLOW + 6 = 56`. The live
strategy fetches `SCALP_KLINE_COUNT=100` bars, and `_rolling_vwap` is a **cumulative** VWAP anchored
to the start of that fetched window — so raising `SCALP_KLINE_COUNT` to satisfy the ribbon would
silently shift `ema_confluence`'s VWAP anchor and change its live behavior. `NW_KLINE_COUNT` (default
260: 200-period trend EMA + safety buffer for `nw_series`'s historical tail) is used only when
`BASE_SIGNAL=nw_ribbon`, keeping the two signals fully isolated with no shared side effects.

## `strategy.py` changes

- Rename `_base_signal` → `_base_signal_ema_confluence` (body unchanged).
- Add `_base_signal_nw_ribbon(df: pd.DataFrame) -> str | None`: extracts `close` as a numpy array,
  calls `nw_kernel.base_signal_nw(closes, h=NW_H, r=NW_R, lag=NW_LAG, smooth=NW_SMOOTH,
  fast=EMA_RIBBON_FAST, mid=EMA_RIBBON_MID, slow=EMA_RIBBON_SLOW, trend=EMA_RIBBON_TREND)`, and
  uppercases the result (or returns `None` unchanged).
- Module-level dispatch, resolved once at import (config is static per process anyway — no benefit to
  a per-call lookup):

  ```python
  _BASE_SIGNAL_FNS: dict[str, Callable[[pd.DataFrame], str | None]] = {
      "ema_confluence": _base_signal_ema_confluence,
      "nw_ribbon": _base_signal_nw_ribbon,
  }
  _base_signal = _BASE_SIGNAL_FNS[BASE_SIGNAL]
  _KLINE_COUNT = SCALP_KLINE_COUNT if BASE_SIGNAL == "ema_confluence" else NW_KLINE_COUNT
  _MIN_BARS = (EMA_SLOW + 6) if BASE_SIGNAL == "ema_confluence" else (EMA_RIBBON_TREND + 6)
  ```

  The `+6` buffer matches the existing `ema_confluence` convention (`EMA_SLOW + 6`) exactly — applied
  to whichever period is the slowest component of the active signal. `NW_KLINE_COUNT`'s default of
  260 comfortably exceeds this 206-bar minimum with headroom to spare.

- `_try_arm_setup` and `_monitor_setup` swap their hardcoded `SCALP_KLINE_COUNT` / `EMA_SLOW + 6`
  references for `_KLINE_COUNT` / `_MIN_BARS`. The call sites for `_base_signal(window)` itself do
  not change — they already call through the module-level name, now bound to whichever function.

## Telegram message: surfacing the trigger without new plumbing

`bot.py`'s `format_signal()` already renders `signal.timeframe_summary` verbatim as the "🧭 Signal:"
line. `strategy.py` already builds that string (and the armed-setup `trend_summary` / `setup_reason`
fields) as e.g. `f"1m base signal + liq filter passed | funding {funding*100:.4f}%"` and
`f"1m liq-scalp | {reason}"`. Prefixing `BASE_SIGNAL` into those existing strings —
`f"1m {BASE_SIGNAL} | {reason}"` — surfaces the trigger in the alert with **zero changes to
`bot.py`, no new `Signal` field, and no DB migration**.

## Data flow

Unchanged for `ema_confluence`. For `nw_ribbon`: same scheduler cadence, same per-symbol
`LiqEstimator`/liquidity-filter/OI-poll infra, same `armed_setups` persistence and `Signal` dataclass
shape, same `main.py` firing-budget/gap/cooldown/concurrency logic — only the trigger function and
kline-fetch size differ.

## Error handling

No new failure paths. `_base_signal_nw_ribbon` returns `None` on insufficient bars (mirrors
`nw_kernel.nw_signal`'s existing `len(closes) < 60` guard) or no slope-turn/ribbon-agreement, which
`_try_arm_setup`/`_monitor_setup` already treat as "no signal this bar, skip" — identical to how
`_base_signal_ema_confluence` returning `None` is handled today.

## Testing

- **New `tests/test_nw_kernel.py`**:
  - `rq_weights`: correct shape, all weights positive, monotonically decreasing with `i`.
  - `nw_estimate` non-repainting: the estimate at bar *k* (computed via `nw_estimate(closes[:k+1])`)
    is identical whether or not later bars exist in the full series — verified by comparing against
    the corresponding entry of `nw_series` computed on the full array.
  - `nw_signal`: slope-turn detection on a synthetic pullback-resume series (down-up-down shape)
    fires `"bullish_change"`/`"bearish_change"` at the correct bar, `None` elsewhere.
  - `ema_ribbon_bias`: constructed stacked-EMA price series returns `"long"`/`"short"`/`"neutral"`
    correctly for each case.
  - `base_signal_nw`: only fires when the slope-turn direction agrees with the ribbon bias (trend
    continuation), `None` when they disagree.
- **Update `tests/test_strategy_liq_scalp.py`**: fix `from strategy import _base_signal` →
  `_base_signal_ema_confluence`. Purely mechanical — behavior of those tests is unchanged.
- No changes to `tests/test_liq_estimator.py` or `tests/test_mexc_client.py`.

## Verification plan

Same approach as the `ema_confluence` local verification done earlier: run `python main.py` locally
(server bot stopped first) with `BASE_SIGNAL=nw_ribbon` and the same temporary gap/cooldown overrides,
watch logs for `[SCALP-ARM]` / `[SCALP-SIGNAL]` / `[SCAN] Fired` lines to confirm the pipeline fires
correctly end-to-end, then run `pytest` for the new/updated unit tests.

## Addendum: monitor-phase re-confirmation must be signal-specific

Discovered during the live verification run (2026-07-14, first attempt): `nw_ribbon` setups armed
with a **fully-cleared liquidity filter** (real RR 1.50 levels) were invalidated on the very next
monitor cycle, every time.

Root cause: `main.py` runs Phase 2 (monitor, over all coins) before Phase 1 (arm) each scan cycle
(`main.py:135-146` then `main.py:239-255`) — so a setup armed in cycle *N* is first checked by
`_monitor_setup` in cycle *N+1*, never the same cycle. `_monitor_setup`'s re-confirmation
(`if _base_signal(window) != direction: invalidate`) works for `ema_confluence` because its
condition (EMA stack + VWAP + RSI + volume) is a snapshot that can hold true across several
consecutive bars. `nw_kernel.nw_signal`, by contrast, is a **one-bar pulse**: it returns
`"bullish_change"`/`"bearish_change"` only on the exact bar the smoothed kernel's slope flips, then
`None` on every following bar (there is no new "turn" to detect a second time). Re-demanding that
exact pulse on the very next cycle's re-confirmation check means every `nw_ribbon` setup — even one
whose liquidity filter cleared instantly — is invalidated one cycle after arming, before it can ever
reach a fire. This is structural, not rare bad luck; it was observed on every armed `nw_ribbon`
setup in the verification run (`AVAX_USDT`, `BSB_USDT` provisionally, `DRAM_USDT`/`NVIDIA_USDT` with
real cleared levels — all four invalidated the cycle immediately after arming).

**Resolution (user-approved):** make the monitor-phase re-confirmation check itself
`BASE_SIGNAL`-dispatched, matching the existing `_BASE_SIGNAL_FNS` pattern:

- `ema_confluence`: re-confirmation is unchanged — still requires `_base_signal_ema_confluence(df)
  == direction` (the original snapshot re-check).
- `nw_ribbon`: re-confirmation instead checks only that `nw_kernel.ema_ribbon_bias(...)` still
  agrees with the armed direction — the initial **arm** still requires the actual slope-turn pulse
  (`_try_arm_setup` is unchanged), but **monitor** no longer demands a fresh pulse every cycle, only
  that the broader trend hasn't reversed.

New dispatch, alongside the existing `_BASE_SIGNAL_FNS`:

```python
def _still_active_ema_confluence(df: pd.DataFrame, direction: str) -> bool:
    return _base_signal_ema_confluence(df) == direction


def _still_active_nw_ribbon(df: pd.DataFrame, direction: str) -> bool:
    closes = df["close"].astype(float).to_numpy()
    bias = nw_kernel.ema_ribbon_bias(
        closes, fast=EMA_RIBBON_FAST, mid=EMA_RIBBON_MID, slow=EMA_RIBBON_SLOW, trend=EMA_RIBBON_TREND,
    )
    return bias == direction.lower()


_STILL_ACTIVE_FNS: dict[str, Callable[[pd.DataFrame, str], bool]] = {
    "ema_confluence": _still_active_ema_confluence,
    "nw_ribbon": _still_active_nw_ribbon,
}
_still_active = _STILL_ACTIVE_FNS[BASE_SIGNAL]
```

`_monitor_setup`'s invalidation check changes from `if _base_signal(window) != direction:` to
`if not _still_active(window, direction):` — the only line that changes in `_monitor_setup`.

This is implemented as Task 5 in the plan (added after the fact — Tasks 1-4 were already complete
and reviewed when this was discovered). The verification run (Task 4) is re-executed after Task 5
lands, to confirm `nw_ribbon` can now actually survive to a `[SCAN] Fired` line.
