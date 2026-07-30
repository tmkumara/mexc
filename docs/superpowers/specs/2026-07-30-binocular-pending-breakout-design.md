# Binocular Pending-Breakout v1 — Design Spec

Date: 2026-07-30
Sources: `D:\Downloads\2- Binocular Trend ( PAID ).txt` (same Pine Script as the
prior two migrations) and `D:\Downloads\Finexa-KT\architecture.txt` (a
from-scratch project brief this spec adapts into the existing bot instead of
following literally — see "Deviations from architecture.txt" below).

**Relationship to prior Binocular work.** A Binocular-derived strategy
(Chandelier-direction + PVT-vs-signal + dual-RSI(25/55), gated by a 15m
Supply/Demand zone and a BTC filter) was already built and deployed on
2026-07-27, then fully ripped out on 2026-07-29 — it fired ~0 live signals
and, once loosened, backtested with a fragile 30.8%/16.7% LONG/SHORT
win-rate split over 44 trades. The current live strategy (Ribbon-Flip
Trend-Bar Confirmation v1) deliberately dropped Chandelier/PVT/RSI entirely.

This spec revives the Chandelier/PVT/dual-RSI engine as the primary trigger
again, but the entry/exit mechanics around it are genuinely different from
what was tried before:

- **Then:** single-pass entry using the *previous* candle's high/low as a
  breakout-buffer proxy (a same-bar approximation), fixed single TP/SL.
- **Now:** a real two-phase pending-breakout lifecycle (PENDING → OPEN, per
  the Pine script's actual `en`/`HighVal`/`LowVal`/`buy1`/`sell1` state
  machine) plus 3 laddered targets with partial exits and a breakeven step
  after T1 — closer to how the indicator is actually meant to be traded.

Per explicit user instruction: **fully replace** the live strategy (not a
side-by-side toggle), but it ships **off** (`STRATEGY_BINOCULAR_ENABLED=false`,
old `STRATEGY_V1_ENABLED=true` stays on) until backtested — including a
direct comparison against the 2026-07-27 attempt's 44-trade baseline on the
same symbols/period — and reviewed before flipping the defaults and
deploying.

## Objective

Replace `strategy.py`'s active pipeline with:

1. **Trigger** (unchanged core math from the 2026-07-27 attempt): Chandelier
   ATR(10)×2.2 direction flip + PVT-vs-21-period-signal-line +
   RSI(25)-vs-RSI(55), transition-detected (`BUY and not BUY[-1]`).
2. **Optional confirmation** (`SIGNAL_MODE=confirmed`, default): the
   existing 6-EMA ribbon (30/35/40/45/50 vs 60) must be aligned with the
   trigger's direction, and close must be on the correct side of EMA200.
   `SIGNAL_MODE=original` skips this and fires on the raw trigger alone.
   `SIGNAL_MODE=strict` additionally requires daily-VWAP side plus at least
   `MTF_MIN_CONFIRMATIONS` of `CONFIRMATION_TIMEFRAMES` agreeing — fully
   implemented (see "VWAP filter + multi-timeframe confirmation" below),
   just not the default, to keep scan cost flat across the ~80-coin pool
   unless explicitly opted into.
3. **Pending-breakout entry**: a transition creates a PENDING setup with a
   precomputed entry level (`high × (1 + buffer)` for BUY / `low × (1 -
   buffer)` for SELL), structural SL (`min(prev_low, low)` / `max(prev_high,
   high)`, no ATR buffer — capped at `MAX_SL_PRICE_PCT`, reject if wider),
   and 3 stacked targets (`diff = (high - prev_low) × 2`; `T1 = high + diff`,
   `T2 = T1 + diff`, `T3 = T2 + diff`, mirrored for SELL). The setup expires
   after `PENDING_SIGNAL_EXPIRY_CANDLES` (default 5) candles if price never
   breaks the entry level, and is cancelled if an opposite-direction
   transition appears first.
4. **3-target partial exits**: 50% closed at T1 (SL then moves to
   breakeven), 30% at T2, 20% at T3. A trade that reaches T1 then stops at
   breakeven is a small **win**, not a loss.

## Deviations from architecture.txt

**Revision (2026-07-30, later same day):** the first version of this spec
trimmed `SIGNAL_MODE=strict` (VWAP + MTF) and real position sizing out of
scope to limit scan cost and blast radius. Per explicit follow-up
instruction — stay in the current project, but make the *strategy logic*
track `architecture.txt` fully, not a reduced version — both are back in
scope. See "VWAP filter + multi-timeframe confirmation" and "Position
sizing (informational)" below. `SIGNAL_MODE` still *defaults* to
`confirmed` (not `strict`) so the live coin-pool scan cost doesn't change
unless the operator opts in — the mode itself is now fully implemented,
just not the default.

`architecture.txt` specifies a from-scratch `binocular_signal_bot/` project
(ccxt, SQLAlchemy, pydantic-settings, its own CLI, pytest suite, console
notifications). Per the "upgrade in place" decision, that project
scaffolding is still not built — the strategy rules move into the existing
bot instead of the bot moving to a new project:

- Data comes from the existing `market_data.py` → `mexc_client.py` MEXC
  REST/WS stack, not ccxt.
- Persistence extends the existing `signals`/`armed_setups` SQLite tables
  in `database.py`, not a new SQLAlchemy layer.
- Notifications stay Telegram (`bot.py`), not console-only.
- Testing stays this repo's existing `pytest` suite under `tests/`, not a
  separate project's test tree.
- The doc's phased dev sequence (scaffold → models → indicators → ... →
  docs) isn't followed as a literal sequence; the "Migration order" section
  below is this project's equivalent, adapted to an existing codebase.

## Preparation

1. Backup branch `backup/ribbon-trendbar-confirmation-v1` cut from current
   `main` HEAD (what's live on the server right now), pushed to `origin`.
2. Implementation on a new branch/worktree off `main`. `main` auto-deploys
   on push (`CLAUDE.md`) — nothing merges to `main` until backtested.

## Components retained as-is

`coin_scanner.py`, `mexc_client.py`, `market_data.py`, `candle_cache.py`,
`mexc_ws_client.py`, `ws_manager.py`, `reports.py`, `clear_db.py`,
`scalper_v3_strategy.py` / `STRATEGY_NAME_V3` track and `backtest/engine.py`
(fully independent — untouched, confirmed via cross-reference that neither
imports anything being removed below). `calculate_ema`, `calculate_rsi`,
`calculate_atr`, `calculate_supertrend` all stay in `strategy.py`:
`calculate_atr` is **still needed** (now feeds the Chandelier trailing stop
instead of the old SL buffer), `calculate_supertrend` is imported by
`backtest/engine.py` (v3's backtester), `calculate_rsi` now also serves the
dual-RSI(25/55) trigger, and all four have generic coverage in
`tests/test_indicators.py` which is otherwise unaffected.

`calculate_ema_ribbon` (already in `strategy.py`) is retained and reused —
its role changes from "primary trigger" to "confirmed-mode filter."

## Components removed from the active runtime

From `strategy.py`: `calculate_trend_bar`, `_detect_ribbon_flip`, and the
current `_calculate_tp_sl`/`_score_candidate`/`evaluate_symbol` bodies
(rewritten below).

From `config.py`: `RIBBON_LOOKBACK_BARS`, `TREND_BAR_PAC_LENGTH`,
`SL_ATR_BUFFER_MULTIPLIER`, `SL_FLOOR_ATR_MULT`, `TARGET_ROI_PCT`,
`TP_PRICE_PCT` — the new engine's SL is a pure capped swing-stop (no ATR
buffer/floor) and its targets are structural (T1/T2/T3), not a fixed
ROI-%-distance TP, so these no longer have meaning for the primary
strategy. **Keep** `RIBBON_MA1_LEN..RIBBON_MA5_LEN`, `RIBBON_BASELINE_LEN`
(repurposed, not removed), `MAX_SL_ROI_PCT`, `MAX_SL_PRICE_PCT`, `MIN_RR`,
`LEVERAGE`, and every scan/coin-pool/cooldown/expiry config constant
(unchanged).

## New indicators (`strategy.py`)

```python
calculate_pvt(df) -> pd.Series
# pvt[i] = pvt[i-1] + volume[i] * (close[i] - close[i-1]) / close[i-1]; pvt[0] = 0

calculate_pvt_signal(pvt, length, ma_type) -> pd.Series
# SMA or EMA of pvt, per PVT_SIGNAL_TYPE/PVT_SIGNAL_LENGTH

calculate_chandelier_direction(df, atr_period, multiplier) -> tuple[pd.Series, pd.Series, pd.Series]
# Returns (direction, long_stop_prev, short_stop_prev) -- ports the Pine
# calculation() function's longStop/shortStop/dir recursion exactly,
# including using the *previous* bar's stop levels for the BUY/SELL
# comparison (matches longStopPrev/shortStopPrev in the source, not the
# current bar's just-updated stop). Implemented as a forward numpy loop,
# same shape as the existing calculate_supertrend loop.

calculate_ema200(df) -> pd.Series
# calculate_ema(close, BINOCULAR_EMA200_LEN), default length 200
```

Raw trigger (vectorized over the whole closed series, no persisted state —
same "recompute fresh every scan" approach `_detect_ribbon_flip` already
used):

```python
BUY  = (direction == 1)  & (pvt > pvt_signal) & (rsi_fast > rsi_slow)
SELL = (direction == -1) & (pvt < pvt_signal) & (rsi_fast < rsi_slow)
new_buy  = BUY.iloc[-1]  and not BUY.iloc[-2]
new_sell = SELL.iloc[-1] and not SELL.iloc[-2]
```

`SIGNAL_MODE=confirmed` (default) additionally requires, on the same bar:
```python
# LONG
ma1>baseline and ma2>baseline and ma3>baseline and ma4>baseline and ma5>baseline and close > ema200
# SHORT: mirrored, all < baseline, close < ema200
```

## VWAP filter + multi-timeframe confirmation (`SIGNAL_MODE=strict`)

Ported from `architecture.txt` sections 8-9, fully implemented (not
trimmed) but not the default mode.

**Daily VWAP** (`calculate_daily_vwap(df) -> pd.Series`): typical price
`(high+low+close)/3`, cumulative `Σ(typical×volume) / Σ(volume)`, reset at
each UTC day boundary (`df.index.date` change) — same session-reset shape
as the Pine script's daily VWAP, computed on `ENTRY_TF` candles directly
(no separate daily-resolution fetch needed, since cumulative-sum-with-reset
works on any intraday series). `strict` BUY requires `close > daily_vwap`;
SELL requires `close < daily_vwap`.

**Multi-timeframe confirmation**: fetches `CONFIRMATION_TIMEFRAMES`
(default `30m,1h,4h` — one step above `ENTRY_TF=15m`, mirroring the Pine
script's own base-timeframe-relative confirmation set) via the existing
`get_market_klines`, each dropped to closed candles independently. Per the
Pine script's actual MTF table function (`signal()`, lines 448-471 of the
source — notably *not* the same 3-condition BUY/SELL used for the main
trigger, it omits the RSI term):
```python
def mtf_signal(df_tf) -> tuple[bool, bool]:  # (buy, sell)
    direction, _, _ = calculate_chandelier_direction(df_tf, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df_tf); pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    buy  = direction.iloc[-1] == 1  and pvt.iloc[-1] > pvt_signal.iloc[-1]
    sell = direction.iloc[-1] == -1 and pvt.iloc[-1] < pvt_signal.iloc[-1]
    return buy, sell
```
`strict` BUY requires `close > daily_vwap` **and** at least
`MTF_MIN_CONFIRMATIONS` (default 2) of the 3 `CONFIRMATION_TIMEFRAMES`
returning `buy=True` from `mtf_signal`; SELL mirrored. Only completed
higher-timeframe candles are used (same `iloc[:-1]` convention).

`strict` scan cost: 3 extra kline fetches per symbol per scan, only paid
when `SIGNAL_MODE=strict` is explicitly set — `confirmed` (the default)
and `original` never touch this code path.

## Position sizing (informational)

Ported from `architecture.txt` section 11. This bot never places real
orders (Telegram-signal-only, `DRY_RUN` gates even paper broadcasts), so
position size is a **display-only** field alongside the existing
leverage/ROI% numbers — it does not replace or gate anything the existing
risk model (`LEVERAGE`, `MAX_SL_ROI_PCT`, `MIN_RR`) already does.

```python
ACCOUNT_BALANCE: float = float(os.getenv("ACCOUNT_BALANCE", "10000"))
RISK_PERCENT_PER_TRADE: float = float(os.getenv("RISK_PERCENT_PER_TRADE", "1.0"))

def position_size(direction, entry, sl) -> float:
    risk_per_unit = (entry - sl) if direction == "LONG" else (sl - entry)
    risk_amount = ACCOUNT_BALANCE * RISK_PERCENT_PER_TRADE / 100.0
    return round(risk_amount / risk_per_unit, 6) if risk_per_unit > 0 else 0.0
```
Computed once at pending-setup creation (using the structural SL, same
inputs the RR gate already uses) and carried through to the confirmed
signal and the Telegram message — not recomputed at confirmation time.

## Pending-breakout entry lifecycle

Reuses the existing (currently dormant) `armed_setups` table rather than a
new table — its shape (`entry_low/high`, `sl_price`, `tp_price`,
`status: armed/fired/missed/expired/invalidated`, `expires_at`,
`fired_signal_id`) already matches PENDING→OPEN/EXPIRED/CANCELLED. Add
three columns via the existing `ALTER TABLE ADD COLUMN` pattern:

```python
("tp2_price", "REAL"),
("tp3_price", "REAL"),
("position_size", "REAL"),
```
(`tp_price` holds T1.) `save_armed_setup()`'s INSERT and the dict it takes
gain `tp2_price`/`tp3_price`/`position_size`. No other schema change to
this table — it was unused by any live code path, so there's no
back-compat concern.

**Setup creation** (`_build_pending_setup(symbol, df_closed) -> dict | None`):
on a fresh `new_buy`/`new_sell` transition (raw, or ribbon+EMA200-confirmed
per `SIGNAL_MODE`):
```python
# LONG
entry = high * (1 + ENTRY_BUFFER_PCT)          # ENTRY_BUFFER_PCT = 0.0002 (0.02%)
sl    = min(prev_low, low)                      # no ATR buffer -- Pine-faithful
diff  = (high - prev_low) * 2
t1, t2, t3 = high + diff, high + 2*diff, high + 3*diff
# SHORT mirrored: entry = low*(1-buf); sl = max(prev_high, high); diff = (prev_high-low)*2; t1..t3 = low - diff*[1,2,3]
```
If `SIGNAL_MODE=strict`, the VWAP + multi-timeframe checks above must also
pass on this same bar before a setup is built at all (`no_vwap_confirmation`
/ `no_mtf_confirmation` otherwise). `position_size(direction, entry, sl)`
(see "Position sizing" above) is computed here and stored on the setup
dict alongside `entry`/`sl`/`t1`/`t2`/`t3`, carried through to the
confirmed signal unchanged.

Reject (never create the setup) if:
- `sl` distance from `entry` exceeds `MAX_SL_PRICE_PCT` (`stop_too_wide`)
- `rr = abs(t1 - entry) / abs(entry - sl) < MIN_RR` (`rr_below_min`)
- `not valid_trade_geometry(direction, entry, t1, sl)` (`invalid_geometry`)

**Setup confirmation** (checked each scan, same cadence as today's
`SCAN_INTERVAL_MINUTES`, against the latest *closed* candle only):
- BUY: `high > entry` → confirmed. SELL: `low < entry` → confirmed.
- Same-candle SL guard: if the confirming candle's `low <= sl` (BUY) /
  `high >= sl` (SELL) is *also* true, treat as an instant stop rather than
  confirming a trade (SL-first tie-break, same convention as the outcome
  checker).
- Not confirmed and `age_in_candles > PENDING_SIGNAL_EXPIRY_CANDLES` →
  `mark_armed_setup_expired`.
- Not confirmed but a fresh opposite-direction transition now exists →
  `mark_armed_setup_invalidated` (cancel-opposite-pending rule).
- Otherwise stays armed, re-checked next scan.

On confirmation, `entry`/`sl`/`t1`/`t2`/`t3` are used exactly as computed at
creation time (never recalculated against the confirming candle) — matches
the Pine script, which fixes these values on the signal bar.

## Outcome tracking: 3-target partial exits

`signals` table gains (existing `ALTER TABLE ADD COLUMN` pattern):
```python
("tp3_price", "REAL"),
("tp2_hit_at", "TEXT"),
("position_size", "REAL"),
```
(`tp1_price`, `tp1_hit_at`, `breakeven_triggered_at` already exist from the
Super Scalper v3 migration and are reused here for their literal purpose
this time, not just as an informational ping.)

New `outcome_check.check_target_ladder()` (added alongside, not replacing,
`check_tp_sl` — `check_tp_sl` stays because `scalper_v3_strategy.py` still
uses it for v3's flat SL/TP2 model):

```python
def check_target_ladder(
    direction, entry, sl, t1, t2, t3, df, entry_candle_cutoff,
    close_fracs=(0.5, 0.3, 0.2),   # T1/T2/T3, sums to 1.0
) -> dict | None:
    """Walks closed candles after entry_candle_cutoff. State: remaining
    position, current_sl (moves to entry/breakeven after T1), stage (0-3
    targets hit so far). Each candle, SL-first same-candle tie-break
    (existing convention): if current_sl is touched, realize the remaining
    position at current_sl and close. Else check targets in order
    (T1 then T2 then T3, one stage advance per candle, mirroring the
    Pine script's per-candle highest-target state); on a stage advance,
    realize close_fracs[stage] of the position at that target's price and
    move current_sl to entry once stage 1 (T1) is reached (if
    MOVE_SL_TO_BREAKEVEN_AFTER_T1). Reaching T3 fully closes.

    Returns None while still open, else:
    {"status": "win"|"loss", "pnl_roi": float, "t1_hit_at": ts|None,
     "t2_hit_at": ts|None, "closed_at": ts, "final_stage": 0-3}
    status is "loss" only if SL is hit before T1 ever triggers (full
    position, negative ROI); every other close (including a T1-then-
    breakeven stop) is "win" since realized ROI is >= 0.
    """
```
`pnl_roi` is the leverage-scaled weighted sum of each realized fraction's
price-move %, matching the existing `_calculate_pnl_roi` convention
elsewhere. Expiry (`SIGNAL_EXPIRE_HOURS`) is still handled the same way it
is today, in `main.py`, before this function is even called.

## `evaluate_symbol` — replaced by two functions

The old single `evaluate_symbol(symbol) -> Signal | None` doesn't fit a
two-phase (create-then-confirm-later) strategy. It's replaced by:

```python
def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None
    # fetch -> drop forming candle -> settle-age check -> compute
    # trigger/confirmation -> _build_pending_setup -> geometry/RR/SL-cap
    # gates -> returns the setup dict (same shape save_armed_setup expects)
    # or None. Only called for symbols with no currently-armed setup.

def check_setup_confirmation(setup: dict) -> tuple[str, float | None]
    # fetch latest candles -> ("confirmed", fill_price) |
    # ("expired", None) | ("invalidated", None) | ("waiting", None)
```
`direction_slot_available` and `valid_trade_geometry` are unchanged and
reused by both.

## Candidate scoring (0-100, used to rank multiple new pending setups within one scan)

- Chandelier/PVT/RSI alignment strength (40) — magnitude of
  `pvt - pvt_signal` relative to its own recent range, and
  `rsi_fast - rsi_slow` spread, combined
- Confirmed-mode agreement quality (20; scores 20 flat if `SIGNAL_MODE=original`) —
  ribbon separation from baseline vs ATR, same shape as the current
  `_score_candidate`'s alignment term
- RR quality (20) — same shape as today, `MIN_RR` floor / `2×MIN_RR` ceiling
- Entry-buffer clearance (20) — how far the current close already sits
  from the computed entry level (closer = more likely to confirm soon =
  higher score)

## Reject-reason buckets

`no_chandelier_direction`, `no_pvt_momentum`, `no_rsi_regime`,
`no_ribbon_confirmation`, `no_ema200_confirmation`, `no_vwap_confirmation`,
`no_mtf_confirmation` (last two only reachable when `SIGNAL_MODE=strict`),
plus existing `stop_too_wide`, `rr_below_min`, `invalid_geometry`,
`missing_data`, `insufficient_history`, `candle_not_settled`, `error`
(unchanged shape).

## Configuration (`config.py`)

```python
STRATEGY_NAME default -> "Binocular Pending-Breakout v1"
STRATEGY_BINOCULAR_ENABLED: bool = os.getenv("STRATEGY_BINOCULAR_ENABLED", "false") == "true"
# Stays false until backtested and reviewed -- see Rollout below.
# STRATEGY_V1_ENABLED stays "true" (default unchanged) until this flips.

SIGNAL_MODE: str = os.getenv("SIGNAL_MODE", "confirmed")   # "original" | "confirmed" | "strict"
CONFIRMATION_TIMEFRAMES: str = os.getenv("CONFIRMATION_TIMEFRAMES", "30m,1h,4h")   # strict mode only
MTF_MIN_CONFIRMATIONS: int = int(os.getenv("MTF_MIN_CONFIRMATIONS", "2"))          # strict mode only

ACCOUNT_BALANCE: float = float(os.getenv("ACCOUNT_BALANCE", "10000"))              # informational only
RISK_PERCENT_PER_TRADE: float = float(os.getenv("RISK_PERCENT_PER_TRADE", "1.0"))  # informational only

PVT_SIGNAL_TYPE: str = os.getenv("PVT_SIGNAL_TYPE", "SMA")
PVT_SIGNAL_LENGTH: int = int(os.getenv("PVT_SIGNAL_LENGTH", "21"))
RSI_FAST_PERIOD: int = int(os.getenv("RSI_FAST_PERIOD", "25"))
RSI_SLOW_PERIOD: int = int(os.getenv("RSI_SLOW_PERIOD", "55"))
CHANDELIER_ATR_PERIOD: int = int(os.getenv("CHANDELIER_ATR_PERIOD", "10"))
CHANDELIER_MULTIPLIER: float = float(os.getenv("CHANDELIER_MULTIPLIER", "2.2"))
BINOCULAR_EMA200_LEN: int = int(os.getenv("BINOCULAR_EMA200_LEN", "200"))

ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))   # 0.02%
PENDING_SIGNAL_EXPIRY_CANDLES: int = int(os.getenv("PENDING_SIGNAL_EXPIRY_CANDLES", "5"))

TARGET1_CLOSE_FRACTION: float = float(os.getenv("TARGET1_CLOSE_FRACTION", "0.5"))
TARGET2_CLOSE_FRACTION: float = float(os.getenv("TARGET2_CLOSE_FRACTION", "0.3"))
TARGET3_CLOSE_FRACTION: float = float(os.getenv("TARGET3_CLOSE_FRACTION", "0.2"))
MOVE_SL_TO_BREAKEVEN_AFTER_T1: bool = os.getenv("MOVE_SL_TO_BREAKEVEN_AFTER_T1", "true").lower() == "true"
```
`RIBBON_MA1_LEN..RIBBON_MA5_LEN`, `RIBBON_BASELINE_LEN` unchanged (reused).
`MAX_SL_ROI_PCT`/`MAX_SL_PRICE_PCT`/`MIN_RR`/`LEVERAGE` unchanged (reused).

## Fixes required outside `strategy.py`

- **`main.py`**: `scan_and_fire_signals` rewritten with the two-phase loop
  described above (process existing armed setups first: confirm/expire/
  invalidate; then scan the remaining pool for new pending setups). `TARGET_ROI_PCT`
  import (`main.py:63`) and its startup log line (`main.py:527`) removed —
  no equivalent fixed-%-target concept exists for this strategy; log the
  new `SIGNAL_MODE`, `ENTRY_BUFFER_PCT`, `PENDING_SIGNAL_EXPIRY_CANDLES`
  instead. `check_outcomes` rewritten to call `check_target_ladder`
  instead of `check_tp_sl`, handling T1/T2 progress notifications and the
  breakeven marker before final close.
- **`bot.py`**: `format_signal` rewritten to show all 3 targets and which
  fraction closes at each, plus (matching `architecture.txt`'s own
  "Signal output" example almost field-for-field) EMA ribbon state, VWAP
  side, MTF confirmation count, and position size — the VWAP/MTF lines
  only render when `SIGNAL_MODE=strict` (otherwise show `SIGNAL_MODE`
  plainly). New `notify_target_progress` (T1/T2 hit, reusing the
  `notify_v3_progress` reply-to-message pattern) sends "50% closed at T1,
  SL moved to breakeven" style updates; `notify_outcome` extended to label
  partial-ladder results (e.g. "T1+T2 HIT, BE STOP", "FULL TARGET
  (T1+T2+T3)", "STOPPED OUT") instead of only "TARGET HIT"/"STOP HIT".
  `cmd_status`'s config import/display block (`bot.py:208-223,244-246`)
  swaps `RIBBON_LOOKBACK_BARS`/`TREND_BAR_PAC_LENGTH`/`TARGET_ROI_PCT` for
  `SIGNAL_MODE`/`ENTRY_BUFFER_PCT`/`PENDING_SIGNAL_EXPIRY_CANDLES`.
- **`webui.py`**: `get_strategy_config()` (`webui.py:232-267`) drops the
  removed config names, adds the new ones including `signal_mode`,
  `confirmation_timeframes`, `mtf_min_confirmations`, `account_balance`,
  `risk_percent_per_trade`; new pending-setups panel backed by
  `db.get_armed_setups()`; inline dashboard JS fields that read the
  removed names updated in the same change (the 2026-07-29 spec flagged
  this exact class of gap once before — Python and JS must be fixed
  together, not left for a later pass).
- **`database.py`**: add `armed_setups.tp2_price/tp3_price/position_size`
  and `signals.tp3_price/tp2_hit_at/position_size` columns (existing
  `ALTER TABLE ADD COLUMN` pattern); `save_armed_setup`'s INSERT gains the
  three new columns; new `mark_signal_tp2_hit(signal_id, hit_at)`
  mirroring the existing `mark_signal_tp1_hit`; `save_signal` gains
  optional `tp2_price=None, tp3_price=None, position_size=None` kwargs.

## Testing

New `tests/test_binocular_indicators.py`: `test_pvt_accumulates_correctly`,
`test_pvt_signal_sma`, `test_pvt_signal_ema`,
`test_chandelier_direction_bullish`, `test_chandelier_direction_bearish`,
`test_chandelier_uses_previous_bar_stop_for_comparison`,
`test_chandelier_does_not_use_future_data`,
`test_raw_buy_transition_detected`, `test_raw_sell_transition_detected`,
`test_confirmed_mode_requires_ribbon_and_ema200`,
`test_daily_vwap_resets_at_session_boundary`,
`test_strict_mode_requires_vwap_side`,
`test_strict_mode_requires_min_mtf_confirmations`,
`test_mtf_signal_omits_rsi_term` (documents the Pine-faithful
`signal()`-vs-`calculation()` asymmetry), `test_position_size_calculation`.

New `tests/test_strategy_binocular_pending.py`: long + short
`test_pending_setup_created_on_transition`,
`test_pending_setup_rejected_when_stop_too_wide`,
`test_pending_setup_rejected_when_rr_below_min`,
`test_setup_confirms_on_entry_breakout`,
`test_setup_expires_after_n_candles`,
`test_setup_invalidated_by_opposite_transition`,
`test_same_candle_sl_blocks_confirmation`,
`test_strict_mode_setup_rejected_without_vwap_confirmation`,
`test_strict_mode_setup_rejected_without_mtf_confirmation`,
`test_pending_setup_carries_position_size`.

New `tests/test_outcome_target_ladder.py`:
`test_t1_hit_realizes_half_position`,
`test_t1_then_breakeven_stop_is_a_small_win`,
`test_sl_before_t1_is_a_full_loss`,
`test_full_ladder_t1_t2_t3_all_hit`,
`test_same_candle_sl_priority_over_target`.

**Legacy cleanup**: `tests/test_ribbon_trendbar_indicators.py` and
`tests/test_strategy_ribbon_trendbar.py` deleted (test functions/logic
removed above). `tests/strategy_fixtures.py`: remove ribbon-flip-only
fixture builders no longer used by any remaining test, add new builders
for pending-setup/target-ladder test data. `tests/test_indicators.py`,
`tests/test_outcome_check.py`, `tests/test_scalper_v3_strategy.py`,
`tests/test_super_scalper_v3.py`, `tests/test_backtest_engine.py`
unaffected (none touch anything removed above).

## Backtest utility

`scripts/backtest_simple_strategy.py` needs a structural rewrite — its
current loop assumes one `evaluate_symbol(symbol) -> Signal` call per bar
with an immediately-known TP/SL, which doesn't fit a create-now/
confirm-later strategy. New loop, per symbol:

```
for i in range(min_start, len(df)-1):
    if in an open confirmed trade: skip ahead (unchanged pattern)
    elif an armed pending setup exists:
        check confirmation/expiry/invalidation against bar i (as today's live check_setup_confirmation would)
        if confirmed: run check_target_ladder forward from bar i, record the Trade, skip ahead past its close
    else:
        try detect_pending_setup(symbol) as-of bar i; if found, hold it as the active pending setup and keep scanning
```
Reuses `check_target_ladder` directly (same function the live bot calls),
same source-of-truth principle the script already follows for
`evaluate_symbol`. `min_start` recomputed as
`max(RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD, RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH) + 10`.
`BacktestStats`/`Trade` gain `t1_hit`/`t2_hit`/`t3_hit` fields so the
report can show T1/T2/T3 hit-rates (the architecture doc explicitly asks
for these) alongside win rate/PF/drawdown, which the report already
computes; also add a monthly-performance breakdown (grouping `Trade`s by
the confirmation candle's month), the one architecture.txt report item
(`§16`) the existing report doesn't already produce.

**Backtesting `SIGNAL_MODE=strict`**: when the script is run with
`SIGNAL_MODE=strict` set, `detect_pending_setup`'s as-of view must also
fetch and monkeypatch each of `CONFIRMATION_TIMEFRAMES` the same way it
already fakes `ENTRY_TF` (`_fake` in `backtest_symbol`, currently a single
`if interval == ENTRY_TF` branch) — extend that fake to serve the
higher-timeframe "as-of" slices too, sourced from separately-fetched
`get_klines_extended(symbol, tf, days)` calls per confirmation timeframe,
resampled/aligned to each bar `i`'s timestamp. This is real added
complexity, isolated to the `strict`-mode code path — `original`/`confirmed`
backtests (the default) don't pay for it.

**Required before this goes live**: run the rewritten script against the
same symbols and lookback window used in the 2026-07-27 baseline (per that
spec's numbers: 10 symbols, 6 months, 44 trades total, 30.8%/16.7%
LONG/SHORT win rate) and present a direct comparison. Only after that
comparison is reviewed does `STRATEGY_BINOCULAR_ENABLED` flip to `true`
(and `STRATEGY_V1_ENABLED` to `false`) as a separate, deliberate step —
not bundled into this implementation.

## Migration order

1. **Backup** — cut `backup/ribbon-trendbar-confirmation-v1` branch, push.
2. **Indicators + tests** — `calculate_pvt`, `calculate_pvt_signal`,
   `calculate_chandelier_direction`, `calculate_ema200`, unit tests, green.
3. **Pending-setup lifecycle** — `detect_pending_setup`,
   `check_setup_confirmation`, `database.py` schema/function additions,
   tests, green.
4. **Outcome ladder** — `outcome_check.check_target_ladder`, tests, green.
5. **Config** — remove old ribbon-flip-only settings, add new Binocular
   settings, `STRATEGY_NAME`/`STRATEGY_BINOCULAR_ENABLED` defaults.
6. **Dependents** — `main.py` (two-phase scan + ladder outcome check),
   `bot.py` (Python), `webui.py` (Python **and** JS).
7. **Backtest script** — rewrite for the two-phase/ladder model, run
   against the 2026-07-27 baseline symbols/period, capture the comparison.
8. **Cleanup** — delete superseded tests, full suite green, dry-run boot
   check with `STRATEGY_BINOCULAR_ENABLED=true` locally (server stays on
   v1 until the backtest comparison is reviewed and defaults are flipped
   in a follow-up change).

## Acceptance criteria

- No references to `calculate_trend_bar`/`_detect_ribbon_flip` remain
  (`calculate_ema_ribbon` correctly still remains, repurposed)
- Pending setups only confirm/expire/invalidate against completed candles;
  no forming-candle access anywhere in the new pipeline
- Every confirmed setup satisfies `valid_trade_geometry`, `rr >= MIN_RR`
  (vs T1), and structural SL `<= MAX_SL_ROI_PCT/100/LEVERAGE`
- `check_target_ladder` never marks a loss for a trade that has already
  realized any T1 profit; SL-before-T1 is always a full loss
- `SIGNAL_MODE=strict` (VWAP + MTF) and position sizing are fully
  implemented and tested, not stubbed — even though `SIGNAL_MODE` still
  defaults to `confirmed`
- `main.py`, `bot.py`, `webui.py` (Python and JS), `database.py` all
  updated and verified — none left referencing removed config constants
- `scalper_v3_strategy.py`, `backtest/engine.py`, `outcome_check.check_tp_sl`
  all untouched and still function (v3 track fully independent)
- All tests pass; rewritten backtest script runs with no future-data
  leakage; its results are compared against the 2026-07-27 baseline before
  any live-default change
- `STRATEGY_BINOCULAR_ENABLED=false` and `STRATEGY_V1_ENABLED=true` at the
  end of this work — flipping them is a deliberate follow-up, not part of
  this change
- `backup/ribbon-trendbar-confirmation-v1` branch exists on `origin`

## Final verification commands

```bash
python -m pytest -v
python -c "import config; import strategy; import main; import bot; import webui; import database; import outcome_check"
STRATEGY_BINOCULAR_ENABLED=true python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT WLD_USDT --days 180
DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false STRATEGY_BINOCULAR_ENABLED=true STRATEGY_V1_ENABLED=false python main.py
```
Confirm startup logs show strategy name `Binocular Pending-Breakout v1`,
`SIGNAL_MODE`, entry buffer, pending expiry candles, target fractions,
leverage, dry-run enabled — and that the backtest report includes T1/T2/T3
hit rates alongside win rate/PF/drawdown for comparison against the
2026-07-27 baseline.
