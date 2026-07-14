# Pluggable nw_ribbon/ema_confluence Base Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `nw_kernel.py` (ported from `~liqbot-poc/nw_kernel.py`) as a second selectable
`BASE_SIGNAL` trigger (`"nw_ribbon"`, alongside the existing `"ema_confluence"`), dispatched via a
dict in `strategy.py`, defaulting to today's live behavior unchanged.

**Architecture:** `nw_kernel.py` is a new, self-contained pure-math module (no imports from
`strategy.py`/`config.py`, mirrors `liq_estimator.py`'s independence). `strategy.py` renames its
existing base-signal function, adds a thin wrapper around `nw_kernel.base_signal_nw`, and resolves
one of the two via a module-level dispatch dict keyed by `config.BASE_SIGNAL`. Everything downstream
of the base signal (liquidity filter, arm/monitor lifecycle, OI poll loop, Telegram broadcast,
firing-budget throttling) is untouched and fully shared between both signals.

**Tech Stack:** Python 3.10, numpy, pandas, pytest (existing stack — no new dependencies).

## Global Constraints

- `BASE_SIGNAL` defaults to `"ema_confluence"` — merging to `main` (auto-deploys) must not change the
  server's live trigger behavior.
- `nw_ribbon` runs on the same 1m `SCALP_TF` as `ema_confluence` — no new timeframe, no new scheduler
  cadence, no new OI/ticker plumbing.
- `ema_confluence`'s kline fetch size (`SCALP_KLINE_COUNT`, and therefore its cumulative VWAP anchor)
  must not change. `nw_ribbon` gets its own `NW_KLINE_COUNT` (default 260) used only when selected.
- An invalid `BASE_SIGNAL` value must fail fast (`KeyError` at import), not silently fall back.
- No DB schema changes. The base-signal name is surfaced in the Telegram alert by prefixing
  `BASE_SIGNAL` into the existing `trend_summary`/`timeframe_summary` strings — no new `Signal`
  field, no `bot.py` changes.
- Full spec: `docs/superpowers/specs/2026-07-14-nw-ribbon-base-signal-design.md`.

---

### Task 1: Port `nw_kernel.py` with unit tests

**Files:**
- Create: `nw_kernel.py` (project root, alongside `liq_estimator.py`)
- Test: `tests/test_nw_kernel.py`

**Interfaces:**
- Produces (consumed by Task 3):
  - `rq_weights(n: int, h: float, r: float) -> np.ndarray`
  - `nw_estimate(closes: np.ndarray, h: float = 8.0, r: float = 8.0) -> float`
  - `nw_series(closes: np.ndarray, h: float = 8.0, r: float = 8.0, tail: int = 6) -> np.ndarray`
  - `nw_signal(closes: np.ndarray, h: float = 8.0, r: float = 8.0, lag: int = 2, smooth: bool = False) -> str | None`
    (returns `"bullish_change"` / `"bearish_change"` / `None`)
  - `ema(arr, n) -> np.ndarray`
  - `ema_ribbon_bias(closes: np.ndarray, fast: int = 20, mid: int = 50, slow: int = 100, trend: int = 200) -> str`
    (returns `"long"` / `"short"` / `"neutral"`)
  - `base_signal_nw(closes: np.ndarray, h: float = 8.0, r: float = 8.0, lag: int = 2, smooth: bool = False, fast: int = 20, mid: int = 50, slow: int = 100, trend: int = 200) -> str | None`
    (returns `"long"` / `"short"` / `None`)

- [ ] **Step 1: Write the failing test file**

All synthetic test data below was empirically verified against the poc's `nw_kernel.py` before
being locked into this plan — these are the exact arrays and exact expected outputs.

Create `tests/test_nw_kernel.py`:

```python
import numpy as np
import pytest

import nw_kernel as NW


def test_rq_weights_shape_and_positivity():
    w = NW.rq_weights(10, h=8.0, r=8.0)
    assert w.shape == (10,)
    assert np.all(w > 0)
    assert np.all(np.diff(w) < 0)  # weight strictly decreases as bars get older


def test_nw_estimate_is_non_repainting():
    rng = np.random.default_rng(42)
    base = 100 + np.cumsum(rng.normal(0, 1, 80))
    extra = 100 + np.cumsum(rng.normal(0, 1, 20)) + base[-1]
    extended = np.concatenate([base, extra])
    k = 50
    estimate_before_extension = NW.nw_estimate(base[:k + 1])
    estimate_after_extension = NW.nw_estimate(extended[:k + 1])
    assert estimate_after_extension == pytest.approx(estimate_before_extension)


def test_nw_signal_detects_bullish_change_on_pullback_resume():
    closes = np.concatenate([np.linspace(100, 70, 60), [100.0]])
    assert NW.nw_signal(closes) == "bullish_change"


def test_nw_signal_detects_bearish_change_on_spike_reversal():
    closes = np.concatenate([np.linspace(100, 130, 60), [100.0]])
    assert NW.nw_signal(closes) == "bearish_change"


def test_nw_signal_none_on_flat_market():
    closes = np.full(80, 100.0)
    assert NW.nw_signal(closes) is None


def test_ema_ribbon_bias_long_on_uptrend():
    closes = 100 + np.cumsum(np.full(250, 0.3))
    assert NW.ema_ribbon_bias(closes) == "long"


def test_ema_ribbon_bias_short_on_downtrend():
    closes = 100 - np.cumsum(np.full(250, 0.3))
    assert NW.ema_ribbon_bias(closes) == "short"


def test_ema_ribbon_bias_neutral_on_flat_market():
    closes = np.full(250, 100.0)
    assert NW.ema_ribbon_bias(closes) == "neutral"


def _build_bullish_pullback_resume():
    pre = 100 + np.cumsum(np.full(400, 0.3))
    pullback = np.linspace(pre[-1], pre[-1] - 2, 10)
    resume = pullback[-1] + np.linspace(0, 1.0, 2)[1:]
    return np.concatenate([pre, pullback, resume])


def _build_bearish_bounce_resume():
    pre = 100 + np.cumsum(np.full(400, -0.3))
    bounce = np.linspace(pre[-1], pre[-1] + 2, 10)
    resume = bounce[-1] - np.linspace(0, 1.0, 2)[1:]
    return np.concatenate([pre, bounce, resume])


def test_base_signal_nw_fires_long_when_turn_agrees_with_ribbon():
    closes = _build_bullish_pullback_resume()
    assert NW.ema_ribbon_bias(closes) == "long"
    assert NW.nw_signal(closes) == "bullish_change"
    assert NW.base_signal_nw(closes) == "long"


def test_base_signal_nw_fires_short_when_turn_agrees_with_ribbon():
    closes = _build_bearish_bounce_resume()
    assert NW.ema_ribbon_bias(closes) == "short"
    assert NW.nw_signal(closes) == "bearish_change"
    assert NW.base_signal_nw(closes) == "short"


def test_base_signal_nw_none_when_turn_disagrees_with_ribbon():
    pre = 100 + np.cumsum(np.full(400, 0.3))
    spike = np.linspace(pre[-1], pre[-1] + 30, 60)
    jump_down = np.array([pre[-1]])
    closes = np.concatenate([pre, spike, jump_down])
    assert NW.ema_ribbon_bias(closes) == "long"
    assert NW.nw_signal(closes) == "bearish_change"
    assert NW.base_signal_nw(closes) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_nw_kernel.py -v`
Expected: `ModuleNotFoundError: No module named 'nw_kernel'` (or collection error) — the module
doesn't exist yet.

- [ ] **Step 3: Write `nw_kernel.py`**

Create `nw_kernel.py`:

```python
"""
Nadaraya-Watson regression, Rational Quadratic kernel -- non-repainting port
of jdehorty's Pine v5 indicator (MPL 2.0). Causal: bar i uses only bars <= i.

Signals (matching Pine):
  slope-change mode (smoothColors=false): bullish when yhat1 turns up
  crossover mode   (smoothColors=true) : yhat2 (h-lag) crossing yhat1

Self-contained pure-math module: no imports from strategy.py or config.py,
matching how liq_estimator.py stays independently testable.
"""

from __future__ import annotations

import numpy as np


def rq_weights(n: int, h: float, r: float) -> np.ndarray:
    """w_i = (1 + i^2 / (2*r*h^2))^(-r) for i = 0..n-1 (i=0 is newest bar)."""
    i = np.arange(n, dtype=float)
    return np.power(1.0 + (i ** 2) / (h ** 2 * 2.0 * r), -r)


def nw_estimate(closes: np.ndarray, h: float = 8.0, r: float = 8.0) -> float:
    """Kernel estimate at the LAST bar, using the trailing window like Pine."""
    win = min(len(closes), 500)   # cap for speed; weights ~0 beyond this
    seg = closes[-win:][::-1]     # newest first
    w = rq_weights(len(seg), h, r)
    return float(np.dot(seg, w) / w.sum())


def nw_series(closes: np.ndarray, h: float = 8.0, r: float = 8.0, tail: int = 6) -> np.ndarray:
    """Last `tail` estimates (enough for slope/cross detection)."""
    out = []
    for k in range(len(closes) - tail, len(closes)):
        out.append(nw_estimate(closes[:k + 1], h, r))
    return np.array(out)


def nw_signal(closes: np.ndarray, h: float = 8.0, r: float = 8.0,
              lag: int = 2, smooth: bool = False) -> str | None:
    """Returns 'bullish_change' | 'bearish_change' | None on the last closed bar."""
    if len(closes) < 60:
        return None
    y1 = nw_series(closes, h, r)
    if smooth:
        y2 = nw_series(closes, h - lag, r)
        if y2[-2] <= y1[-2] and y2[-1] > y1[-1]:
            return "bullish_change"
        if y2[-2] >= y1[-2] and y2[-1] < y1[-1]:
            return "bearish_change"
        return None
    was_bear = y1[-3] > y1[-2]
    was_bull = y1[-3] < y1[-2]
    is_bear = y1[-2] > y1[-1]
    is_bull = y1[-2] < y1[-1]
    if is_bull and was_bear:
        return "bullish_change"
    if is_bear and was_bull:
        return "bearish_change"
    return None


def ema(arr, n):
    a = np.asarray(arr, dtype=float)
    alpha = 2 / (n + 1)
    out = np.empty_like(a)
    out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def ema_ribbon_bias(closes: np.ndarray, fast: int = 20, mid: int = 50,
                     slow: int = 100, trend: int = 200) -> str:
    """'long' | 'short' | 'neutral' from the fast/mid/slow/trend EMA stack."""
    e_fast, e_mid, e_slow, e_trend = (ema(closes, n)[-1] for n in (fast, mid, slow, trend))
    px = closes[-1]
    if e_fast > e_mid > e_slow and px > e_trend:
        return "long"
    if e_fast < e_mid < e_slow and px < e_trend:
        return "short"
    return "neutral"


def base_signal_nw(closes: np.ndarray, h: float = 8.0, r: float = 8.0, lag: int = 2,
                    smooth: bool = False, fast: int = 20, mid: int = 50,
                    slow: int = 100, trend: int = 200) -> str | None:
    """Combined: NW slope-turn fires the trigger, EMA ribbon gates direction.
    Only take NW turns IN the direction of the ribbon (trend continuation)."""
    trig = nw_signal(closes, h, r, lag, smooth)
    if trig is None:
        return None
    bias = ema_ribbon_bias(closes, fast, mid, slow, trend)
    if trig == "bullish_change" and bias == "long":
        return "long"
    if trig == "bearish_change" and bias == "short":
        return "short"
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_nw_kernel.py -v`
Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add nw_kernel.py tests/test_nw_kernel.py
git commit -m "feat: port nw_ribbon kernel (Nadaraya-Watson + EMA ribbon) from liqbot-poc"
```

---

### Task 2: Add `BASE_SIGNAL` + NW config constants

**Files:**
- Modify: `config.py:61-76` (between `STRATEGY_NAME` and `TARGET_MARGIN_PROFIT`)
- Modify: `.env.example:23-24` (between `FUNDING_EXTREME` and the trailing comment block)

**Interfaces:**
- Consumes: nothing (pure config).
- Produces (consumed by Task 3): `config.BASE_SIGNAL: str`, `config.NW_H/NW_R/NW_LAG/NW_SMOOTH: float|int|bool`,
  `config.EMA_RIBBON_FAST/MID/SLOW/TREND: int`, `config.NW_KLINE_COUNT: int`.

- [ ] **Step 1: Add the new constants to `config.py`**

In `config.py`, the existing block currently reads (around line 63):

```python
# ── Base signal (1m EMA/RSI/VWAP/volume) ────────────────────────────
SCALP_TF: str               = os.getenv("SCALP_TF", "1m")
```

Insert a new section immediately **before** that line:

```python
# ── Base signal selection ───────────────────────────────────────────
BASE_SIGNAL: str = os.getenv("BASE_SIGNAL", "ema_confluence")   # "ema_confluence" | "nw_ribbon"

# ── Base signal (1m EMA/RSI/VWAP/volume) ────────────────────────────
SCALP_TF: str               = os.getenv("SCALP_TF", "1m")
```

Then, in the existing block that currently ends with (around line 75):

```python
SCALP_VOLUME_MIN_MULT: float  = float(os.getenv("SCALP_VOLUME_MIN_MULT", "1.3"))
SCALP_VOLUME_MA_BARS: int     = int(os.getenv("SCALP_VOLUME_MA_BARS", "20"))
```

Add immediately after `SCALP_VOLUME_MA_BARS`:

```python
SCALP_VOLUME_MIN_MULT: float  = float(os.getenv("SCALP_VOLUME_MIN_MULT", "1.3"))
SCALP_VOLUME_MA_BARS: int     = int(os.getenv("SCALP_VOLUME_MA_BARS", "20"))

# ── Base signal (nw_ribbon: Nadaraya-Watson kernel + EMA ribbon) ────
# Only used when BASE_SIGNAL=nw_ribbon. NW_KLINE_COUNT is separate from
# SCALP_KLINE_COUNT so ema_confluence's cumulative-VWAP anchor never shifts.
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

- [ ] **Step 2: Verify config imports cleanly**

Run: `python -c "import config; print(config.BASE_SIGNAL, config.NW_KLINE_COUNT, config.EMA_RIBBON_TREND)"`
Expected: `ema_confluence 260 200`

- [ ] **Step 3: Document the new vars in `.env.example`**

In `.env.example`, the block currently reads (around line 23):

```
OI_POLL_SEC=60
FUNDING_EXTREME=0.0004

# 1m scalping fires much more often than the old hourly VP-OB strategy.
```

Insert between `FUNDING_EXTREME` and the trailing comment block:

```
OI_POLL_SEC=60
FUNDING_EXTREME=0.0004

# Pluggable base signal -- "ema_confluence" (default, live today) or
# "nw_ribbon" (Nadaraya-Watson kernel + EMA 20/50/100/200 ribbon, ported
# from ~liqbot-poc/nw_kernel.py). The NW_*/EMA_RIBBON_*/NW_KLINE_COUNT vars
# below only apply when BASE_SIGNAL=nw_ribbon.
BASE_SIGNAL=ema_confluence
NW_H=8.0
NW_R=8.0
NW_LAG=2
NW_SMOOTH=false
EMA_RIBBON_FAST=20
EMA_RIBBON_MID=50
EMA_RIBBON_SLOW=100
EMA_RIBBON_TREND=200
NW_KLINE_COUNT=260

# 1m scalping fires much more often than the old hourly VP-OB strategy.
```

- [ ] **Step 4: Commit**

```bash
git add config.py .env.example
git commit -m "feat: add BASE_SIGNAL/NW_* config constants for pluggable base signal"
```

---

### Task 3: Wire the dispatch into `strategy.py`

**Files:**
- Modify: `strategy.py:35-60` (config import block)
- Modify: `strategy.py:155-176` (`_base_signal` → rename + new wrapper + dispatch)
- Modify: `strategy.py:263-323` (`_try_arm_setup`)
- Modify: `strategy.py:328-378` (`_monitor_setup`)
- Modify: `tests/test_strategy_liq_scalp.py:6` (import fix)

**Interfaces:**
- Consumes: `nw_kernel.base_signal_nw` (Task 1), `config.BASE_SIGNAL`/`NW_*`/`EMA_RIBBON_*`/`NW_KLINE_COUNT` (Task 2).
- Produces: `strategy._base_signal(df: pd.DataFrame) -> str | None` (unchanged public shape, now
  dispatches to whichever signal is configured) — no other module imports from `strategy.py`'s
  internals, so nothing outside this file needs to change.

- [ ] **Step 1: Update the config import block**

In `strategy.py`, the import block currently reads:

```python
from config import (
    SCALP_TF,
    SCALP_KLINE_COUNT,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_PERIOD,
    RSI_LONG_MIN,
    RSI_LONG_MAX,
    RSI_SHORT_MIN,
    RSI_SHORT_MAX,
    SCALP_VOLUME_MIN_MULT,
    SCALP_VOLUME_MA_BARS,
    TARGET_MARGIN_PROFIT,
    MIN_RR,
    MAX_SL_PRICE_PCT,
    LEVERAGE_TIERS,
    MMR_BUFFER,
    BUCKET_PCT,
    CLUSTER_DECAY,
    CLUSTER_LOOKAROUND,
    CLUSTER_MIN_PERCENTILE,
    FUNDING_EXTREME,
    SCALP_ARM_MAX_AGE_BARS,
    LEVERAGE,
)
```

Replace with:

```python
from typing import Callable

import nw_kernel
from config import (
    BASE_SIGNAL,
    SCALP_TF,
    SCALP_KLINE_COUNT,
    NW_KLINE_COUNT,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_PERIOD,
    RSI_LONG_MIN,
    RSI_LONG_MAX,
    RSI_SHORT_MIN,
    RSI_SHORT_MAX,
    SCALP_VOLUME_MIN_MULT,
    SCALP_VOLUME_MA_BARS,
    NW_H,
    NW_R,
    NW_LAG,
    NW_SMOOTH,
    EMA_RIBBON_FAST,
    EMA_RIBBON_MID,
    EMA_RIBBON_SLOW,
    EMA_RIBBON_TREND,
    TARGET_MARGIN_PROFIT,
    MIN_RR,
    MAX_SL_PRICE_PCT,
    LEVERAGE_TIERS,
    MMR_BUFFER,
    BUCKET_PCT,
    CLUSTER_DECAY,
    CLUSTER_LOOKAROUND,
    CLUSTER_MIN_PERCENTILE,
    FUNDING_EXTREME,
    SCALP_ARM_MAX_AGE_BARS,
    LEVERAGE,
)
```

- [ ] **Step 2: Rename `_base_signal`, add the nw_ribbon wrapper, and the dispatch**

The function currently reads:

```python
def _base_signal(df: pd.DataFrame) -> str | None:
    """EMA 9/21/50 stack + rolling VWAP side + RSI zone + volume confirmation."""
    close = df["close"].astype(float).to_numpy()
    if len(close) < EMA_SLOW + 5:
        return None
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    volume = df["volume"].astype(float).to_numpy()

    e_fast = _ema(close, EMA_FAST)[-1]
    e_mid = _ema(close, EMA_MID)[-1]
    e_slow = _ema(close, EMA_SLOW)[-1]
    r = _rsi(close, RSI_PERIOD)[-1]
    vwap = _rolling_vwap(high, low, close, volume)[-1]
    vol_ok = volume[-1] > SCALP_VOLUME_MIN_MULT * volume[-(SCALP_VOLUME_MA_BARS + 1): -1].mean()
    price = close[-1]

    if e_fast > e_mid > e_slow and price > vwap and RSI_LONG_MIN < r < RSI_LONG_MAX and vol_ok:
        return "LONG"
    if e_fast < e_mid < e_slow and price < vwap and RSI_SHORT_MIN < r < RSI_SHORT_MAX and vol_ok:
        return "SHORT"
    return None
```

Replace with (only the function name changes, body identical, plus the new wrapper and dispatch
appended after it):

```python
def _base_signal_ema_confluence(df: pd.DataFrame) -> str | None:
    """EMA 9/21/50 stack + rolling VWAP side + RSI zone + volume confirmation."""
    close = df["close"].astype(float).to_numpy()
    if len(close) < EMA_SLOW + 5:
        return None
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    volume = df["volume"].astype(float).to_numpy()

    e_fast = _ema(close, EMA_FAST)[-1]
    e_mid = _ema(close, EMA_MID)[-1]
    e_slow = _ema(close, EMA_SLOW)[-1]
    r = _rsi(close, RSI_PERIOD)[-1]
    vwap = _rolling_vwap(high, low, close, volume)[-1]
    vol_ok = volume[-1] > SCALP_VOLUME_MIN_MULT * volume[-(SCALP_VOLUME_MA_BARS + 1): -1].mean()
    price = close[-1]

    if e_fast > e_mid > e_slow and price > vwap and RSI_LONG_MIN < r < RSI_LONG_MAX and vol_ok:
        return "LONG"
    if e_fast < e_mid < e_slow and price < vwap and RSI_SHORT_MIN < r < RSI_SHORT_MAX and vol_ok:
        return "SHORT"
    return None


def _base_signal_nw_ribbon(df: pd.DataFrame) -> str | None:
    """Nadaraya-Watson kernel slope-turn, gated by the EMA 20/50/100/200 ribbon."""
    closes = df["close"].astype(float).to_numpy()
    direction = nw_kernel.base_signal_nw(
        closes, h=NW_H, r=NW_R, lag=NW_LAG, smooth=NW_SMOOTH,
        fast=EMA_RIBBON_FAST, mid=EMA_RIBBON_MID, slow=EMA_RIBBON_SLOW, trend=EMA_RIBBON_TREND,
    )
    return direction.upper() if direction else None


_BASE_SIGNAL_FNS: dict[str, Callable[[pd.DataFrame], str | None]] = {
    "ema_confluence": _base_signal_ema_confluence,
    "nw_ribbon": _base_signal_nw_ribbon,
}
_base_signal = _BASE_SIGNAL_FNS[BASE_SIGNAL]
_KLINE_COUNT = SCALP_KLINE_COUNT if BASE_SIGNAL == "ema_confluence" else NW_KLINE_COUNT
_MIN_BARS = (EMA_SLOW + 6) if BASE_SIGNAL == "ema_confluence" else (EMA_RIBBON_TREND + 6)
```

- [ ] **Step 3: Update `_try_arm_setup` to use `_KLINE_COUNT`/`_MIN_BARS` and tag the trigger name**

The function currently reads:

```python
def _try_arm_setup(symbol: str) -> None:
    if db.armed_setup_exists(symbol):
        return

    df = get_klines(symbol, SCALP_TF, count=SCALP_KLINE_COUNT)
    if df is None or df.empty or len(df) < EMA_SLOW + 6:
        return
```

Replace the fetch/guard lines with:

```python
def _try_arm_setup(symbol: str) -> None:
    if db.armed_setup_exists(symbol):
        return

    df = get_klines(symbol, SCALP_TF, count=_KLINE_COUNT)
    if df is None or df.empty or len(df) < _MIN_BARS:
        return
```

Further down, this line:

```python
            "trend_summary": f"1m base signal + liq filter passed | funding {funding * 100:.4f}%",
```

becomes:

```python
            "trend_summary": f"1m {BASE_SIGNAL} + liq filter passed | funding {funding * 100:.4f}%",
```

And this line:

```python
    db.save_armed_setup({
        "symbol": symbol,
        "direction": direction,
        "trigger_price": price,
        "entry_low": price,
        "entry_high": price,
        "sl_price": provisional_sl,
        "tp_price": provisional_tp,
        "rr": round(provisional_rr, 2),
        "score": 0.0,
        "setup_reason": reason,
        "trend_summary": f"1m base signal (awaiting liq filter) | funding {funding * 100:.4f}%",
        "expires_at": expires_at.isoformat(),
    })
```

becomes:

```python
    db.save_armed_setup({
        "symbol": symbol,
        "direction": direction,
        "trigger_price": price,
        "entry_low": price,
        "entry_high": price,
        "sl_price": provisional_sl,
        "tp_price": provisional_tp,
        "rr": round(provisional_rr, 2),
        "score": 0.0,
        "setup_reason": reason,
        "trend_summary": f"1m {BASE_SIGNAL} (awaiting liq filter) | funding {funding * 100:.4f}%",
        "expires_at": expires_at.isoformat(),
    })
```

- [ ] **Step 4: Update `_monitor_setup` to use `_KLINE_COUNT`/`_MIN_BARS` and tag the trigger name**

This block:

```python
    df = get_klines(symbol, SCALP_TF, count=SCALP_KLINE_COUNT)
    if df is None or df.empty or len(df) < EMA_SLOW + 6:
        return None
    window = df.iloc[:-1]
```

becomes:

```python
    df = get_klines(symbol, SCALP_TF, count=_KLINE_COUNT)
    if df is None or df.empty or len(df) < _MIN_BARS:
        return None
    window = df.iloc[:-1]
```

And this line:

```python
        timeframe_summary=f"1m liq-scalp | {reason}",
```

becomes:

```python
        timeframe_summary=f"1m {BASE_SIGNAL} liq-scalp | {reason}",
```

- [ ] **Step 5: Fix the existing test's import**

In `tests/test_strategy_liq_scalp.py`, line 6 currently reads:

```python
from strategy import _base_signal, _evaluate_liquidity, _valid_trade_geometry
```

Replace with:

```python
from strategy import _base_signal_ema_confluence as _base_signal, _evaluate_liquidity, _valid_trade_geometry
```

(The rest of that test file calls `_base_signal(df)` — the alias means no other line in that file
needs to change.)

- [ ] **Step 6: Run the full test suite**

Run: `pytest -v`
Expected: all tests PASS, including the renamed-import `test_strategy_liq_scalp.py` tests and the
new `test_nw_kernel.py` tests from Task 1.

- [ ] **Step 7: Verify the default `BASE_SIGNAL` still resolves to `ema_confluence` behavior**

Run: `python -c "import strategy; print(strategy.BASE_SIGNAL, strategy._base_signal.__name__, strategy._KLINE_COUNT, strategy._MIN_BARS)"`
Expected: `ema_confluence _base_signal_ema_confluence 100 56`

- [ ] **Step 8: Commit**

```bash
git add strategy.py tests/test_strategy_liq_scalp.py
git commit -m "feat: dispatch strategy base signal through BASE_SIGNAL (ema_confluence | nw_ribbon)"
```

---

### Task 4: Local live verification of `nw_ribbon`

**Files:** none (verification only, no code changes).

**Interfaces:** none.

- [ ] **Step 1: Confirm the server's `mexc-bot` service is stopped**

If it's running, stop it first (SSH to `68.168.222.74`, `systemctl stop mexc-bot`) — running two
instances against the same Telegram bot token causes a polling conflict (see prior verification
session in this conversation for why).

- [ ] **Step 2: Run the bot locally with `BASE_SIGNAL=nw_ribbon` and loosened throttling**

From the project root (same pattern used for the earlier `ema_confluence` verification —
env vars only, `.env` itself is never modified):

```bash
BASE_SIGNAL=nw_ribbon MIN_DAILY_SIGNAL_GAP_MINUTES=2 SIGNAL_COOLDOWN_MINUTES=2 python main.py > run_local_nw.out.log 2>&1 &
```

- [ ] **Step 3: Confirm the config picked up `nw_ribbon`**

Run: `grep -E "CONFIG|Scheduler started" run_local_nw.out.log`
Expected: a `[CONFIG]` line, and no import errors.

- [ ] **Step 4: Watch the log for `nw_ribbon` arm/monitor/fire activity**

Tail `run_local_nw.out.log` (or use the Monitor tool as in the earlier session) watching for
`[SCALP-ARM]`, `[SCALP-SIGNAL]`, `[SCALP-INVALIDATE]`, `[SCAN] Fired`, and any `Traceback`/`ERROR`.
Confirm at least one full `[SCAN] Fired #N | ...` line appears, proving the `nw_ribbon` trigger
reaches all the way through the liquidity filter and firing-budget logic, exactly like the
`ema_confluence` run verified earlier in this conversation.

- [ ] **Step 5: Stop the local process and clean up**

```bash
# find and kill the local python main.py process, then:
rm -f run_local_nw.out.log
```

Do not commit any log files. Restart the server's `mexc-bot` service if it was stopped in Step 1.

**Result of first run (2026-07-14):** confirmed a real defect — see Task 5 below. `nw_ribbon`
armed setups (including ones with a fully-cleared liquidity filter) were invalidated on the very
next monitor cycle, every time, before ever reaching a fire. Task 5 fixes this; Task 4's steps are
re-run after Task 5 lands (see Task 5's final step).

---

### Task 5: Make monitor-phase re-confirmation signal-specific

**Why:** discovered via Task 4's live run, documented in the design spec's "Addendum" section
(`docs/superpowers/specs/2026-07-14-nw-ribbon-base-signal-design.md`). `main.py` runs Phase 2
(monitor, over all coins) before Phase 1 (arm) each cycle (`main.py:135-146` then
`main.py:239-255`), so a setup armed in cycle *N* is first checked by `_monitor_setup` in cycle
*N+1*. `nw_kernel.nw_signal` is a one-bar pulse (fires only on the exact turn bar, `None`
afterward), so `_monitor_setup`'s re-confirmation (`_base_signal(window) != direction` →
invalidate) killed every `nw_ribbon` setup one cycle after arming — including setups whose
liquidity filter had already fully cleared — because the pulse had already faded by the next
check. `ema_confluence` is unaffected: its condition is a snapshot that holds across several bars,
which is why this was never visible before `nw_ribbon` existed.

**Files:**
- Modify: `strategy.py` (new dispatch functions after the existing `_BASE_SIGNAL_FNS` block; one
  changed line inside `_monitor_setup`)
- Modify: `tests/test_strategy_liq_scalp.py` (import line + 5 new tests)

**Interfaces:**
- Consumes: `_base_signal_ema_confluence` (Task 3), `nw_kernel.ema_ribbon_bias` (Task 1),
  `EMA_RIBBON_FAST/MID/SLOW/TREND` (Task 2, already imported into `strategy.py` by Task 3).
- Produces: `strategy._still_active_ema_confluence(df: pd.DataFrame, direction: str) -> bool`,
  `strategy._still_active_nw_ribbon(df: pd.DataFrame, direction: str) -> bool` — used only inside
  `_monitor_setup`, no other module needs these.

- [ ] **Step 1: Write the failing tests**

In `tests/test_strategy_liq_scalp.py`, the import line currently reads (after Task 3's fix):

```python
from strategy import _base_signal_ema_confluence as _base_signal, _evaluate_liquidity, _valid_trade_geometry
```

Replace with:

```python
from strategy import (
    _base_signal_ema_confluence as _base_signal,
    _evaluate_liquidity,
    _valid_trade_geometry,
    _still_active_ema_confluence,
    _still_active_nw_ribbon,
)
```

Then append these tests to the end of the file (after `test_valid_trade_geometry`):

```python
def test_still_active_ema_confluence_true_when_signal_still_matches():
    df = _build_df([4, 4, -5])
    assert _still_active_ema_confluence(df, "LONG") is True


def test_still_active_ema_confluence_false_when_signal_no_longer_matches():
    df = _build_df([4, 4, -5])
    assert _still_active_ema_confluence(df, "SHORT") is False


def test_still_active_nw_ribbon_true_when_ribbon_still_agrees():
    closes = 100 + np.cumsum(np.full(250, 0.3))
    df = pd.DataFrame({"close": closes})
    assert _still_active_nw_ribbon(df, "LONG") is True


def test_still_active_nw_ribbon_false_when_ribbon_disagrees():
    closes = 100 + np.cumsum(np.full(250, 0.3))
    df = pd.DataFrame({"close": closes})
    assert _still_active_nw_ribbon(df, "SHORT") is False


def test_still_active_nw_ribbon_false_when_ribbon_neutral():
    closes = np.full(250, 100.0)
    df = pd.DataFrame({"close": closes})
    assert _still_active_nw_ribbon(df, "LONG") is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_strategy_liq_scalp.py -v`
Expected: collection error / `ImportError: cannot import name '_still_active_ema_confluence'` —
the functions don't exist in `strategy.py` yet.

- [ ] **Step 3: Add the new dispatch to `strategy.py`**

The dispatch block currently reads:

```python
_BASE_SIGNAL_FNS: dict[str, Callable[[pd.DataFrame], str | None]] = {
    "ema_confluence": _base_signal_ema_confluence,
    "nw_ribbon": _base_signal_nw_ribbon,
}
_base_signal = _BASE_SIGNAL_FNS[BASE_SIGNAL]
_KLINE_COUNT = SCALP_KLINE_COUNT if BASE_SIGNAL == "ema_confluence" else NW_KLINE_COUNT
_MIN_BARS = (EMA_SLOW + 6) if BASE_SIGNAL == "ema_confluence" else (EMA_RIBBON_TREND + 6)
```

Replace with (appending the new dispatch immediately after, nothing above this changes):

```python
_BASE_SIGNAL_FNS: dict[str, Callable[[pd.DataFrame], str | None]] = {
    "ema_confluence": _base_signal_ema_confluence,
    "nw_ribbon": _base_signal_nw_ribbon,
}
_base_signal = _BASE_SIGNAL_FNS[BASE_SIGNAL]
_KLINE_COUNT = SCALP_KLINE_COUNT if BASE_SIGNAL == "ema_confluence" else NW_KLINE_COUNT
_MIN_BARS = (EMA_SLOW + 6) if BASE_SIGNAL == "ema_confluence" else (EMA_RIBBON_TREND + 6)


def _still_active_ema_confluence(df: pd.DataFrame, direction: str) -> bool:
    return _base_signal_ema_confluence(df) == direction


def _still_active_nw_ribbon(df: pd.DataFrame, direction: str) -> bool:
    """Re-confirm only the EMA ribbon bias -- nw_signal's slope-turn is a
    one-bar pulse that fades immediately, so re-demanding a fresh pulse every
    monitor cycle would invalidate every setup before it could ever fire."""
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

- [ ] **Step 4: Update `_monitor_setup`'s invalidation check**

This line:

```python
    if _base_signal(window) != direction:
        db.mark_armed_setup_invalidated(setup["id"], "base signal no longer active")
        logger.info("[SCALP-INVALIDATE] %s %s base signal dropped", symbol, direction)
        return None
```

becomes:

```python
    if not _still_active(window, direction):
        db.mark_armed_setup_invalidated(setup["id"], "base signal no longer active")
        logger.info("[SCALP-INVALIDATE] %s %s base signal dropped", symbol, direction)
        return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_strategy_liq_scalp.py -v`
Expected: all tests PASS, including the 5 new ones.

- [ ] **Step 6: Run the full test suite**

Run: `pytest -v`
Expected: all tests PASS (no regressions in `ema_confluence`'s behavior — its re-confirmation path
is unchanged, just renamed/wrapped).

- [ ] **Step 7: Commit**

```bash
git add strategy.py tests/test_strategy_liq_scalp.py
git commit -m "fix: monitor-phase re-confirmation for nw_ribbon checks ribbon bias, not the one-bar pulse"
```

- [ ] **Step 8: Re-run Task 4's live verification**

Repeat Task 4's Steps 1-5 exactly (confirm server bot stopped, run locally with
`BASE_SIGNAL=nw_ribbon` + loosened throttling, watch logs, confirm at least one `[SCAN] Fired #N`
line, stop and clean up). This time, an `nw_ribbon` setup that clears the liquidity filter should
survive past the next monitor cycle (ribbon bias persists across many bars, unlike the pulse) and
should be able to reach a fire.

---

## Post-plan

`main` is unaffected until you decide to merge `feature/liq-scalp-v14`. `BASE_SIGNAL` defaults to
`ema_confluence`, so merging (without also changing `.env` on the server) changes nothing about the
server's live behavior — `nw_ribbon` only activates if you explicitly set `BASE_SIGNAL=nw_ribbon` in
the server's `.env` afterward.
