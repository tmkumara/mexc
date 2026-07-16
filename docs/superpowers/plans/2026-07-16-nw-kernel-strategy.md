# Nadaraya-Watson Kernel Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, backtest-research-only trading strategy around
a Python port of the "Nadaraya-Watson: Rational Quadratic Kernel
(Non-Repainting)" Pine Script indicator, replacing the retired Fibonacci MTF
strategy as this project's active research direction.

**Architecture:** `nw_kernel.py` ports the indicator's causal kernel
regression and slope-flip detection. `nw_strategy.py` wraps it with a 4H
EMA(200) trend filter, RSI/volume confirmation, a BTC 4H market-safety
filter, and fixed-ROI TP with a structural (swing + ATR buffer, capped)
SL — a single public entry point `evaluate_symbol_nw(symbol, btc_context,
reject_sink)`. `config_nw.py` is fully standalone. A backtest harness
(`scripts/backtest_nw_strategy.py`) walks 1H candles bar-by-bar exactly like
`scripts/backtest_fib_strategy.py`.

**Tech Stack:** Python, pandas, numpy, pytest.

## Global Constraints

- `nw_strategy.py` may import **only** `calculate_ema`, `calculate_rsi`,
  `calculate_atr` from the live bot's `strategy.py` — nothing else. No file
  under this strategy's ownership ever imports or modifies `main.py`,
  `strategy.py` (beyond those three functions), `config.py`, `bot.py`,
  `webui.py`, or `database.py`.
- `config_nw.py` is fully independent: every tunable value is
  `os.getenv("NW_...")`-driven with a hardcoded default, and it never reads
  the live bot's `config.py`. The four leverage/ROI/RR Python attribute
  names stay bare (`LEVERAGE`, `TARGET_ROI_PCT`, `MAX_SL_ROI_PCT`, `MIN_RR`)
  even though their env var names are `NW_`-prefixed — this matches
  `config_fib.py`'s existing convention.
- Only completed candles are ever evaluated (`iloc[:-1]` drops the forming
  candle before any indicator touches a dataframe).
- `mexc_client.py`, `coin_scanner.py`, `market_data.py`, and
  `scripts/backtest_simple_strategy.py` are reused read-only, unmodified.
- TP is fixed-distance: `TP_PRICE_PCT = TARGET_ROI_PCT / 100 / LEVERAGE`.
  SL is structural (swing level ± ATR buffer), with its price-distance
  **capped** (not rejected) at `MAX_SL_PRICE_PCT = MAX_SL_ROI_PCT / 100 /
  LEVERAGE` — this intentionally follows the original live bot's
  cap-not-reject SL model (see `docs/superpowers/specs/2026-07-16-nw-kernel-strategy-design.md`),
  not the fib strategy's later reject-if-too-wide redesign.
- Reference spec: `docs/superpowers/specs/2026-07-16-nw-kernel-strategy-design.md`.

---

### Task 1: Kernel indicator math (`nw_kernel.py`)

**Files:**
- Create: `nw_kernel.py`
- Test: `tests/test_nw_kernel.py`

**Interfaces:**
- Produces: `kernel_weight(i: int, h: float, r: float) -> float`,
  `nw_estimate(src: np.ndarray, h: float, r: float, window: int) -> np.ndarray`,
  `detect_slope_flip(yhat: np.ndarray) -> pd.Series` (values `"bullish"`,
  `"bearish"`, or `"none"`, same length as `yhat`, `object` dtype).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_nw_kernel.py
import numpy as np
import pandas as pd

from nw_kernel import kernel_weight, nw_estimate, detect_slope_flip


def test_kernel_weight_at_zero_distance_is_one():
    assert kernel_weight(0, 8.0, 8.0) == 1.0
    assert kernel_weight(0, 3.0, 25.0) == 1.0


def test_kernel_weight_decreases_with_distance():
    w0 = kernel_weight(0, 8.0, 8.0)
    w10 = kernel_weight(10, 8.0, 8.0)
    w50 = kernel_weight(50, 8.0, 8.0)
    assert w0 > w10 > w50 > 0.0


def _naive_kernel_regression(src: np.ndarray, h: float, r: float) -> np.ndarray:
    """Literal transcription of the Pine Script loop (sums all available
    history at every bar) -- used only as a reference to validate the
    vectorized nw_estimate against, not a hand-computed numeric answer."""
    n = len(src)
    out = np.full(n, np.nan)
    for t in range(n):
        cw = 0.0
        cumw = 0.0
        for i in range(t + 1):
            w = kernel_weight(i, h, r)
            cw += src[t - i] * w
            cumw += w
        out[t] = cw / cumw
    return out


def test_nw_estimate_matches_naive_full_history_sum():
    rng = np.random.default_rng(42)
    src = 100.0 + np.cumsum(rng.normal(0, 0.5, size=30))
    expected = _naive_kernel_regression(src, h=3.0, r=3.0)
    actual = nw_estimate(src, h=3.0, r=3.0, window=30)
    assert np.allclose(actual, expected, rtol=1e-9, atol=1e-9)


def test_nw_estimate_constant_series_returns_constant():
    src = np.full(50, 100.0)
    result = nw_estimate(src, h=8.0, r=8.0, window=300)
    assert np.allclose(result, 100.0)


def test_nw_estimate_truncation_is_close_to_full_history():
    rng = np.random.default_rng(7)
    src = 100.0 + np.cumsum(rng.normal(0, 0.5, size=150))
    full = nw_estimate(src, h=8.0, r=8.0, window=150)
    truncated = nw_estimate(src, h=8.0, r=8.0, window=60)
    # Rational-quadratic weight decays fast under h=8/r=8 (default config
    # params) -- bars beyond ~60 back contribute negligibly. If this
    # tolerance turns out too tight once actually run, loosen it slightly
    # rather than widening the window; the point of the test is that
    # truncation is a safe approximation, not exact equality.
    assert np.allclose(full[60:], truncated[60:], atol=1e-6)


def test_nw_estimate_window_larger_than_series_does_not_crash():
    src = np.array([100.0, 101.0, 99.0, 102.0])
    result = nw_estimate(src, h=8.0, r=8.0, window=300)
    assert len(result) == 4
    assert not np.isnan(result).any()


def test_detect_slope_flip_identifies_bearish_then_bullish():
    yhat = np.array([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0])
    flips = detect_slope_flip(yhat)
    assert list(flips) == ["none", "none", "none", "bearish", "none", "bullish", "none"]


def test_detect_slope_flip_handles_leading_nan():
    yhat = np.array([np.nan, np.nan, 1.0, 2.0, 1.0, 2.0])
    flips = detect_slope_flip(yhat)
    assert flips.iloc[0] == "none"
    assert flips.iloc[1] == "none"
    # bar 4 (value 1.0, after rising to 2.0 at bar3): bearish flip
    assert flips.iloc[4] == "bearish"
    # bar 5 (value 2.0, after falling to 1.0 at bar4): bullish flip
    assert flips.iloc[5] == "bullish"


def test_detect_slope_flip_flat_series_has_no_flips():
    yhat = np.full(10, 100.0)
    flips = detect_slope_flip(yhat)
    assert all(f == "none" for f in flips)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nw_kernel.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nw_kernel'`

- [ ] **Step 3: Implement `nw_kernel.py`**

```python
"""
Nadaraya-Watson rational-quadratic-kernel regression (non-repainting).

Ported from the Pine Script indicator "Nadaraya-Watson: Rational Quadratic
Kernel (Non-Repainting)" by jdehorty (MPL-2.0). `kernel_regression` there is
a causal (backward-looking only) kernel-weighted moving average, recomputed
fresh at every bar using only that bar and earlier ones -- non-repainting
because there is no centered/future-looking window. The weight of a bar `i`
positions back from the current bar is `(1 + i^2/(2*h^2*r))^-r`, decaying
with distance. `nw_estimate` here truncates the sum to the most recent
`window` bars per point as a numerical approximation of the Pine loop's
full-history sum -- see tests/test_nw_kernel.py for the accuracy check
against a literal, untruncated transcription of the Pine loop.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def kernel_weight(i: int, h: float, r: float) -> float:
    """Rational quadratic kernel weight for a bar `i` positions back."""
    return (1.0 + (i ** 2) / (h ** 2 * 2.0 * r)) ** (-r)


def nw_estimate(src: np.ndarray, h: float, r: float, window: int) -> np.ndarray:
    """Causal rational-quadratic kernel regression, one estimate per bar.

    For bar t, sums src[t], src[t-1], ..., src[max(0, t-window+1)] weighted
    by kernel_weight(i, h, r), i = distance back from t. Implemented as a
    causal convolution: full-mode np.convolve(src, w)[t] equals
    sum_i w[i]*src[t-i] over the valid (in-range) i, which is exactly this
    sum once sliced to the first len(src) outputs.
    """
    n = len(src)
    window = min(window, n)
    i = np.arange(window)
    w = (1.0 + (i ** 2) / (h ** 2 * 2.0 * r)) ** (-r)

    numerator = np.convolve(src, w, mode="full")[:n]
    denominator = np.convolve(np.ones(n), w, mode="full")[:n]
    return numerator / denominator


def detect_slope_flip(yhat: np.ndarray) -> pd.Series:
    """Per-bar bullish/bearish/none slope-flip classification, matching
    Pine's default (non-smoothColors) isBullishChange/isBearishChange: a
    bullish flip at bar t means yhat was falling into t-1 (yhat[t-2] >
    yhat[t-1]) and is now rising into t (yhat[t-1] < yhat[t]). Bearish is
    the mirror. The first two bars of any series are always "none", as is
    any bar whose flip comparison touches a NaN.
    """
    yhat = np.asarray(yhat, dtype=float)
    n = len(yhat)
    result = np.full(n, "none", dtype=object)

    for t in range(2, n):
        y0, y1, y2 = yhat[t - 2], yhat[t - 1], yhat[t]
        if np.isnan(y0) or np.isnan(y1) or np.isnan(y2):
            continue
        was_bearish = y0 > y1
        was_bullish = y0 < y1
        is_bearish = y1 > y2
        is_bullish = y1 < y2
        if is_bullish and was_bearish:
            result[t] = "bullish"
        elif is_bearish and was_bullish:
            result[t] = "bearish"

    return pd.Series(result)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_nw_kernel.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add nw_kernel.py tests/test_nw_kernel.py
git commit -m "feat: add Nadaraya-Watson kernel regression + slope-flip detection"
```

---

### Task 2: Standalone config (`config_nw.py`)

**Files:**
- Create: `config_nw.py`
- Test: `tests/test_config_nw.py`

**Interfaces:**
- Produces: all attributes listed below, consumed by `nw_strategy.py` in
  Tasks 3-7 and `scripts/backtest_nw_strategy.py` in Task 8.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_nw.py
import config_nw


def test_defaults_loaded():
    assert config_nw.NW_TREND_TF == "4h"
    assert config_nw.NW_ENTRY_TF == "1h"
    assert config_nw.NW_TREND_EMA_PERIOD == 200
    assert config_nw.NW_LOOKBACK_WINDOW == 8.0
    assert config_nw.NW_RELATIVE_WEIGHTING == 8.0
    assert config_nw.NW_KERNEL_SUM_WINDOW == 300
    assert config_nw.NW_SWING_FRACTAL_BARS == 5
    assert config_nw.NW_SWING_LOOKBACK_BARS == 20
    assert config_nw.NW_RSI_PERIOD == 14
    assert config_nw.NW_RSI_LONG_MIN == 45
    assert config_nw.NW_RSI_LONG_MAX == 75
    assert config_nw.NW_RSI_SHORT_MIN == 25
    assert config_nw.NW_RSI_SHORT_MAX == 55
    assert config_nw.NW_VOLUME_MA_PERIOD == 20
    assert config_nw.NW_MIN_VOLUME_MULTIPLIER == 1.0
    assert config_nw.NW_ATR_PERIOD == 14
    assert config_nw.NW_SL_ATR_BUFFER_MULTIPLIER == 0.5
    assert config_nw.LEVERAGE == 20
    assert config_nw.TARGET_ROI_PCT == 15.0
    assert config_nw.MAX_SL_ROI_PCT == 20.0
    assert config_nw.MIN_RR == 1.5
    assert config_nw.NW_ENABLE_BTC_FILTER is True
    assert config_nw.NW_BTC_FILTER_TF == "4h"
    assert config_nw.NW_SIGNAL_EXPIRE_HOURS == 48


def test_derived_tp_price_pct():
    expected = config_nw.TARGET_ROI_PCT / 100.0 / config_nw.LEVERAGE
    assert abs(config_nw.TP_PRICE_PCT - expected) < 1e-12


def test_derived_max_sl_price_pct():
    expected = config_nw.MAX_SL_ROI_PCT / 100.0 / config_nw.LEVERAGE
    assert abs(config_nw.MAX_SL_PRICE_PCT - expected) < 1e-12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config_nw.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'config_nw'`

- [ ] **Step 3: Implement `config_nw.py`**

```python
import os
from dotenv import load_dotenv

load_dotenv()

# ── Strategy: Nadaraya-Watson Kernel MTF (backtest research only) ────────
STRATEGY_NAME: str = os.getenv("NW_STRATEGY_NAME", "Nadaraya-Watson Kernel MTF (research)")

NW_TREND_TF: str = os.getenv("NW_TREND_TF", "4h")
NW_ENTRY_TF: str = os.getenv("NW_ENTRY_TF", "1h")

NW_TREND_KLINE_COUNT: int = int(os.getenv("NW_TREND_KLINE_COUNT", "260"))
NW_ENTRY_KLINE_COUNT: int = int(os.getenv("NW_ENTRY_KLINE_COUNT", "500"))

NW_TREND_EMA_PERIOD: int = int(os.getenv("NW_TREND_EMA_PERIOD", "200"))

# Nadaraya-Watson rational quadratic kernel params (Pine Script defaults).
NW_LOOKBACK_WINDOW: float = float(os.getenv("NW_LOOKBACK_WINDOW", "8.0"))       # Pine "h"
NW_RELATIVE_WEIGHTING: float = float(os.getenv("NW_RELATIVE_WEIGHTING", "8.0"))  # Pine "r"
# Bars summed per kernel estimate -- a numerical truncation bound, not a
# Pine parameter. Kernel weight decays fast with distance under the
# default h/r, so bars beyond this contribute negligibly (see
# tests/test_nw_kernel.py's truncation-accuracy test).
NW_KERNEL_SUM_WINDOW: int = int(os.getenv("NW_KERNEL_SUM_WINDOW", "300"))

# Structural swing lookup, used only to anchor the SL (see Task 5).
NW_SWING_FRACTAL_BARS: int = int(os.getenv("NW_SWING_FRACTAL_BARS", "5"))
NW_SWING_LOOKBACK_BARS: int = int(os.getenv("NW_SWING_LOOKBACK_BARS", "20"))

NW_RSI_PERIOD: int = int(os.getenv("NW_RSI_PERIOD", "14"))
NW_RSI_LONG_MIN: float = float(os.getenv("NW_RSI_LONG_MIN", "45"))
NW_RSI_LONG_MAX: float = float(os.getenv("NW_RSI_LONG_MAX", "75"))
NW_RSI_SHORT_MIN: float = float(os.getenv("NW_RSI_SHORT_MIN", "25"))
NW_RSI_SHORT_MAX: float = float(os.getenv("NW_RSI_SHORT_MAX", "55"))

NW_VOLUME_MA_PERIOD: int = int(os.getenv("NW_VOLUME_MA_PERIOD", "20"))
NW_MIN_VOLUME_MULTIPLIER: float = float(os.getenv("NW_MIN_VOLUME_MULTIPLIER", "1.0"))

NW_ATR_PERIOD: int = int(os.getenv("NW_ATR_PERIOD", "14"))
# Buffer beyond the structural swing level used to place the SL.
NW_SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("NW_SL_ATR_BUFFER_MULTIPLIER", "0.5"))

LEVERAGE: int = int(os.getenv("NW_LEVERAGE", "20"))
TARGET_ROI_PCT: float = float(os.getenv("NW_TARGET_ROI_PCT", "15.0"))
TP_PRICE_PCT: float = TARGET_ROI_PCT / 100.0 / LEVERAGE
MAX_SL_ROI_PCT: float = float(os.getenv("NW_MAX_SL_ROI_PCT", "20.0"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE
MIN_RR: float = float(os.getenv("NW_MIN_RR", "1.5"))

# ── BTC market safety filter (4H variant) ─────────────────────────────
NW_ENABLE_BTC_FILTER: bool = os.getenv("NW_ENABLE_BTC_FILTER", "true").lower() == "true"
NW_BTC_FILTER_SYMBOL: str = os.getenv("NW_BTC_FILTER_SYMBOL", "BTC_USDT")
NW_BTC_FILTER_TF: str = os.getenv("NW_BTC_FILTER_TF", "4h")
NW_BTC_MAX_OPPOSING_MOVE_PCT: float = float(os.getenv("NW_BTC_MAX_OPPOSING_MOVE_PCT", "0.20"))
NW_BTC_MAX_SINGLE_CANDLE_MOVE_PCT: float = float(os.getenv("NW_BTC_MAX_SINGLE_CANDLE_MOVE_PCT", "0.60"))
NW_BTC_MAX_THREE_CANDLE_MOVE_PCT: float = float(os.getenv("NW_BTC_MAX_THREE_CANDLE_MOVE_PCT", "1.20"))

# ── Backtest-only: outcome expiry + fee/slippage estimates ────────────
NW_SIGNAL_EXPIRE_HOURS: float = float(os.getenv("NW_SIGNAL_EXPIRE_HOURS", "48"))

ESTIMATED_ENTRY_FEE_PCT: float = float(os.getenv("NW_ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT: float = float(os.getenv("NW_ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT: float = float(os.getenv("NW_ESTIMATED_SLIPPAGE_PCT", "0.01"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config_nw.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add config_nw.py tests/test_config_nw.py
git commit -m "feat: add standalone config for Nadaraya-Watson kernel strategy"
```

---

### Task 3: Trend filter + structural swing detection (`nw_strategy.py` part A)

**Files:**
- Create: `nw_strategy.py` (this task starts the file)
- Create: `tests/nw_fixtures.py`
- Test: `tests/test_nw_strategy.py` (this task starts the file)

**Interfaces:**
- Consumes: `config_nw.py` (Task 2), `calculate_ema` from `strategy.py`.
- Produces: `_detect_trend_htf(df_4h: pd.DataFrame) -> str | None`,
  `find_swing_low(df: pd.DataFrame, end_idx: int, lookback: int, fractal_bars: int) -> float | None`,
  `find_swing_high(df: pd.DataFrame, end_idx: int, lookback: int, fractal_bars: int) -> float | None`.
  Later tasks (4-7) append to this same `nw_strategy.py` file.

- [ ] **Step 1: Write `tests/nw_fixtures.py`**

```python
"""
Deterministic OHLCV fixture builders for Nadaraya-Watson kernel strategy
tests.

Numeric constants here are reasoned, not hand-executed against pandas -- if
a test using these fails for the wrong reason, adjust the constants below
and re-run. That is expected TDD iteration, not a defect in the test itself
(same convention as tests/fib_fixtures.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_4h_trend_df(direction: str = "LONG", bars: int = 220, start_price: float = 100.0) -> pd.DataFrame:
    """Steadily trending, noiseless 4H series -- long enough for EMA200 to
    settle cleanly. Ends with one duplicated row so callers can `iloc[:-1]`
    to drop the "forming" candle."""
    idx = pd.date_range("2026-01-01", periods=bars, freq="4h")
    step = 0.15 if direction == "LONG" else -0.15
    closes = start_price + np.arange(bars) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_swing_fixture(kind: str = "low", fractal_bars: int = 5, pivot_value: float = 95.0) -> pd.DataFrame:
    """A short, deterministic 1H series with exactly one unambiguous
    fractal pivot at its center: `fractal_bars` strictly-descending bars
    into the pivot, then `fractal_bars` strictly-ascending bars out of it
    (mirrored for a swing high). No duplicated forming-candle row -- callers
    pass this directly (find_swing_low/high take an explicit end_idx)."""
    n = fractal_bars * 2 + 1
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    sign = 1.0 if kind == "low" else -1.0
    left = pivot_value + sign * np.arange(fractal_bars, 0, -1) * 1.0
    right = pivot_value + sign * np.arange(1, fractal_bars + 1) * 1.0
    closes = np.concatenate([left, [pivot_value], right])
    opens = closes.copy()
    highs = closes + 0.3
    lows = closes - 0.3
    if kind == "low":
        lows = closes.copy()
        lows[fractal_bars] = pivot_value
    else:
        highs = closes.copy()
        highs[fractal_bars] = pivot_value
    volumes = np.full(n, 1000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def make_1h_nw_df(
    direction: str = "LONG",
    pad_bars: int = 60,
    base_price: float = 100.0,
    dip_depth: float = 2.5,
    recovery_depth: float = 1.0,
    final_decline_depth: float = 1.0,
    bounce_size: float = 2.0,
    volume_mult: float = 1.8,
) -> pd.DataFrame:
    """
    LONG story (SHORT mirrors the sign of every price move):
      pad:            `pad_bars` flat bars at base_price (warmup only, and
                       keeps RSI's EWM average away from a 0/0 edge case).
      dip_down:       10 bars, monotonic decline base_price ->
                       base_price - dip_depth.
      dip_recover:    5 bars, monotonic rise back up to
                       base_price - dip_depth + recovery_depth -- confirms
                       an interior NW_SWING_FRACTAL_BARS-fractal low at the
                       dip bottom, findable by find_swing_low within
                       NW_SWING_LOOKBACK_BARS of the signal bar.
      final_decline:  10 bars, monotonic decline from the recovered level
                       down to a new, lower bottom -- guarantees the kernel
                       estimate is still declining into the last two
                       pre-signal bars (the "was_bearish" half of the
                       bullish slope-flip condition).
      signal bar:     1 bar, closes `bounce_size` above the final_decline
                       bottom, with a volume spike -- this is the bar
                       evaluate_symbol_nw/_detect_nw_signal check.

    Ends with one duplicated row so callers can `iloc[:-1]` to drop the
    "forming" candle -- the *second-to-last* row of the returned frame is
    the actual signal bar once that drop happens.

    Numeric constants here are reasoned, not hand-executed against pandas --
    if a test fails for the wrong reason (kernel flip / RSI / swing not
    landing as expected), adjust dip_depth/recovery_depth/
    final_decline_depth/bounce_size and re-run. Same convention as
    tests/fib_fixtures.py.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    total_bars = pad_bars + 10 + 5 + 10 + 1
    idx = pd.date_range("2026-01-01", periods=total_bars, freq="1h")

    pad = np.full(pad_bars, base_price)
    dip_bottom = base_price - sign * dip_depth
    dip_down = np.linspace(base_price, dip_bottom, 11)[1:]
    recovered_level = dip_bottom + sign * recovery_depth
    dip_recover = np.linspace(dip_bottom, recovered_level, 6)[1:]
    final_bottom = recovered_level - sign * final_decline_depth
    final_decline = np.linspace(recovered_level, final_bottom, 11)[1:]
    signal_close = final_bottom + sign * bounce_size

    closes = np.concatenate([pad, dip_down, dip_recover, final_decline, [signal_close]])
    opens = np.empty(len(closes))
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    highs = np.maximum(opens, closes) + 0.02
    lows = np.minimum(opens, closes) - 0.02

    volumes = np.full(len(closes), 1000.0)
    volumes[-1] = 1000.0 * volume_mult

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def patch_nw_klines(monkeypatch, nw_strategy_module, df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> None:
    """Route nw_strategy.get_market_klines(symbol, interval, count) to
    fixtures by interval."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        if interval == "4h":
            return df_4h
        if interval == "1h":
            return df_1h
        raise ValueError(f"unexpected interval {interval!r} in test")

    monkeypatch.setattr(nw_strategy_module, "get_market_klines", _fake)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_nw_strategy.py
from nw_strategy import _detect_trend_htf, find_swing_low, find_swing_high
from tests.nw_fixtures import make_4h_trend_df, make_swing_fixture


def test_detect_trend_htf_long_when_trending_up():
    df = make_4h_trend_df("LONG")
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) == "LONG"


def test_detect_trend_htf_short_when_trending_down():
    df = make_4h_trend_df("SHORT")
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) == "SHORT"


def test_detect_trend_htf_none_when_insufficient_history():
    df = make_4h_trend_df("LONG", bars=50)
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) is None


def test_find_swing_low_detects_known_pivot():
    df = make_swing_fixture(kind="low", fractal_bars=5, pivot_value=95.0)
    end_idx = len(df) - 1
    result = find_swing_low(df, end_idx, lookback=20, fractal_bars=5)
    assert result is not None
    assert abs(result - 95.0) < 1e-9


def test_find_swing_high_detects_known_pivot():
    df = make_swing_fixture(kind="high", fractal_bars=5, pivot_value=105.0)
    end_idx = len(df) - 1
    result = find_swing_high(df, end_idx, lookback=20, fractal_bars=5)
    assert result is not None
    assert abs(result - 105.0) < 1e-9


def test_find_swing_low_returns_none_when_no_fractal_in_lookback():
    # Monotonic decline -- no interior fractal low exists (the minimum is
    # always at the trailing edge, never confirmed by 5 higher bars after
    # it), so nothing should be found within a short lookback.
    import numpy as np
    import pandas as pd
    closes = 100.0 - np.arange(15) * 0.5
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": np.full(15, 1000.0),
    })
    result = find_swing_low(df, end_idx=14, lookback=8, fractal_bars=5)
    assert result is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_nw_strategy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nw_strategy'`

- [ ] **Step 4: Implement `nw_strategy.py` (part A)**

```python
"""
Nadaraya-Watson Kernel Multi-Timeframe Strategy (backtest research only).

4H trend (EMA200) gates direction; a Nadaraya-Watson rational-quadratic
kernel slope-flip on 1H (see nw_kernel.py, ported from the "Nadaraya-Watson:
Rational Quadratic Kernel (Non-Repainting)" Pine Script indicator by
jdehorty) confirms entry, alongside RSI + volume. TP is a fixed ROI-target
distance; SL is structural (swing level + ATR buffer, capped). Only
completed candles are ever used. Fully standalone from strategy.py -- see
docs/superpowers/specs/2026-07-16-nw-kernel-strategy-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import nw_kernel
import config_nw
from strategy import calculate_ema, calculate_rsi, calculate_atr
from market_data import get_market_klines

logger = logging.getLogger(__name__)


@dataclass
class NwSignal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    rr: float
    score: float
    generated_at: datetime


@dataclass
class BtcContext4h:
    close: float
    ema_200: float
    one_candle_move_pct: float
    three_candle_move_pct: float


# ── trend filter ────────────────────────────────────────────────────────

def _ema_slope_ok(ema: pd.Series, direction: str, tolerance: float = 1e-9) -> bool:
    current = ema.iloc[-1]
    three_bars_ago = ema.iloc[-4]
    if direction == "LONG":
        return current >= three_bars_ago - tolerance
    return current <= three_bars_ago + tolerance


def _detect_trend_htf(df_4h: pd.DataFrame) -> str | None:
    if len(df_4h) < config_nw.NW_TREND_EMA_PERIOD + 5:
        return None
    ema200 = calculate_ema(df_4h["close"], config_nw.NW_TREND_EMA_PERIOD)
    close = float(df_4h["close"].iloc[-1])

    if close > float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "LONG"):
        return "LONG"
    if close < float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "SHORT"):
        return "SHORT"
    return None


# ── structural swing detection (used to anchor the SL in Task 5) ────────

def find_swing_low(df: pd.DataFrame, end_idx: int, lookback: int, fractal_bars: int) -> float | None:
    """Most recent confirmed fractal swing low at or before `end_idx`,
    searching back `lookback` bars. A fractal low at position p requires
    low[p] <= every low in the `fractal_bars`-bar window on each side
    (tie-inclusive), and both sides of that window must already exist
    within [0, end_idx] -- i.e. only fully-confirmed pivots count."""
    lows = df["low"].to_numpy()
    hi = end_idx - fractal_bars
    lo = max(fractal_bars, end_idx - lookback)
    for p in range(hi, lo - 1, -1):
        if p - fractal_bars < 0:
            continue
        window = lows[p - fractal_bars: p + fractal_bars + 1]
        if lows[p] <= window.min():
            return float(lows[p])
    return None


def find_swing_high(df: pd.DataFrame, end_idx: int, lookback: int, fractal_bars: int) -> float | None:
    highs = df["high"].to_numpy()
    hi = end_idx - fractal_bars
    lo = max(fractal_bars, end_idx - lookback)
    for p in range(hi, lo - 1, -1):
        if p - fractal_bars < 0:
            continue
        window = highs[p - fractal_bars: p + fractal_bars + 1]
        if highs[p] >= window.max():
            return float(highs[p])
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_nw_strategy.py tests/test_nw_kernel.py tests/test_config_nw.py -v`
Expected: PASS. If `test_find_swing_low_detects_known_pivot` /
`test_find_swing_high_detects_known_pivot` or the `make_1h_nw_df`-dependent
tests added in later tasks fail because the fixture's numeric constants
don't land exactly as reasoned above, adjust the fixture constants (not the
production code) and re-run -- expected TDD iteration per the fixture's own
docstring.

- [ ] **Step 6: Commit**

```bash
git add nw_strategy.py tests/nw_fixtures.py tests/test_nw_strategy.py
git commit -m "feat: add NW strategy trend filter + structural swing detection"
```

---

### Task 4: Kernel-flip entry signal + RSI/volume confirmation (`nw_strategy.py` part B)

**Files:**
- Modify: `nw_strategy.py` (append)
- Modify: `tests/test_nw_strategy.py` (append)

**Interfaces:**
- Consumes: `nw_kernel.nw_estimate`/`detect_slope_flip` (Task 1),
  `find_swing_low`/`find_swing_high` (Task 3), `calculate_rsi`/`calculate_atr`
  from `strategy.py`.
- Produces: `_detect_nw_signal(df_1h: pd.DataFrame, direction: str) -> tuple[bool, str, dict]`.
  The `details` dict (on success) carries keys `close`, `rsi`, `atr`,
  `volume_ratio`, `swing_level` -- consumed by Task 5's TP/SL calculation
  and Task 7's scoring.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_nw_strategy.py
from nw_strategy import _detect_nw_signal
from tests.nw_fixtures import make_1h_nw_df


def test_detect_nw_signal_long_fires_on_bullish_flip_with_confirmation():
    df = make_1h_nw_df("LONG")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_nw_signal(closed, "LONG")
    assert ok is True, reason
    assert details["close"] == closed["close"].iloc[-1]
    assert details["swing_level"] is not None
    assert 0.0 <= details["rsi"] <= 100.0


def test_detect_nw_signal_short_fires_on_bearish_flip_with_confirmation():
    df = make_1h_nw_df("SHORT")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_nw_signal(closed, "SHORT")
    assert ok is True, reason


def test_detect_nw_signal_rejects_when_no_flip_on_latest_bar():
    # A pure, unbroken decline never flips -- the latest bar is still
    # falling, not turning up.
    import numpy as np
    import pandas as pd
    closes = 100.0 - np.arange(80) * 0.05
    df = pd.DataFrame({
        "open": closes + 0.01, "high": closes + 0.05, "low": closes - 0.05,
        "close": closes, "volume": np.full(80, 1000.0),
    })
    ok, reason, _ = _detect_nw_signal(df, "LONG")
    assert ok is False
    assert "flip" in reason


def test_detect_nw_signal_rejects_low_volume():
    df = make_1h_nw_df("LONG", volume_mult=0.5)
    closed = df.iloc[:-1]
    ok, reason, _ = _detect_nw_signal(closed, "LONG")
    assert ok is False
    assert "volume" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nw_strategy.py -v -k detect_nw_signal`
Expected: FAIL with `ImportError: cannot import name '_detect_nw_signal'`

- [ ] **Step 3: Append `_detect_nw_signal` to `nw_strategy.py`**

```python
# append to nw_strategy.py

# ── kernel-flip entry signal + confirmation ──────────────────────────────

def _detect_nw_signal(df_1h: pd.DataFrame, direction: str) -> tuple[bool, str, dict]:
    close_arr = df_1h["close"].to_numpy(dtype=float)
    yhat = nw_kernel.nw_estimate(
        close_arr, config_nw.NW_LOOKBACK_WINDOW, config_nw.NW_RELATIVE_WEIGHTING,
        config_nw.NW_KERNEL_SUM_WINDOW,
    )
    flips = nw_kernel.detect_slope_flip(yhat)

    expected_flip = "bullish" if direction == "LONG" else "bearish"
    if flips.iloc[-1] != expected_flip:
        return False, "no kernel slope flip on latest candle", {}

    rsi = calculate_rsi(df_1h["close"], config_nw.NW_RSI_PERIOD)
    atr = calculate_atr(df_1h, config_nw.NW_ATR_PERIOD)

    rsi_last = float(rsi.iloc[-1])
    atr_last = float(atr.iloc[-1])
    close = float(df_1h["close"].iloc[-1])
    vol_last = float(df_1h["volume"].iloc[-1])
    vol_avg = float(df_1h["volume"].iloc[-(config_nw.NW_VOLUME_MA_PERIOD + 1):-1].mean())

    rsi_min, rsi_max = (
        (config_nw.NW_RSI_LONG_MIN, config_nw.NW_RSI_LONG_MAX) if direction == "LONG"
        else (config_nw.NW_RSI_SHORT_MIN, config_nw.NW_RSI_SHORT_MAX)
    )
    if not (rsi_min <= rsi_last <= rsi_max):
        return False, f"RSI {rsi_last:.1f} outside {direction.lower()} range", {}

    if vol_avg <= 0 or not (vol_last >= config_nw.NW_MIN_VOLUME_MULTIPLIER * vol_avg):
        ratio = (vol_last / vol_avg) if vol_avg else 0.0
        return False, f"volume ratio {ratio:.2f} below {config_nw.NW_MIN_VOLUME_MULTIPLIER}", {}

    end_idx = len(df_1h) - 1
    if direction == "LONG":
        swing_level = find_swing_low(
            df_1h, end_idx, config_nw.NW_SWING_LOOKBACK_BARS, config_nw.NW_SWING_FRACTAL_BARS,
        )
    else:
        swing_level = find_swing_high(
            df_1h, end_idx, config_nw.NW_SWING_LOOKBACK_BARS, config_nw.NW_SWING_FRACTAL_BARS,
        )
    if swing_level is None:
        return False, "no structural swing found for stop placement", {}

    details = {
        "close": close,
        "rsi": rsi_last,
        "atr": atr_last,
        "volume_ratio": vol_last / vol_avg if vol_avg else 0.0,
        "swing_level": swing_level,
    }
    return True, "", details
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_nw_strategy.py -v -k detect_nw_signal`
Expected: PASS. If the `make_1h_nw_df`-driven success tests fail because
RSI/volume/swing don't land as reasoned (e.g. RSI outside the 45-75/25-55
band, or no swing found within the lookback), adjust `dip_depth` /
`recovery_depth` / `final_decline_depth` / `bounce_size` in
`tests/nw_fixtures.py` and re-run -- this is expected TDD iteration per the
fixture's own docstring, not a production-code defect.

- [ ] **Step 5: Commit**

```bash
git add nw_strategy.py tests/test_nw_strategy.py
git commit -m "feat: add NW kernel-flip entry signal with RSI/volume confirmation"
```

---

### Task 5: Trade geometry, TP/SL, RR (`nw_strategy.py` part C)

**Files:**
- Modify: `nw_strategy.py` (append)
- Modify: `tests/test_nw_strategy.py` (append)

**Interfaces:**
- Consumes: `details` dict produced by `_detect_nw_signal` (Task 4).
- Produces: `_valid_trade_geometry(direction, entry, tp, sl) -> bool`,
  `_calc_rr(entry, tp, sl) -> float`,
  `_calculate_tp_sl_nw(direction: str, entry: float, details: dict) -> tuple[float, float] | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_nw_strategy.py
from nw_strategy import _valid_trade_geometry, _calc_rr, _calculate_tp_sl_nw
import config_nw


def test_valid_trade_geometry_long():
    assert _valid_trade_geometry("LONG", entry=100.0, tp=101.0, sl=99.0) is True
    assert _valid_trade_geometry("LONG", entry=100.0, tp=99.0, sl=101.0) is False


def test_valid_trade_geometry_short():
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=99.0, sl=101.0) is True
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=101.0, sl=99.0) is False


def test_calc_rr():
    assert abs(_calc_rr(entry=100.0, tp=102.0, sl=99.0) - 2.0) < 1e-9


def test_calculate_tp_sl_nw_long_fixed_tp_structural_sl():
    entry = 100.0
    details = {"atr": 0.20, "swing_level": 98.5}  # swing 1.5 below entry
    result = _calculate_tp_sl_nw("LONG", entry, details)
    assert result is not None
    tp, sl = result
    expected_tp = entry * (1.0 + config_nw.TP_PRICE_PCT)
    assert abs(tp - expected_tp) < 1e-9
    # raw sl distance = entry - (swing_level - atr*buffer)
    #                 = 100 - (98.5 - 0.20*0.5) = 100 - 98.4 = 1.6
    raw_distance = entry - (98.5 - 0.20 * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER)
    expected_distance = min(raw_distance, entry * config_nw.MAX_SL_PRICE_PCT)
    assert abs((entry - sl) - expected_distance) < 1e-9


def test_calculate_tp_sl_nw_short_fixed_tp_structural_sl():
    entry = 100.0
    details = {"atr": 0.20, "swing_level": 101.5}
    result = _calculate_tp_sl_nw("SHORT", entry, details)
    assert result is not None
    tp, sl = result
    expected_tp = entry * (1.0 - config_nw.TP_PRICE_PCT)
    assert abs(tp - expected_tp) < 1e-9
    raw_distance = (101.5 + 0.20 * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER) - entry
    expected_distance = min(raw_distance, entry * config_nw.MAX_SL_PRICE_PCT)
    assert abs((sl - entry) - expected_distance) < 1e-9


def test_calculate_tp_sl_nw_caps_at_max_sl_when_swing_far_away():
    entry = 100.0
    # Swing level absurdly far away -- raw distance must be capped, not
    # left uncapped or rejected.
    details = {"atr": 0.20, "swing_level": 50.0}
    result = _calculate_tp_sl_nw("LONG", entry, details)
    assert result is not None
    tp, sl = result
    assert abs((entry - sl) - entry * config_nw.MAX_SL_PRICE_PCT) < 1e-9


def test_calculate_tp_sl_nw_rejects_when_swing_on_wrong_side_of_entry():
    entry = 100.0
    # LONG's swing_level (even after the ATR buffer) sits above entry --
    # not a valid structural stop.
    details = {"atr": 0.20, "swing_level": 100.5}
    result = _calculate_tp_sl_nw("LONG", entry, details)
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nw_strategy.py -v -k "geometry or calc_rr or tp_sl"`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Append to `nw_strategy.py`**

```python
# append to nw_strategy.py

# ── trade geometry, TP/SL, RR ────────────────────────────────────────────

def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    return tp < entry < sl


def _calc_rr(entry: float, tp: float, sl: float) -> float:
    reward = abs(tp - entry)
    risk = abs(entry - sl)
    return reward / risk if risk > 0 else 0.0


def _calculate_tp_sl_nw(direction: str, entry: float, details: dict) -> tuple[float, float] | None:
    """TP is a fixed ROI-target distance. SL is structural -- the swing
    level found by _detect_nw_signal, pushed out by an ATR buffer -- with
    its price distance from entry capped (not rejected) at
    MAX_SL_PRICE_PCT. This intentionally mirrors the original live bot's
    cap-not-reject SL model rather than the fib strategy's later
    reject-if-too-wide redesign (see this plan's Global Constraints)."""
    atr_last = details["atr"]
    swing_level = details["swing_level"]

    if direction == "LONG":
        tp = entry * (1.0 + config_nw.TP_PRICE_PCT)
        raw_sl_distance = entry - (swing_level - atr_last * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER)
        if raw_sl_distance <= 0:
            return None
        sl_distance = min(raw_sl_distance, entry * config_nw.MAX_SL_PRICE_PCT)
        sl = entry - sl_distance
    else:
        tp = entry * (1.0 - config_nw.TP_PRICE_PCT)
        raw_sl_distance = (swing_level + atr_last * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER) - entry
        if raw_sl_distance <= 0:
            return None
        sl_distance = min(raw_sl_distance, entry * config_nw.MAX_SL_PRICE_PCT)
        sl = entry + sl_distance

    return tp, sl
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_nw_strategy.py -v -k "geometry or calc_rr or tp_sl"`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add nw_strategy.py tests/test_nw_strategy.py
git commit -m "feat: add NW strategy TP/SL, trade geometry, RR calculation"
```

---

### Task 6: BTC 4H market-safety filter (`nw_strategy.py` part D)

**Files:**
- Modify: `nw_strategy.py` (append)
- Modify: `tests/test_nw_strategy.py` (append)

**Interfaces:**
- Produces: `build_btc_context_4h() -> BtcContext4h | None`,
  `_btc_filter_ok_4h(direction: str, btc: BtcContext4h) -> tuple[bool, str]`.
  Identical shape to `fib_strategy.py`'s BTC filter, reimplemented locally
  (no cross-import) per the isolation constraint.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_nw_strategy.py
from nw_strategy import BtcContext4h, _btc_filter_ok_4h


def test_btc_filter_ok_long_when_btc_bullish():
    btc = BtcContext4h(close=105.0, ema_200=100.0, one_candle_move_pct=0.1, three_candle_move_pct=0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is True, reason


def test_btc_filter_blocks_long_when_btc_bearish():
    btc = BtcContext4h(close=95.0, ema_200=100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is False
    assert "bearish" in reason


def test_btc_filter_blocks_on_extreme_single_candle_volatility():
    btc = BtcContext4h(close=105.0, ema_200=100.0, one_candle_move_pct=0.9, three_candle_move_pct=0.3)
    ok, reason = _btc_filter_ok_4h("LONG", btc)
    assert ok is False
    assert "volatility" in reason


def test_btc_filter_ok_short_when_btc_bearish():
    btc = BtcContext4h(close=95.0, ema_200=100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)
    ok, reason = _btc_filter_ok_4h("SHORT", btc)
    assert ok is True, reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nw_strategy.py -v -k btc_filter`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Append to `nw_strategy.py`**

```python
# append to nw_strategy.py

# ── BTC 4H market-safety filter ──────────────────────────────────────────

def build_btc_context_4h() -> BtcContext4h | None:
    df = get_market_klines(config_nw.NW_BTC_FILTER_SYMBOL, config_nw.NW_BTC_FILTER_TF, count=config_nw.NW_TREND_KLINE_COUNT)
    if df is None or df.empty:
        return None
    closed = df.iloc[:-1].copy()
    if len(closed) < config_nw.NW_TREND_EMA_PERIOD + 5:
        return None

    ema200 = calculate_ema(closed["close"], config_nw.NW_TREND_EMA_PERIOD)

    latest_close = float(closed["close"].iloc[-1])
    previous_close = float(closed["close"].iloc[-2])
    close_three_bars_ago = float(closed["close"].iloc[-4])

    one_candle_move_pct = (latest_close - previous_close) / previous_close * 100.0
    three_candle_move_pct = (latest_close - close_three_bars_ago) / close_three_bars_ago * 100.0

    return BtcContext4h(
        close=latest_close,
        ema_200=float(ema200.iloc[-1]),
        one_candle_move_pct=one_candle_move_pct,
        three_candle_move_pct=three_candle_move_pct,
    )


def _btc_filter_ok_4h(direction: str, btc: BtcContext4h) -> tuple[bool, str]:
    if abs(btc.one_candle_move_pct) > config_nw.NW_BTC_MAX_SINGLE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"
    if abs(btc.three_candle_move_pct) > config_nw.NW_BTC_MAX_THREE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"

    if direction == "LONG":
        if not (
            btc.close > btc.ema_200
            and btc.three_candle_move_pct >= -config_nw.NW_BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bearish trend"
    else:
        if not (
            btc.close < btc.ema_200
            and btc.three_candle_move_pct <= config_nw.NW_BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bullish trend"

    return True, ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_nw_strategy.py -v -k btc_filter`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add nw_strategy.py tests/test_nw_strategy.py
git commit -m "feat: add NW strategy BTC 4H market-safety filter"
```

---

### Task 7: Scoring + full integration (`nw_strategy.py` part E)

**Files:**
- Modify: `nw_strategy.py` (append, completes the file)
- Modify: `tests/test_nw_strategy.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 3-6.
- Produces: `_score_candidate_nw(direction: str, details: dict, rr: float) -> float`,
  `_reason_bucket(reason: str) -> str`, `_bump(reject_sink: dict | None, key: str) -> None`,
  and the public entry point:
  `evaluate_symbol_nw(symbol: str, btc_context: BtcContext4h | None = None, reject_sink: dict | None = None) -> NwSignal | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_nw_strategy.py
import nw_strategy
from nw_strategy import evaluate_symbol_nw, BtcContext4h as _BtcCtx
from tests.nw_fixtures import patch_nw_klines


def test_evaluate_symbol_nw_returns_signal_for_valid_long_setup(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_nw_df("LONG")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)
    monkeypatch.setattr(nw_strategy.config_nw, "NW_ENABLE_BTC_FILTER", False)

    sig = evaluate_symbol_nw("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= nw_strategy.config_nw.MIN_RR


def test_evaluate_symbol_nw_returns_signal_for_valid_short_setup(monkeypatch):
    df_4h = make_4h_trend_df("SHORT")
    df_1h = make_1h_nw_df("SHORT")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)
    monkeypatch.setattr(nw_strategy.config_nw, "NW_ENABLE_BTC_FILTER", False)

    sig = evaluate_symbol_nw("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price


def test_evaluate_symbol_nw_returns_none_when_no_htf_trend(monkeypatch):
    # Flat 4H series -- close hovers at EMA200, no clear trend.
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2026-01-01", periods=220, freq="4h")
    closes = np.full(220, 100.0)
    df_4h = pd.DataFrame({
        "open": closes, "high": closes + 0.01, "low": closes - 0.01,
        "close": closes, "volume": np.full(220, 1000.0),
    })
    df_4h = pd.concat([df_4h, df_4h.iloc[[-1]]])
    df_1h = make_1h_nw_df("LONG")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)

    reject_sink = {}
    sig = evaluate_symbol_nw("TEST_USDT", reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("no_trend", 0) >= 1


def test_evaluate_symbol_nw_uses_reject_sink_for_btc_filter(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_nw_df("LONG")
    patch_nw_klines(monkeypatch, nw_strategy, df_4h, df_1h)
    monkeypatch.setattr(nw_strategy.config_nw, "NW_ENABLE_BTC_FILTER", True)

    bearish_btc = _BtcCtx(close=95.0, ema_200=100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)
    reject_sink = {}
    sig = evaluate_symbol_nw("TEST_USDT", btc_context=bearish_btc, reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("btc_filter", 0) >= 1


def test_score_candidate_nw_within_bounds():
    details = {"rsi": 60.0, "volume_ratio": 1.5}
    score = nw_strategy._score_candidate_nw("LONG", details, rr=2.0)
    assert 0.0 <= score <= 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_nw_strategy.py -v -k "evaluate_symbol_nw or score_candidate"`
Expected: FAIL with `ImportError` / `AttributeError`

- [ ] **Step 3: Append to `nw_strategy.py`**

```python
# append to nw_strategy.py

# ── scoring ───────────────────────────────────────────────────────────

def _score_candidate_nw(direction: str, details: dict, rr: float) -> float:
    score = 30.0  # 4H trend + kernel-flip alignment -- already gated true/false upstream

    rsi = details["rsi"]
    ideal_lo, ideal_hi = (55.0, 65.0) if direction == "LONG" else (35.0, 45.0)
    if ideal_lo <= rsi <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi - ideal_lo), abs(rsi - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 25.0 * rsi_quality

    vol_ratio = details["volume_ratio"]
    if config_nw.NW_MIN_VOLUME_MULTIPLIER < 2.0:
        vol_quality = min(1.0, max(0.0, (vol_ratio - config_nw.NW_MIN_VOLUME_MULTIPLIER) / (2.0 - config_nw.NW_MIN_VOLUME_MULTIPLIER)))
    else:
        vol_quality = 1.0 if vol_ratio >= config_nw.NW_MIN_VOLUME_MULTIPLIER else 0.0
    score += 20.0 * vol_quality

    if config_nw.MIN_RR < 2.0:
        rr_quality = min(1.0, max(0.0, (rr - config_nw.MIN_RR) / (2.0 - config_nw.MIN_RR)))
    else:
        rr_quality = 1.0 if rr >= config_nw.MIN_RR else 0.0
    score += 25.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)


# ── public entry point ─────────────────────────────────────────────────

def _reason_bucket(reason: str) -> str:
    if reason == "no kernel slope flip on latest candle":
        return "no_flip"
    if "no structural swing" in reason:
        return "no_swing"
    if reason.startswith("RSI"):
        return "rsi_out_of_band"
    if reason.startswith("volume ratio"):
        return "low_volume"
    return "nw_other"


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def evaluate_symbol_nw(
    symbol: str,
    btc_context: "BtcContext4h | None" = None,
    reject_sink: dict | None = None,
) -> NwSignal | None:
    try:
        raw_4h = get_market_klines(symbol, config_nw.NW_TREND_TF, count=config_nw.NW_TREND_KLINE_COUNT)
        raw_1h = get_market_klines(symbol, config_nw.NW_ENTRY_TF, count=config_nw.NW_ENTRY_KLINE_COUNT)

        if raw_4h is None or raw_4h.empty or raw_1h is None or raw_1h.empty:
            logger.debug("[NW-REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed_4h = raw_4h.iloc[:-1].copy()
        closed_1h = raw_1h.iloc[:-1].copy()

        if len(closed_4h) < config_nw.NW_TREND_EMA_PERIOD + 5:
            _bump(reject_sink, "insufficient_history")
            return None
        min_1h_needed = config_nw.NW_SWING_LOOKBACK_BARS + config_nw.NW_SWING_FRACTAL_BARS + config_nw.NW_VOLUME_MA_PERIOD + 10
        if len(closed_1h) < min_1h_needed:
            _bump(reject_sink, "insufficient_history")
            return None

        direction = _detect_trend_htf(closed_4h)
        if direction is None:
            logger.debug("[NW-REJECT] %s no 4H trend", symbol)
            _bump(reject_sink, "no_trend")
            return None

        ok, reason, details = _detect_nw_signal(closed_1h, direction)
        if not ok:
            logger.debug("[NW-REJECT] %s %s", symbol, reason)
            _bump(reject_sink, _reason_bucket(reason))
            return None

        if config_nw.NW_ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context_4h()
            if ctx is None:
                _bump(reject_sink, "btc_context_unavailable")
                return None
            btc_ok, btc_reason = _btc_filter_ok_4h(direction, ctx)
            if not btc_ok:
                logger.debug("[NW-REJECT] %s %s %s", symbol, direction, btc_reason)
                _bump(reject_sink, "btc_filter")
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl_nw(direction, entry, details)
        if tp_sl is None:
            _bump(reject_sink, "invalid_structural_stop")
            return None
        tp, sl = tp_sl

        if not _valid_trade_geometry(direction, entry, tp, sl):
            _bump(reject_sink, "invalid_geometry")
            return None

        rr = _calc_rr(entry, tp, sl)
        if rr < config_nw.MIN_RR:
            _bump(reject_sink, "rr_below_min")
            return None

        score = _score_candidate_nw(direction, details, rr)

        logger.info(
            "[NW-CANDIDATE] %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
            symbol, direction, score, entry, tp, sl, rr,
        )

        return NwSignal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            leverage=config_nw.LEVERAGE,
            rr=round(rr, 2),
            score=score,
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        logger.error("[NW-EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
```

- [ ] **Step 4: Run the full strategy test suite**

Run: `python -m pytest tests/test_nw_kernel.py tests/test_config_nw.py tests/test_nw_strategy.py -v`
Expected: PASS, all tests. If the integration tests fail because
`make_1h_nw_df`'s reasoned constants don't produce a passing RR/geometry
combination (e.g. RR lands just under `MIN_RR`), adjust the fixture's
`dip_depth`/`recovery_depth`/`final_decline_depth`/`bounce_size` and re-run
-- expected TDD iteration, not a production-code defect.

- [ ] **Step 5: Commit**

```bash
git add nw_strategy.py tests/test_nw_strategy.py
git commit -m "feat: add NW strategy scoring and evaluate_symbol_nw integration"
```

---

### Task 8: Backtest harness (`scripts/backtest_nw_strategy.py`)

**Files:**
- Create: `scripts/backtest_nw_strategy.py`

**Interfaces:**
- Consumes: `nw_strategy.evaluate_symbol_nw` (Task 7), `config_nw` (Task 2),
  `get_klines_extended` from `scripts/backtest_simple_strategy.py` (already
  present, unmodified, shared with the retired fib strategy's harness).
- Produces: a runnable CLI script, `python scripts/backtest_nw_strategy.py
  --symbols SYM1 SYM2 --days 60 --workers 6`.

This task has no unit tests of its own (same as
`scripts/backtest_fib_strategy.py` in the prior plan) -- it is validated by
actually running it, which happens in the manual tuning phase after this
plan completes.

- [ ] **Step 1: Implement `scripts/backtest_nw_strategy.py`**

```python
"""
Backtest utility for the Nadaraya-Watson Kernel Multi-Timeframe strategy.

Walks 1H candles forward in time, at each completed bar building an
"as-of" view (all 4H/1H/BTC candles up to and including that bar, plus a
duplicated last row standing in for the not-yet-formed candle) and calling
nw_strategy.evaluate_symbol_nw against it -- the exact same function a live
runtime would use, so backtest and any future live wiring share one source
of truth.

Reuses get_klines_extended's start/end pagination directly from
scripts/backtest_simple_strategy.py rather than duplicating it (same
pattern as the retired scripts/backtest_fib_strategy.py).
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import nw_strategy
import config_nw
from backtest_simple_strategy import get_klines_extended

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    rr: float
    outcome: str            # "win" | "loss" | "expired"
    gross_roi_pct: float
    net_roi_pct: float


@dataclass
class BacktestStats:
    trades: list[Trade] = field(default_factory=list)

    def add(self, trade: Trade) -> None:
        self.trades.append(trade)

    def print_report(self) -> None:
        n = len(self.trades)
        print(f"Total trades:        {n}")
        if n == 0:
            print("No trades generated -- nothing further to report.")
            return

        wins = [t for t in self.trades if t.outcome == "win"]
        losses = [t for t in self.trades if t.outcome == "loss"]
        expired = [t for t in self.trades if t.outcome == "expired"]

        win_rate = len(wins) / n * 100.0
        gross_roi = sum(t.gross_roi_pct for t in self.trades)
        total_fees = sum(t.gross_roi_pct - t.net_roi_pct for t in self.trades)
        net_roi = sum(t.net_roi_pct for t in self.trades)
        avg_roi = net_roi / n

        consecutive = max_consecutive = 0
        running = peak = 0.0
        max_drawdown = 0.0
        for t in self.trades:
            if t.outcome == "loss":
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
            else:
                consecutive = 0
            running += t.net_roi_pct
            peak = max(peak, running)
            max_drawdown = min(max_drawdown, running - peak)

        avg_rr = sum(t.rr for t in self.trades) / n

        longs = [t for t in self.trades if t.direction == "LONG"]
        shorts = [t for t in self.trades if t.direction == "SHORT"]

        print(f"Wins:                {len(wins)}")
        print(f"Losses:              {len(losses)}")
        print(f"Expired trades:      {len(expired)}")
        print(f"Win rate:            {win_rate:.1f}%")
        print(f"Gross ROI:           {gross_roi:+.1f}%")
        print(f"Estimated fees:      {total_fees:.1f}%")
        print(f"Net ROI:             {net_roi:+.1f}%")
        print(f"Average ROI/trade:   {avg_roi:+.2f}%")
        print(f"Max consecutive losses: {max_consecutive}")
        print(f"Max drawdown:        {max_drawdown:.1f}%")
        print(f"Average RR:          {avg_rr:.2f}")

        def _bucket_report(label: str, bucket: list[Trade]) -> None:
            if not bucket:
                print(f"{label} performance:  no trades")
                return
            bwins = sum(1 for t in bucket if t.outcome == "win")
            print(
                f"{label} performance:  {len(bucket)} trades, "
                f"{bwins}/{len(bucket)} wins ({bwins / len(bucket) * 100:.1f}%), "
                f"net ROI {sum(t.net_roi_pct for t in bucket):+.1f}%"
            )

        _bucket_report("LONG", longs)
        _bucket_report("SHORT", shorts)

        print("\nPerformance by symbol:")
        for symbol in sorted({t.symbol for t in self.trades}):
            _bucket_report(f"  {symbol}", [t for t in self.trades if t.symbol == symbol])


def _with_forming_row(df: pd.DataFrame, upto_idx: int, window_count: int) -> pd.DataFrame:
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start: upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def _find_as_of_index(df: pd.DataFrame, timestamp) -> int | None:
    eligible = df.index[df.index <= timestamp]
    if len(eligible) == 0:
        return None
    return int(df.index.get_loc(eligible[-1]))


def _simulate_outcome(
    direction: str, entry: float, tp: float, sl: float,
    df_1h: pd.DataFrame, entry_idx: int,
) -> tuple[str, int]:
    max_bars = int(config_nw.NW_SIGNAL_EXPIRE_HOURS)  # 1 bar == 1 hour on the 1H entry TF
    for offset in range(1, max_bars + 1):
        idx = entry_idx + offset
        if idx >= len(df_1h):
            return "expired", offset
        high = float(df_1h["high"].iloc[idx])
        low = float(df_1h["low"].iloc[idx])

        hit_sl = (low <= sl) if direction == "LONG" else (high >= sl)
        if hit_sl:
            return "loss", offset
        hit_tp = (high >= tp) if direction == "LONG" else (low <= tp)
        if hit_tp:
            return "win", offset

    return "expired", max_bars


def _roi_with_costs(direction: str, entry: float, exit_price: float, outcome: str) -> tuple[float, float]:
    if direction == "LONG":
        price_move_pct = (exit_price - entry) / entry * 100.0
    else:
        price_move_pct = (entry - exit_price) / entry * 100.0

    gross_roi = price_move_pct * config_nw.LEVERAGE
    cost_pct = (
        config_nw.ESTIMATED_ENTRY_FEE_PCT + config_nw.ESTIMATED_EXIT_FEE_PCT + config_nw.ESTIMATED_SLIPPAGE_PCT
    ) * config_nw.LEVERAGE
    net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi
    return round(gross_roi, 3), round(net_roi, 3)


def backtest_symbol(symbol: str, days: int, df_btc_full: pd.DataFrame) -> list[Trade]:
    """Runs in its own worker process -- returns this symbol's trades rather
    than mutating shared state, since process pool workers don't share
    memory."""
    trades: list[Trade] = []

    df_4h_full = get_klines_extended(symbol, config_nw.NW_TREND_TF, days)
    df_1h_full = get_klines_extended(symbol, config_nw.NW_ENTRY_TF, days)

    if df_4h_full.empty or df_1h_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_4h_full)} x {config_nw.NW_TREND_TF} bars, "
        f"{len(df_1h_full)} x {config_nw.NW_ENTRY_TF} bars",
        flush=True,
    )

    min_start = max(
        config_nw.NW_TREND_EMA_PERIOD + 5,
        config_nw.NW_SWING_LOOKBACK_BARS + config_nw.NW_SWING_FRACTAL_BARS + config_nw.NW_VOLUME_MA_PERIOD + 15,
    )
    in_trade_until_idx = -1

    original_get_market_klines = nw_strategy.get_market_klines
    candle_minutes = _TF_MINUTES[config_nw.NW_ENTRY_TF]
    trend_tf_minutes = _TF_MINUTES[config_nw.NW_TREND_TF]
    btc_tf_minutes = _TF_MINUTES[config_nw.NW_BTC_FILTER_TF]

    try:
        for i in range(min_start, len(df_1h_full) - 1):
            if i <= in_trade_until_idx:
                continue

            ts = df_1h_full.index[i]
            eval_time = ts + pd.Timedelta(minutes=candle_minutes)
            trend_idx = _find_as_of_index(df_4h_full, eval_time - pd.Timedelta(minutes=trend_tf_minutes))
            btc_idx = (
                _find_as_of_index(df_btc_full, eval_time - pd.Timedelta(minutes=btc_tf_minutes))
                if not df_btc_full.empty else None
            )
            if trend_idx is None or trend_idx < config_nw.NW_TREND_EMA_PERIOD + 5:
                continue

            as_of_1h = _with_forming_row(df_1h_full, i, config_nw.NW_ENTRY_KLINE_COUNT)
            as_of_4h = _with_forming_row(df_4h_full, trend_idx, config_nw.NW_TREND_KLINE_COUNT)
            as_of_btc = _with_forming_row(df_btc_full, btc_idx, config_nw.NW_TREND_KLINE_COUNT) if btc_idx is not None else None

            def _fake(sym: str, interval: str, count: int = 100, _1h=as_of_1h, _4h=as_of_4h, _btc=as_of_btc):
                if sym == config_nw.NW_BTC_FILTER_SYMBOL and interval == config_nw.NW_BTC_FILTER_TF:
                    return _btc if _btc is not None else pd.DataFrame()
                if interval == config_nw.NW_ENTRY_TF:
                    return _1h
                if interval == config_nw.NW_TREND_TF:
                    return _4h
                return pd.DataFrame()

            nw_strategy.get_market_klines = _fake

            sig = nw_strategy.evaluate_symbol_nw(symbol)

            if sig is None:
                continue

            outcome, bars_held = _simulate_outcome(
                sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, df_1h_full, i,
            )
            exit_price = sig.tp_price if outcome == "win" else (
                sig.sl_price if outcome == "loss" else float(df_1h_full["close"].iloc[min(i + bars_held, len(df_1h_full) - 1)])
            )
            gross_roi, net_roi = _roi_with_costs(sig.direction, sig.entry_price, exit_price, outcome)

            trades.append(Trade(
                symbol=symbol, direction=sig.direction, entry_price=sig.entry_price,
                tp_price=sig.tp_price, sl_price=sig.sl_price, rr=sig.rr,
                outcome=outcome, gross_roi_pct=gross_roi, net_roi_pct=net_roi,
            ))

            in_trade_until_idx = i + bars_held
    finally:
        nw_strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Nadaraya-Watson Kernel MTF Strategy")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=60, help="requested lookback in days (best-effort, paginated via start/end)")
    parser.add_argument("--workers", type=int, default=6, help="parallel worker processes, one symbol each")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- paginated via MEXC start/end)")

    print(f"[{config_nw.NW_BTC_FILTER_SYMBOL}] fetching shared BTC context ({config_nw.NW_BTC_FILTER_TF})...")
    df_btc_full = get_klines_extended(config_nw.NW_BTC_FILTER_SYMBOL, config_nw.NW_BTC_FILTER_TF, args.days)
    print(f"[{config_nw.NW_BTC_FILTER_SYMBOL}] achieved history: {len(df_btc_full)} x {config_nw.NW_BTC_FILTER_TF} bars")

    stats = BacktestStats()
    total_days_covered = args.days
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(backtest_symbol, symbol, args.days, df_btc_full): symbol
            for symbol in args.symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                for trade in future.result():
                    stats.add(trade)
            except Exception as e:
                print(f"[{symbol}] FAILED: {e}", flush=True)

    print("\n" + "=" * 60)
    stats.print_report()
    if stats.trades:
        trades_per_day = len(stats.trades) / total_days_covered
        print(f"Average trades/day (all symbols combined): {trades_per_day:.2f}")


if __name__ == "__main__":
    sys.exit(main() or 0)
```

- [ ] **Step 2: Smoke-test the harness on one symbol, short window**

Run: `python scripts/backtest_nw_strategy.py --symbols BTC_USDT --days 10 --workers 1`
Expected: runs to completion without a traceback and prints a report
(0 trades is an acceptable smoke-test outcome — this step only verifies the
harness itself runs; a longer, wider-pool backtest happens in the manual
tuning phase after this plan completes).

- [ ] **Step 3: Commit**

```bash
git add scripts/backtest_nw_strategy.py
git commit -m "feat: add backtest harness for Nadaraya-Watson kernel strategy"
```

---

## Post-Plan: Manual Tuning Phase

Once all 8 tasks pass review, the plan's execution is complete. The
subsequent config-tuning/backtesting research loop (curated ~47-symbol pool
first, then the full ~658-symbol pool per the design spec's Backtest
Workflow section) happens as direct, iterative instructions in the
conversation, the same way it did for the retired fib strategy -- it is
deliberately not part of this formal task plan.
