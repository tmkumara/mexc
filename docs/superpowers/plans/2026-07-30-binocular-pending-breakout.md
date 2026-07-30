# Binocular Pending-Breakout v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully replace `strategy.py`'s Ribbon-Flip Trend-Bar Confirmation v1 pipeline with a Chandelier/PVT/dual-RSI trigger, a two-phase pending-breakout entry lifecycle, and 3-target partial-exit outcome tracking, per `docs/superpowers/specs/2026-07-30-binocular-pending-breakout-design.md`.

**Architecture:** A stateless trigger (Chandelier ATR-direction flip + PVT-vs-signal-line + dual-RSI, optionally gated by the existing EMA ribbon+EMA200, optionally further gated by VWAP+MTF in `strict` mode) creates a PENDING setup persisted in the (currently dormant) `armed_setups` table. A later scan confirms the setup once price breaks its precomputed entry level, at which point a real `signals` row is created and tracked through a 3-target partial-exit ladder (50%/30%/20%, breakeven after T1) instead of a single fixed TP/SL.

**Tech Stack:** Python 3, pandas/numpy, SQLite (existing `database.py`), python-telegram-bot (existing `bot.py`), APScheduler (existing `main.py`), pytest.

## Global Constraints

- No forming (still-open) candle is ever evaluated — every function operates on `df.iloc[:-1]` (closed candles only), matching the existing codebase-wide convention.
- `MIN_CANDLE_SETTLE_SECONDS` gate (unchanged, 90s default) applies before any new pending setup is created.
- Every confirmed setup must satisfy `valid_trade_geometry` (unchanged helper) and `rr >= MIN_RR` computed against T1, and structural SL must be `<= MAX_SL_PRICE_PCT` of entry.
- `SIGNAL_MODE=strict` (VWAP + MTF) and position sizing must be fully implemented (not stubbed), even though `SIGNAL_MODE` defaults to `confirmed` and never pays the extra API cost unless explicitly configured.
- `scalper_v3_strategy.py`, `backtest/engine.py`, and `outcome_check.check_tp_sl` are untouched — the Super Scalper v3 track is fully independent.
- No second enable/disable flag is introduced. `STRATEGY_V1_ENABLED` (name unchanged) remains the sole gate for whether the scanner job runs at all. Safety comes from staying on a branch until the backtest comparison (against the 2026-07-27 attempt's 44-trade baseline) is reviewed — do not merge to `main` (which auto-deploys) as part of this plan.
- Tests import fixtures via `from tests.strategy_fixtures import ...` (this repo's established convention — `tests/` is a real package with `__init__.py`).
- Strategy-level config constants are read as bare module-level names inside `strategy.py` (via one `from config import (...)` block at the top of the file), never via a fresh `from config import X` inside a function body — tests monkeypatch `strategy.X`, not `config.X`, matching every existing test in `tests/test_strategy_ribbon_trendbar.py`.

---

### Task 1: Backup branch and feature branch

**Files:** none (git operations only)

- [ ] **Step 1: Confirm working tree is clean**

Run: `git status`
Expected: clean working tree on `main`, matching what's currently deployed.

- [ ] **Step 2: Cut and push the backup branch**

```bash
git checkout -b backup/ribbon-trendbar-confirmation-v1
git push -u origin backup/ribbon-trendbar-confirmation-v1
git checkout main
```

- [ ] **Step 3: Create the implementation branch**

```bash
git checkout -b feature/binocular-pending-breakout-v1
```

All subsequent tasks happen on this branch. Do not merge to `main` until Task 24's backtest comparison is reviewed.

---

### Task 2: PVT and PVT-signal indicators

**Files:**
- Modify: `strategy.py` (add functions after `calculate_ema_ribbon`, which ends at `strategy.py:137`, and before `def calculate_trend_bar` at `strategy.py:140`)
- Test: `tests/test_binocular_indicators.py` (new file)

**Interfaces:**
- Produces: `calculate_pvt(df: pd.DataFrame) -> pd.Series`, `calculate_pvt_signal(pvt: pd.Series, length: int, ma_type: str) -> pd.Series`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_binocular_indicators.py`:

```python
import numpy as np
import pandas as pd
import pytest

from strategy import calculate_pvt, calculate_pvt_signal


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    """A clean, noiseless trend series (step>0 up, step<0 down) -- same
    shape as tests/test_indicators.py's private helper of the same name."""
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_pvt_accumulates_correctly():
    df = pd.DataFrame({
        "close": [100.0, 102.0, 101.0, 103.0],
        "volume": [1000.0, 1500.0, 1200.0, 1800.0],
    })
    pvt = calculate_pvt(df)
    assert pvt.iloc[0] == pytest.approx(0.0)
    assert pvt.iloc[1] == pytest.approx(30.0)
    expected_2 = 30.0 + 1200.0 * (101.0 - 102.0) / 102.0
    assert pvt.iloc[2] == pytest.approx(expected_2)
    expected_3 = expected_2 + 1800.0 * (103.0 - 101.0) / 101.0
    assert pvt.iloc[3] == pytest.approx(expected_3)


def test_pvt_signal_sma():
    pvt = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="SMA")
    assert signal.iloc[2] == pytest.approx((10.0 + 20.0 + 30.0) / 3)
    assert signal.iloc[4] == pytest.approx((30.0 + 40.0 + 50.0) / 3)


def test_pvt_signal_ema():
    pvt = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="EMA")
    # alpha = 2/(3+1) = 0.5, seed = first value (matches calculate_ema's convention)
    expected = [10.0, 15.0, 22.5, 31.25, 40.625]
    for got, want in zip(signal.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -v`
Expected: FAIL with `ImportError: cannot import name 'calculate_pvt'`.

- [ ] **Step 3: Implement in `strategy.py`**

Insert immediately after `calculate_ema_ribbon` (currently the last line before `def calculate_trend_bar`):

```python
def calculate_pvt(df: pd.DataFrame) -> pd.Series:
    close = df["close"]
    volume = df["volume"]
    pct_change = close.pct_change()
    return (pct_change * volume).fillna(0.0).cumsum()


def calculate_pvt_signal(pvt: pd.Series, length: int, ma_type: str) -> pd.Series:
    if ma_type.upper() == "EMA":
        return pvt.ewm(span=length, adjust=False).mean()
    return pvt.rolling(window=length, min_periods=1).mean()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add PVT and PVT-signal indicators"
```

---

### Task 3: Chandelier Exit direction indicator

**Files:**
- Modify: `strategy.py` (add after the functions from Task 2)
- Test: `tests/test_binocular_indicators.py`

**Interfaces:**
- Consumes: `calculate_atr(df, period) -> pd.Series` (existing, `strategy.py:69`)
- Produces: `calculate_chandelier_direction(df: pd.DataFrame, atr_period: int, multiplier: float) -> tuple[pd.Series, pd.Series, pd.Series]` returning `(direction, long_stop_prev, short_stop_prev)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import calculate_chandelier_direction


def test_chandelier_direction_bullish():
    df = _trend_df(40, step=1.0)
    direction, _, _ = calculate_chandelier_direction(df, atr_period=10, multiplier=2.2)
    assert direction.iloc[-1] == 1


def test_chandelier_direction_bearish():
    df = _trend_df(40, step=-1.0)
    direction, _, _ = calculate_chandelier_direction(df, atr_period=10, multiplier=2.2)
    assert direction.iloc[-1] == -1


def test_chandelier_uses_previous_bar_stop_for_comparison():
    df = _trend_df(30, step=1.0)
    direction, long_stop_prev, short_stop_prev = calculate_chandelier_direction(
        df, atr_period=10, multiplier=2.2
    )
    for i in range(1, len(df)):
        close_i = df["close"].iloc[i]
        if close_i > short_stop_prev.iloc[i]:
            expected = 1
        elif close_i < long_stop_prev.iloc[i]:
            expected = -1
        else:
            expected = direction.iloc[i - 1]
        assert direction.iloc[i] == expected


def test_chandelier_does_not_use_future_data():
    df = _trend_df(40, step=1.0)
    dir_full, _, _ = calculate_chandelier_direction(df, atr_period=10, multiplier=2.2)
    dir_partial, _, _ = calculate_chandelier_direction(df.iloc[:25].copy(), atr_period=10, multiplier=2.2)
    for i in range(25):
        assert dir_full.iloc[i] == dir_partial.iloc[i]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -k chandelier -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement in `strategy.py`**

```python
def calculate_chandelier_direction(
    df: pd.DataFrame, atr_period: int, multiplier: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Chandelier Exit direction flip, ported from the Pine script's
    calculation() function's longStop/shortStop/dir recursion. Returns
    (direction, long_stop_prev, short_stop_prev) where the *_prev series
    hold each bar's stop level as it stood going INTO that bar -- what
    the Pine script calls longStopPrev/shortStopPrev, which is what the
    BUY/SELL trigger and the direction flip itself both compare against,
    never the same bar's just-updated stop."""
    close = df["close"]
    atr = calculate_atr(df, atr_period) * multiplier
    highest_close = close.rolling(window=atr_period, min_periods=1).max()
    lowest_close = close.rolling(window=atr_period, min_periods=1).min()

    raw_long = (highest_close - atr).to_numpy()
    raw_short = (lowest_close + atr).to_numpy()
    close_v = close.to_numpy()
    n = len(df)

    long_stop = np.zeros(n)
    short_stop = np.zeros(n)
    direction = np.ones(n, dtype=int)

    long_stop[0] = raw_long[0]
    short_stop[0] = raw_short[0]

    for i in range(1, n):
        long_stop_prev = long_stop[i - 1]
        short_stop_prev = short_stop[i - 1]

        long_stop[i] = (
            max(raw_long[i], long_stop_prev) if close_v[i - 1] > long_stop_prev else raw_long[i]
        )
        short_stop[i] = (
            min(raw_short[i], short_stop_prev) if close_v[i - 1] < short_stop_prev else raw_short[i]
        )

        if close_v[i] > short_stop_prev:
            direction[i] = 1
        elif close_v[i] < long_stop_prev:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    direction_s = pd.Series(direction, index=df.index)
    long_stop_prev_s = pd.Series(long_stop, index=df.index).shift(1).bfill()
    short_stop_prev_s = pd.Series(short_stop, index=df.index).shift(1).bfill()
    return direction_s, long_stop_prev_s, short_stop_prev_s
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -k chandelier -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add Chandelier Exit direction indicator"
```

---

### Task 4: EMA200 and daily VWAP indicators

**Files:**
- Modify: `strategy.py` (add after Task 3's functions)
- Test: `tests/test_binocular_indicators.py`

**Interfaces:**
- Consumes: `calculate_ema(series, period) -> pd.Series` (existing, `strategy.py:54`)
- Produces: `calculate_ema200(df: pd.DataFrame, length: int) -> pd.Series`, `calculate_daily_vwap(df: pd.DataFrame) -> pd.Series`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import calculate_ema200, calculate_daily_vwap, calculate_ema


def test_ema200_matches_calculate_ema():
    df = _trend_df(210, step=0.5)
    ema200 = calculate_ema200(df, 200)
    expected = calculate_ema(df["close"], 200)
    pd.testing.assert_series_equal(ema200, expected)


def test_daily_vwap_resets_at_session_boundary():
    idx = pd.date_range("2026-01-01", periods=4, freq="12h")  # 2 candles/day, 2 days
    df = pd.DataFrame({
        "high":  [101.0, 103.0, 201.0, 203.0],
        "low":   [99.0, 101.0, 199.0, 201.0],
        "close": [100.0, 102.0, 200.0, 202.0],
        "volume": [10.0, 10.0, 10.0, 10.0],
    }, index=idx)
    vwap = calculate_daily_vwap(df)
    day1_vwap_bar2 = (100.0 * 10 + 102.0 * 10) / (10 + 10)
    assert vwap.iloc[1] == pytest.approx(day1_vwap_bar2)
    day2_typical_bar1 = (201.0 + 199.0 + 200.0) / 3
    assert vwap.iloc[2] == pytest.approx(day2_typical_bar1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -k "ema200 or vwap" -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement in `strategy.py`**

```python
def calculate_ema200(df: pd.DataFrame, length: int) -> pd.Series:
    return calculate_ema(df["close"], length)


def calculate_daily_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = typical * df["volume"]
    day = df.index.normalize()
    cum_tp_vol = tp_vol.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_tp_vol / cum_vol.replace(0.0, np.nan)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -k "ema200 or vwap" -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add EMA200 and daily VWAP indicators"
```

---

### Task 5: Raw BUY/SELL trigger and transition detection

**Files:**
- Modify: `strategy.py` — add the `from config import (...)` names needed by this and later tasks to the existing top-level config import block (currently `strategy.py:190-197`); add new functions after Task 4's functions.
- Test: `tests/test_binocular_indicators.py`

**Interfaces:**
- Consumes: `calculate_chandelier_direction`, `calculate_pvt`, `calculate_pvt_signal`, `calculate_rsi` (existing)
- Produces: `calculate_binocular_trigger(df: pd.DataFrame) -> pd.DataFrame` (columns `buy`, `sell`), `detect_transition(trigger: pd.DataFrame) -> str | None`

- [ ] **Step 1: Extend the top-level config import**

In `strategy.py`, replace the existing import block:

```python
from market_data import get_market_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, CANDLE_MINUTES,
    RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN,
    RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS,
    TREND_BAR_PAC_LENGTH, ATR_PERIOD, MIN_CANDLE_SETTLE_SECONDS,
    SL_ATR_BUFFER_MULTIPLIER, SL_FLOOR_ATR_MULT, LEVERAGE, TP_PRICE_PCT, MAX_SL_PRICE_PCT, MIN_RR,
    ENABLE_LONG_SIGNALS,
)
```

with:

```python
from market_data import get_market_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, CANDLE_MINUTES,
    RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN,
    RIBBON_BASELINE_LEN, ATR_PERIOD, MIN_CANDLE_SETTLE_SECONDS,
    LEVERAGE, MAX_SL_PRICE_PCT, MIN_RR, ENABLE_LONG_SIGNALS,
    SIGNAL_MODE, CONFIRMATION_TIMEFRAMES, MTF_MIN_CONFIRMATIONS,
    ACCOUNT_BALANCE, RISK_PERCENT_PER_TRADE,
    PVT_SIGNAL_TYPE, PVT_SIGNAL_LENGTH, RSI_FAST_PERIOD, RSI_SLOW_PERIOD,
    CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER, BINOCULAR_EMA200_LEN,
    ENTRY_BUFFER_PCT, PENDING_SIGNAL_EXPIRY_CANDLES,
    TARGET1_CLOSE_FRACTION, TARGET2_CLOSE_FRACTION, TARGET3_CLOSE_FRACTION,
    MOVE_SL_TO_BREAKEVEN_AFTER_T1,
)
```

(`RIBBON_LOOKBACK_BARS`, `TREND_BAR_PAC_LENGTH`, `SL_ATR_BUFFER_MULTIPLIER`, `SL_FLOOR_ATR_MULT`, `TP_PRICE_PCT` are dropped — they no longer exist in `config.py` after Task 9. This edit will not import-error until Task 9 runs; do this edit together with Task 9 if running strictly in order, or accept a temporary broken import between Task 5 and Task 9 if executing linearly — recommended: do this specific import-block edit as part of Task 9 instead, and skip re-editing the import block in this task if Task 9 hasn't landed yet. **Order note:** do Task 9 (config.py) immediately after this task, before running the full test suite, to avoid a broken intermediate state.)

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import calculate_binocular_trigger, detect_transition
import strategy
import config


def test_detect_transition_new_buy():
    trigger = pd.DataFrame({"buy": [False, False, True], "sell": [False, False, False]})
    assert detect_transition(trigger) == "LONG"


def test_detect_transition_new_sell():
    trigger = pd.DataFrame({"buy": [False, False, False], "sell": [False, False, True]})
    assert detect_transition(trigger) == "SHORT"


def test_detect_transition_no_change_returns_none():
    trigger = pd.DataFrame({"buy": [False, True, True], "sell": [False, False, False]})
    assert detect_transition(trigger) is None


def test_calculate_binocular_trigger_strong_uptrend_eventually_buys(monkeypatch):
    monkeypatch.setattr(strategy, "CHANDELIER_ATR_PERIOD", 10)
    monkeypatch.setattr(strategy, "CHANDELIER_MULTIPLIER", 2.2)
    monkeypatch.setattr(strategy, "PVT_SIGNAL_LENGTH", 21)
    monkeypatch.setattr(strategy, "PVT_SIGNAL_TYPE", "SMA")
    monkeypatch.setattr(strategy, "RSI_FAST_PERIOD", 25)
    monkeypatch.setattr(strategy, "RSI_SLOW_PERIOD", 55)
    df = _trend_df(220, step=1.0)
    trigger = calculate_binocular_trigger(df)
    assert bool(trigger["buy"].iloc[-1]) is True
    assert bool(trigger["sell"].iloc[-1]) is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -k "transition or trigger" -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Implement in `strategy.py`**

```python
def calculate_binocular_trigger(df: pd.DataFrame) -> pd.DataFrame:
    direction, _, _ = calculate_chandelier_direction(df, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(df["close"], RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(df["close"], RSI_SLOW_PERIOD)

    buy = (direction == 1) & (pvt > pvt_signal) & (rsi_fast > rsi_slow)
    sell = (direction == -1) & (pvt < pvt_signal) & (rsi_fast < rsi_slow)
    return pd.DataFrame({"buy": buy, "sell": sell}, index=df.index)


def detect_transition(trigger: pd.DataFrame) -> str | None:
    if len(trigger) < 2:
        return None
    buy_now, buy_prev = bool(trigger["buy"].iloc[-1]), bool(trigger["buy"].iloc[-2])
    sell_now, sell_prev = bool(trigger["sell"].iloc[-1]), bool(trigger["sell"].iloc[-2])
    if buy_now and not buy_prev:
        return "LONG"
    if sell_now and not sell_prev:
        return "SHORT"
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -k "transition or trigger" -v`
Expected: 4 passed. (This step depends on Task 9's config.py changes being present so `strategy.py` imports cleanly — if running tasks strictly in order, do Task 9 before this step.)

- [ ] **Step 6: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add raw Chandelier/PVT/RSI trigger and transition detection"
```

---

### Task 6: Confirmed-mode ribbon + EMA200 filter

**Files:**
- Modify: `strategy.py` (add after Task 5's functions)
- Test: `tests/test_binocular_indicators.py`

**Interfaces:**
- Consumes: `calculate_ema_ribbon` (existing), `calculate_ema200` (Task 4)
- Produces: `confirmed_mode_ok(direction: str, df: pd.DataFrame) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import confirmed_mode_ok


def test_confirmed_mode_ok_for_long_uptrend():
    df = _trend_df(260, step=0.5)
    assert confirmed_mode_ok("LONG", df) is True
    assert confirmed_mode_ok("SHORT", df) is False


def test_confirmed_mode_ok_for_short_downtrend():
    df = _trend_df(260, step=-0.5)
    assert confirmed_mode_ok("SHORT", df) is True
    assert confirmed_mode_ok("LONG", df) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -k confirmed_mode -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement in `strategy.py`**

```python
def confirmed_mode_ok(direction: str, df: pd.DataFrame) -> bool:
    lengths = (RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN)
    ribbon = calculate_ema_ribbon(df, lengths, RIBBON_BASELINE_LEN)
    ema200 = calculate_ema200(df, BINOCULAR_EMA200_LEN)

    ma1 = float(ribbon["ma1"].iloc[-1]); ma2 = float(ribbon["ma2"].iloc[-1])
    ma3 = float(ribbon["ma3"].iloc[-1]); ma4 = float(ribbon["ma4"].iloc[-1])
    ma5 = float(ribbon["ma5"].iloc[-1]); baseline = float(ribbon["baseline"].iloc[-1])
    close = float(df["close"].iloc[-1])
    ema200_last = float(ema200.iloc[-1])

    if direction == "LONG":
        return (
            ma1 > baseline and ma2 > baseline and ma3 > baseline
            and ma4 > baseline and ma5 > baseline and close > ema200_last
        )
    return (
        ma1 < baseline and ma2 < baseline and ma3 < baseline
        and ma4 < baseline and ma5 < baseline and close < ema200_last
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -k confirmed_mode -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add confirmed-mode ribbon+EMA200 filter"
```

---

### Task 7: MTF signal and strict-mode VWAP/MTF filter

**Files:**
- Modify: `strategy.py` (add after Task 6's functions)
- Test: `tests/test_binocular_indicators.py`

**Interfaces:**
- Consumes: `calculate_chandelier_direction`, `calculate_pvt`, `calculate_pvt_signal`, `calculate_daily_vwap`, `get_market_klines` (existing)
- Produces: `mtf_signal(df_tf: pd.DataFrame) -> tuple[bool, bool]`, `strict_mode_ok(direction: str, df: pd.DataFrame, symbol: str) -> tuple[bool, int]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import mtf_signal, strict_mode_ok


def test_mtf_signal_omits_rsi_term(monkeypatch):
    monkeypatch.setattr(strategy, "CHANDELIER_ATR_PERIOD", 10)
    monkeypatch.setattr(strategy, "CHANDELIER_MULTIPLIER", 2.2)
    monkeypatch.setattr(strategy, "PVT_SIGNAL_LENGTH", 21)
    monkeypatch.setattr(strategy, "PVT_SIGNAL_TYPE", "SMA")
    df = _trend_df(220, step=1.0)
    buy, sell = mtf_signal(df)
    assert buy is True
    assert sell is False


def test_strict_mode_requires_vwap_side(monkeypatch):
    monkeypatch.setattr(strategy, "CONFIRMATION_TIMEFRAMES", "30m")
    monkeypatch.setattr(strategy, "MTF_MIN_CONFIRMATIONS", 1)
    idx = pd.date_range("2026-01-01", periods=5, freq="15min")
    df = pd.DataFrame({
        "high":  [105.0, 106.0, 107.0, 108.0, 109.0],
        "low":   [95.0, 96.0, 97.0, 98.0, 99.0],
        "close": [90.0, 91.0, 92.0, 93.0, 80.0],
        "volume": [1000.0] * 5,
    }, index=idx)
    ok, confirmations = strict_mode_ok("LONG", df, "XRP_USDT")
    assert ok is False
    assert confirmations == 0


def test_strict_mode_requires_min_mtf_confirmations(monkeypatch):
    monkeypatch.setattr(strategy, "CONFIRMATION_TIMEFRAMES", "30m,1h,4h")
    monkeypatch.setattr(strategy, "MTF_MIN_CONFIRMATIONS", 2)
    monkeypatch.setattr(strategy, "ENTRY_KLINE_COUNT", 220)

    uptrend = _trend_df(220, step=1.0)
    downtrend_tf = _trend_df(220, step=-1.0)

    def _fake_klines(symbol, interval, count=100):
        return pd.concat([downtrend_tf, downtrend_tf.iloc[[-1]]])

    monkeypatch.setattr(strategy, "get_market_klines", _fake_klines)

    ok, confirmations = strict_mode_ok("LONG", uptrend, "XRP_USDT")
    assert ok is False
    assert confirmations == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -k "mtf or strict" -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement in `strategy.py`**

```python
def mtf_signal(df_tf: pd.DataFrame) -> tuple[bool, bool]:
    direction, _, _ = calculate_chandelier_direction(df_tf, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df_tf)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    buy = bool(direction.iloc[-1] == 1 and pvt.iloc[-1] > pvt_signal.iloc[-1])
    sell = bool(direction.iloc[-1] == -1 and pvt.iloc[-1] < pvt_signal.iloc[-1])
    return buy, sell


def strict_mode_ok(direction: str, df: pd.DataFrame, symbol: str) -> tuple[bool, int]:
    vwap = calculate_daily_vwap(df)
    close = float(df["close"].iloc[-1])
    vwap_last = float(vwap.iloc[-1])
    vwap_ok = close > vwap_last if direction == "LONG" else close < vwap_last
    if not vwap_ok:
        return False, 0

    confirmations = 0
    timeframes = [t.strip() for t in CONFIRMATION_TIMEFRAMES.split(",") if t.strip()]
    for tf in timeframes:
        tf_df = get_market_klines(symbol, tf, count=ENTRY_KLINE_COUNT)
        if tf_df is None or tf_df.empty:
            continue
        tf_closed = tf_df.iloc[:-1]
        if len(tf_closed) < 60:
            continue
        buy, sell = mtf_signal(tf_closed)
        if direction == "LONG" and buy:
            confirmations += 1
        elif direction == "SHORT" and sell:
            confirmations += 1

    return confirmations >= MTF_MIN_CONFIRMATIONS, confirmations
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -k "mtf or strict" -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add MTF signal and strict-mode VWAP/MTF filter"
```

---

### Task 8: Position sizing (informational)

**Files:**
- Modify: `strategy.py` (add after Task 7's functions)
- Test: `tests/test_binocular_indicators.py`

**Interfaces:**
- Produces: `position_size(direction: str, entry: float, sl: float) -> float`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import position_size


def test_position_size_long(monkeypatch):
    monkeypatch.setattr(strategy, "ACCOUNT_BALANCE", 10000.0)
    monkeypatch.setattr(strategy, "RISK_PERCENT_PER_TRADE", 1.0)
    size = position_size("LONG", entry=100.0, sl=98.0)
    assert size == pytest.approx(50.0)


def test_position_size_short(monkeypatch):
    monkeypatch.setattr(strategy, "ACCOUNT_BALANCE", 5000.0)
    monkeypatch.setattr(strategy, "RISK_PERCENT_PER_TRADE", 2.0)
    size = position_size("SHORT", entry=100.0, sl=103.0)
    assert size == pytest.approx(100.0 / 3.0, abs=1e-4)


def test_position_size_zero_risk_returns_zero():
    assert position_size("LONG", entry=100.0, sl=100.0) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -k position_size -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement in `strategy.py`**

```python
def position_size(direction: str, entry: float, sl: float) -> float:
    risk_per_unit = (entry - sl) if direction == "LONG" else (sl - entry)
    if risk_per_unit <= 0:
        return 0.0
    risk_amount = ACCOUNT_BALANCE * RISK_PERCENT_PER_TRADE / 100.0
    return round(risk_amount / risk_per_unit, 6)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -k position_size -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add informational position sizing"
```

---

### Task 9: `config.py` — remove old constants, add new Binocular block

**Files:**
- Modify: `config.py:63-122` (the `ENTRY_TF` through `MIN_RR` block) and the `STRATEGY_NAME` default at `config.py:58-61`

**Interfaces:**
- Produces (new module-level names): `SIGNAL_MODE`, `CONFIRMATION_TIMEFRAMES`, `MTF_MIN_CONFIRMATIONS`, `ACCOUNT_BALANCE`, `RISK_PERCENT_PER_TRADE`, `PVT_SIGNAL_TYPE`, `PVT_SIGNAL_LENGTH`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `BINOCULAR_EMA200_LEN`, `ENTRY_BUFFER_PCT`, `PENDING_SIGNAL_EXPIRY_CANDLES`, `TARGET1_CLOSE_FRACTION`, `TARGET2_CLOSE_FRACTION`, `TARGET3_CLOSE_FRACTION`, `MOVE_SL_TO_BREAKEVEN_AFTER_T1`
- Removes: `RIBBON_LOOKBACK_BARS`, `TREND_BAR_PAC_LENGTH`, `SL_ATR_BUFFER_MULTIPLIER`, `SL_FLOOR_ATR_MULT`, `TARGET_ROI_PCT`, `TP_PRICE_PCT`

- [ ] **Step 1: Update `STRATEGY_NAME` default**

Replace:
```python
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Ribbon-Flip Trend-Bar Confirmation v1",
)
```
with:
```python
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Binocular Pending-Breakout v1",
)
```

- [ ] **Step 2: Replace the strategy config block**

Replace the entire block from `ENTRY_TF: str = os.getenv("ENTRY_TF", "15m")` through `MIN_RR: float = float(os.getenv("MIN_RR", "1.5"))` (currently `config.py:63-122`) with:

```python
ENTRY_TF: str = os.getenv("ENTRY_TF", "15m")
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "220"))
# Bumped from 120 -- EMA200 (confirmed/strict SIGNAL_MODE) needs ~200 bars
# of warmup, plus margin for the Chandelier(10)/RSI(55)/PVT-signal(21)
# periods and the ribbon baseline(60).

# 6-EMA ribbon (Pine script defaults) -- confirmed/strict-mode filter,
# not the primary trigger (see SIGNAL_MODE below).
RIBBON_MA1_LEN: int = int(os.getenv("RIBBON_MA1_LEN", "30"))
RIBBON_MA2_LEN: int = int(os.getenv("RIBBON_MA2_LEN", "35"))
RIBBON_MA3_LEN: int = int(os.getenv("RIBBON_MA3_LEN", "40"))
RIBBON_MA4_LEN: int = int(os.getenv("RIBBON_MA4_LEN", "45"))
RIBBON_MA5_LEN: int = int(os.getenv("RIBBON_MA5_LEN", "50"))
RIBBON_BASELINE_LEN: int = int(os.getenv("RIBBON_BASELINE_LEN", "60"))

# Minimum age (seconds) the last CLOSED candle must have before a signal
# can fire on it. MEXC's kline REST data for a just-closed candle can still
# get revised for a short window after the close.
MIN_CANDLE_SETTLE_SECONDS: int = int(os.getenv("MIN_CANDLE_SETTLE_SECONDS", "90"))

# ATR period backing the Chandelier trailing-stop direction flip.
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

# LONG signals underperformed SHORT in every backtest of the prior
# ribbon-flip strategy -- left enabled by default so the asymmetry can
# keep being observed on live data; set to "false" to run SHORT-only.
ENABLE_LONG_SIGNALS: bool = os.getenv("ENABLE_LONG_SIGNALS", "true").lower() == "true"

MAX_SL_ROI_PCT: float = float(os.getenv("MAX_SL_ROI_PCT", "10.0"))
LEVERAGE: int = int(os.getenv("LEVERAGE", "20"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE

MIN_RR: float = float(os.getenv("MIN_RR", "1.5"))

# ── Strategy: Binocular Pending-Breakout v1 ─────────────────────────
SIGNAL_MODE: str = os.getenv("SIGNAL_MODE", "confirmed")   # "original" | "confirmed" | "strict"
CONFIRMATION_TIMEFRAMES: str = os.getenv("CONFIRMATION_TIMEFRAMES", "30m,1h,4h")   # strict mode only
MTF_MIN_CONFIRMATIONS: int = int(os.getenv("MTF_MIN_CONFIRMATIONS", "2"))          # strict mode only

ACCOUNT_BALANCE: float = float(os.getenv("ACCOUNT_BALANCE", "10000"))              # informational only
RISK_PERCENT_PER_TRADE: float = float(os.getenv("RISK_PERCENT_PER_TRADE", "1.0"))  # informational only

PVT_SIGNAL_TYPE: str = os.getenv("PVT_SIGNAL_TYPE", "SMA")           # "SMA" | "EMA"
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

- [ ] **Step 3: Verify the module still imports**

Run: `python -c "import config"`
Expected: no errors.

- [ ] **Step 4: Re-run every test written so far**

Run: `python -m pytest tests/test_binocular_indicators.py -v`
Expected: all pass now that `strategy.py`'s import block (Task 5, Step 1) resolves cleanly.

- [ ] **Step 5: Commit**

```bash
git add config.py
git commit -m "feat: replace ribbon-flip config block with Binocular settings"
```

---

### Task 10: `database.py` — schema and function additions

**Files:**
- Modify: `database.py` (the `signals` ALTER-TABLE loop at `database.py:61-89`, the `armed_setups` `CREATE TABLE` at `database.py:128-150`, `save_armed_setup` at `database.py:474-501`, `save_signal` at `database.py:166-193`)
- Test: `tests/test_database_binocular_columns.py` (new file)

**Interfaces:**
- Produces: `mark_signal_tp2_hit(signal_id: int, hit_at: datetime) -> None`
- Modifies: `save_armed_setup(setup: dict) -> int | None` (dict now accepts `tp2_price`, `tp3_price`, `position_size`), `save_signal(...)` (gains optional `tp2_price=None, tp3_price=None, position_size=None`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_database_binocular_columns.py`:

```python
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DB_PATH"] = path
    import importlib
    import config
    importlib.reload(config)
    import database
    importlib.reload(database)
    database.init_db()
    yield database
    os.remove(path)


def test_save_armed_setup_stores_three_targets(temp_db):
    setup_id = temp_db.save_armed_setup({
        "symbol": "XRP_USDT", "direction": "LONG",
        "trigger_price": 1.0, "entry_low": 1.0, "entry_high": 1.0,
        "sl_price": 0.98, "tp_price": 1.02, "tp2_price": 1.04, "tp3_price": 1.06,
        "position_size": 500.0, "rr": 1.8, "score": 70.0,
        "expires_at": datetime.now(timezone.utc).isoformat(),
    })
    setups = temp_db.get_armed_setups()
    assert len(setups) == 1
    row = setups[0]
    assert row["tp2_price"] == pytest.approx(1.04)
    assert row["tp3_price"] == pytest.approx(1.06)
    assert row["position_size"] == pytest.approx(500.0)


def test_save_signal_stores_targets_and_position_size(temp_db):
    signal_id = temp_db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0,
        tp_price=1.02, sl_price=0.98, leverage=20,
        generated_at=datetime.now(timezone.utc),
        tp2_price=1.04, tp3_price=1.06, position_size=500.0,
    )
    rows = temp_db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["tp2_price"] == pytest.approx(1.04)
    assert row["tp3_price"] == pytest.approx(1.06)
    assert row["position_size"] == pytest.approx(500.0)


def test_mark_signal_tp2_hit(temp_db):
    signal_id = temp_db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0,
        tp_price=1.02, sl_price=0.98, leverage=20,
        generated_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    temp_db.mark_signal_tp2_hit(signal_id, now)
    rows = temp_db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["tp2_hit_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_database_binocular_columns.py -v`
Expected: FAIL — `sqlite3.OperationalError: table armed_setups has no column named tp2_price` (or similar).

- [ ] **Step 3: Add the new `signals` columns to the existing ALTER-TABLE loop**

In `database.py`, in the `for col, definition in [...]` list at `database.py:61-89`, add three entries (anywhere in the list, e.g. right after the existing `("signal_message_id", "INTEGER")` line):

```python
            ("tp3_price", "REAL"),
            ("tp2_hit_at", "TEXT"),
            ("position_size", "REAL"),
```

- [ ] **Step 4: Add an ALTER-TABLE loop for `armed_setups`**

Immediately after the `armed_setups` `CREATE TABLE IF NOT EXISTS` block and its two `CREATE INDEX` statements (`database.py:128-159`), add:

```python
        for col, definition in [
            ("tp2_price", "REAL"),
            ("tp3_price", "REAL"),
            ("position_size", "REAL"),
        ]:
            try:
                con.execute(f"ALTER TABLE armed_setups ADD COLUMN {col} {definition}")
            except Exception:
                pass
```

- [ ] **Step 5: Update `save_armed_setup`**

Replace the function body:

```python
def save_armed_setup(setup: dict) -> int | None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO armed_setups (
                symbol, direction, status,
                trigger_price, entry_low, entry_high,
                sl_price, tp_price, tp2_price, tp3_price, position_size, rr, score,
                setup_reason, trend_summary,
                created_at, expires_at, updated_at
            ) VALUES (?, ?, 'armed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            setup["symbol"],
            setup["direction"],
            setup["trigger_price"],
            setup["entry_low"],
            setup["entry_high"],
            setup["sl_price"],
            setup["tp_price"],
            setup.get("tp2_price"),
            setup.get("tp3_price"),
            setup.get("position_size"),
            setup["rr"],
            setup["score"],
            setup.get("setup_reason", ""),
            setup.get("trend_summary", ""),
            now,
            setup["expires_at"],
            now,
        ))
        return cur.lastrowid
```

- [ ] **Step 6: Update `save_signal` and add `mark_signal_tp2_hit`**

Replace the `save_signal` signature and body:

```python
def save_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    leverage: int,
    generated_at: datetime,
    strategy_name: str = "",
    score: float = 0.0,
    rr: float = 0.0,
    entry_timeframe: str = "",
    trend_timeframe: str = "",
    setup_reason: str = "",
    tp2_price: float | None = None,
    tp3_price: float | None = None,
    position_size: float | None = None,
) -> int:
    ts = generated_at.isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO signals
              (symbol, direction, entry_price, tp_price, sl_price,
               leverage, status, placed, generated_at, placed_at,
               strategy_name, score, rr, entry_timeframe, trend_timeframe, setup_reason,
               tp2_price, tp3_price, position_size)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, direction, entry_price, tp_price, sl_price, leverage, ts, ts,
            strategy_name, score, rr, entry_timeframe, trend_timeframe, setup_reason,
            tp2_price, tp3_price, position_size,
        ))
        return cur.lastrowid


def mark_signal_tp2_hit(signal_id: int, hit_at: datetime) -> None:
    ts = hit_at.isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE signals
            SET tp2_hit_at = ?
            WHERE id = ? AND tp2_hit_at IS NULL
        """, (ts, signal_id))
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_database_binocular_columns.py -v`
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add database.py tests/test_database_binocular_columns.py
git commit -m "feat: add 3-target and position-size columns to signals/armed_setups"
```

---

### Task 11: Pending-setup builder and scoring

**Files:**
- Modify: `strategy.py` (add after Task 8's functions)
- Test: `tests/test_strategy_binocular_pending.py` (new file)

**Interfaces:**
- Consumes: `valid_trade_geometry` (existing, `strategy.py:200`), `position_size` (Task 8), `calculate_pvt`/`calculate_pvt_signal`/`calculate_rsi`/`calculate_ema_ribbon`/`calculate_atr` (existing/Task 2)
- Produces: `_build_pending_setup(symbol: str, direction: str, df: pd.DataFrame, reject_sink: dict | None = None) -> dict | None`, `_score_pending_setup(direction: str, df: pd.DataFrame, rr: float, mtf_confirmations: int | None) -> float`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_strategy_binocular_pending.py`:

```python
import pandas as pd
import pytest

import strategy
from strategy import _build_pending_setup, _score_pending_setup


def test_build_pending_setup_long():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.8, 100.8, 100.85],
        "high":  [100.9, 100.9, 101.0],
        "low":   [100.7, 100.8, 100.85],
        "close": [100.85, 100.85, 100.95],
        "volume": [1000.0] * 3,
    }, index=idx)

    setup = _build_pending_setup("XRP_USDT", "LONG", df)

    assert setup is not None
    high, prev_low = 101.0, 100.8
    expected_entry = high * (1 + 0.0002)
    assert setup["trigger_price"] == pytest.approx(expected_entry)
    assert setup["sl_price"] == pytest.approx(100.8)
    diff = (high - prev_low) * 2
    assert setup["tp_price"] == pytest.approx(high + diff, abs=1e-6)
    assert setup["tp2_price"] == pytest.approx(high + 2 * diff, abs=1e-6)
    assert setup["tp3_price"] == pytest.approx(high + 3 * diff, abs=1e-6)
    assert setup["rr"] >= 1.5


def test_build_pending_setup_short():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.85, 100.85, 100.75],
        "high":  [100.95, 100.9, 100.85],
        "low":   [100.75, 100.7, 100.6],
        "close": [100.8, 100.75, 100.65],
        "volume": [1000.0] * 3,
    }, index=idx)

    setup = _build_pending_setup("XRP_USDT", "SHORT", df)

    assert setup is not None
    prev_high, low = 100.9, 100.6
    expected_entry = low * (1 - 0.0002)
    assert setup["trigger_price"] == pytest.approx(expected_entry)
    assert setup["sl_price"] == pytest.approx(100.9)
    diff = (prev_high - low) * 2
    assert setup["tp_price"] == pytest.approx(low - diff, abs=1e-6)
    assert setup["tp2_price"] == pytest.approx(low - 2 * diff, abs=1e-6)
    assert setup["tp3_price"] == pytest.approx(low - 3 * diff, abs=1e-6)
    assert setup["rr"] >= 1.5


def test_pending_setup_rejected_when_stop_too_wide():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.0, 100.0, 100.4],
        "high":  [100.1, 100.1, 101.2],
        "low":   [99.9, 100.0, 100.5],
        "close": [100.0, 100.05, 101.1],
        "volume": [1000.0] * 3,
    }, index=idx)
    # sl = min(prev_low=100.0, low=100.5) = 100.0; entry ~= 101.22 ->
    # sl distance ~1.2% of entry, well above MAX_SL_PRICE_PCT (0.5% default).
    setup = _build_pending_setup("XRP_USDT", "LONG", df, reject_sink={})
    assert setup is None


def test_pending_setup_rejected_when_rr_below_min():
    # Numeric constants here are reasoned, not hand-executed against
    # pandas -- same convention as tests/strategy_fixtures.py. If this
    # fails because RR lands >= MIN_RR instead of below it, widen the gap
    # between the swing distance and the (high-prev_low) diff further and
    # re-run -- expected TDD iteration, not a defect in the test itself.
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.7, 100.7, 100.8],
        "high":  [100.75, 100.75, 100.85],
        "low":   [100.65, 100.7, 100.57],
        "close": [100.72, 100.72, 100.7],
        "volume": [1000.0] * 3,
    }, index=idx)
    setup = _build_pending_setup("XRP_USDT", "LONG", df, reject_sink={})
    assert setup is None


def test_pending_setup_carries_position_size(monkeypatch):
    monkeypatch.setattr(strategy, "ACCOUNT_BALANCE", 10000.0)
    monkeypatch.setattr(strategy, "RISK_PERCENT_PER_TRADE", 1.0)
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.8, 100.8, 100.85],
        "high":  [100.9, 100.9, 101.0],
        "low":   [100.7, 100.8, 100.85],
        "close": [100.85, 100.85, 100.95],
        "volume": [1000.0] * 3,
    }, index=idx)
    setup = _build_pending_setup("XRP_USDT", "LONG", df)
    assert setup is not None
    assert setup["position_size"] > 0.0


def test_score_pending_setup_within_bounds(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    monkeypatch.setattr(strategy, "SIGNAL_MODE", "confirmed")
    df = make_15m_trend_df("LONG", bars=220)
    score = _score_pending_setup("LONG", df, rr=2.0, mtf_confirmations=None)
    assert 0.0 <= score <= 100.0


def test_score_pending_setup_higher_rr_scores_higher(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    monkeypatch.setattr(strategy, "SIGNAL_MODE", "confirmed")
    df = make_15m_trend_df("LONG", bars=220)
    low_rr_score = _score_pending_setup("LONG", df, rr=strategy.MIN_RR, mtf_confirmations=None)
    high_rr_score = _score_pending_setup("LONG", df, rr=strategy.MIN_RR + 1.0, mtf_confirmations=None)
    assert high_rr_score > low_rr_score
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `_build_pending_setup` in `strategy.py`**

```python
def _build_pending_setup(
    symbol: str, direction: str, df: pd.DataFrame, reject_sink: dict | None = None
) -> dict | None:
    high = float(df["high"].iloc[-1])
    low = float(df["low"].iloc[-1])
    prev_high = float(df["high"].iloc[-2])
    prev_low = float(df["low"].iloc[-2])

    if direction == "LONG":
        entry = high * (1 + ENTRY_BUFFER_PCT)
        sl = min(prev_low, low)
        diff = (high - prev_low) * 2
        t1, t2, t3 = high + diff, high + 2 * diff, high + 3 * diff
    else:
        entry = low * (1 - ENTRY_BUFFER_PCT)
        sl = max(prev_high, high)
        diff = (prev_high - low) * 2
        t1, t2, t3 = low - diff, low - 2 * diff, low - 3 * diff

    if not valid_trade_geometry(direction, entry, t1, sl):
        _bump(reject_sink, "invalid_geometry")
        return None

    if abs(entry - sl) / entry > MAX_SL_PRICE_PCT:
        _bump(reject_sink, "stop_too_wide")
        return None

    rr = abs(t1 - entry) / abs(entry - sl)
    if rr < MIN_RR:
        _bump(reject_sink, "rr_below_min")
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "trigger_price": entry,
        "entry_low": entry,
        "entry_high": entry,
        "sl_price": sl,
        "tp_price": round(t1, 8),
        "tp2_price": round(t2, 8),
        "tp3_price": round(t3, 8),
        "rr": round(rr, 2),
        "position_size": position_size(direction, entry, sl),
    }
```

- [ ] **Step 4: Run the setup-builder tests to verify they pass**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -k "build_pending or carries_position" -v`
Expected: 5 passed. If `test_pending_setup_rejected_when_rr_below_min` fails because RR lands above `MIN_RR`, adjust the fixture's price gap per its docstring and re-run (this is expected TDD iteration for a hand-reasoned fixture, not a defect).

- [ ] **Step 5: Implement `_score_pending_setup` in `strategy.py`**

```python
def _score_pending_setup(
    direction: str, df: pd.DataFrame, rr: float, mtf_confirmations: int | None
) -> float:
    pvt = calculate_pvt(df)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(df["close"], RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(df["close"], RSI_SLOW_PERIOD)

    pvt_range = max(abs(pvt.iloc[-20:]).max(), 1e-9)
    pvt_strength = min(1.0, abs(pvt.iloc[-1] - pvt_signal.iloc[-1]) / pvt_range)
    rsi_strength = min(1.0, abs(rsi_fast.iloc[-1] - rsi_slow.iloc[-1]) / 30.0)
    score = 40.0 * ((pvt_strength + rsi_strength) / 2.0)

    if SIGNAL_MODE == "original":
        score += 20.0
    else:
        lengths = (RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN)
        ribbon = calculate_ema_ribbon(df, lengths, RIBBON_BASELINE_LEN)
        atr_last = max(float(calculate_atr(df, ATR_PERIOD).iloc[-1]), 1e-9)
        separation = abs(float(ribbon["ma5"].iloc[-1]) - float(ribbon["baseline"].iloc[-1]))
        score += 20.0 * min(1.0, separation / (atr_last * 2.0))

    rr_quality = (
        min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0
        else (1.0 if rr >= MIN_RR else 0.0)
    )
    score += 20.0 * rr_quality

    close = float(df["close"].iloc[-1])
    entry_target = close * (1 + ENTRY_BUFFER_PCT) if direction == "LONG" else close * (1 - ENTRY_BUFFER_PCT)
    clearance_pct = abs(entry_target - close) / close
    score += 20.0 * max(0.0, 1.0 - min(1.0, clearance_pct / 0.01))

    return round(min(100.0, max(0.0, score)), 1)
```

- [ ] **Step 6: Run all tests in this file to verify they pass**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -v`
Expected: 7 passed.

- [ ] **Step 7: Commit**

```bash
git add strategy.py tests/test_strategy_binocular_pending.py
git commit -m "feat: add pending-setup builder and candidate scoring"
```

---

### Task 12: `detect_pending_setup`

**Files:**
- Modify: `strategy.py` (add after Task 11's functions)
- Test: `tests/test_strategy_binocular_pending.py`

**Interfaces:**
- Consumes: everything from Tasks 5-11
- Produces: `detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_binocular_pending.py`:

```python
from tests.strategy_fixtures import patch_klines
from strategy import detect_pending_setup


def test_detect_pending_setup_returns_none_on_missing_data(monkeypatch):
    monkeypatch.setattr(strategy, "get_market_klines", lambda *a, **k: pd.DataFrame())
    sink = {}
    assert detect_pending_setup("XRP_USDT", reject_sink=sink) is None
    assert sink.get("missing_data") == 1


def test_detect_pending_setup_returns_none_on_flat_series(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    df = make_15m_trend_df("LONG", bars=260, start_price=100.0)
    # Force a perfectly flat series (no trend) by overwriting close with a
    # constant -- flat data never flips Chandelier direction cleanly on a
    # fresh transition, so no pending setup should ever be created.
    flat = df.copy()
    flat["close"] = 100.0
    flat["open"] = 100.0
    flat["high"] = 100.05
    flat["low"] = 99.95
    patch_klines(monkeypatch, strategy, flat)
    sink = {}
    assert detect_pending_setup("XRP_USDT", reject_sink=sink) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -k detect_pending_setup -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `detect_pending_setup` in `strategy.py`**

```python
def _classify_no_trigger_reason(df: pd.DataFrame) -> str:
    direction, _, _ = calculate_chandelier_direction(df, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(df["close"], RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(df["close"], RSI_SLOW_PERIOD)

    dir_last = int(direction.iloc[-1])
    if dir_last == 1:
        if not (pvt.iloc[-1] > pvt_signal.iloc[-1]):
            return "no_pvt_momentum"
        if not (rsi_fast.iloc[-1] > rsi_slow.iloc[-1]):
            return "no_rsi_regime"
    else:
        if not (pvt.iloc[-1] < pvt_signal.iloc[-1]):
            return "no_pvt_momentum"
        if not (rsi_fast.iloc[-1] < rsi_slow.iloc[-1]):
            return "no_rsi_regime"
    return "no_chandelier_direction"


def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None:
    try:
        raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
        if raw is None or raw.empty:
            _bump(reject_sink, "missing_data")
            return None

        closed = raw.iloc[:-1].copy()

        min_history = max(
            RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD,
            RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH,
        ) + 10
        if len(closed) < min_history:
            _bump(reject_sink, "insufficient_history")
            return None

        candle_close_time = closed.index[-1].to_pydatetime() + timedelta(minutes=CANDLE_MINUTES)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        trigger = calculate_binocular_trigger(closed)
        direction = detect_transition(trigger)
        if direction is None:
            _bump(reject_sink, _classify_no_trigger_reason(closed))
            return None

        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            _bump(reject_sink, "long_disabled")
            return None

        if SIGNAL_MODE in ("confirmed", "strict") and not confirmed_mode_ok(direction, closed):
            _bump(reject_sink, "no_ribbon_confirmation")
            return None

        mtf_confirmations = None
        if SIGNAL_MODE == "strict":
            ok, mtf_confirmations = strict_mode_ok(direction, closed, symbol)
            if not ok:
                _bump(reject_sink, "no_vwap_confirmation" if mtf_confirmations == 0 else "no_mtf_confirmation")
                return None

        setup = _build_pending_setup(symbol, direction, closed, reject_sink)
        if setup is None:
            return None

        setup["score"] = _score_pending_setup(direction, closed, setup["rr"], mtf_confirmations)
        setup["setup_reason"] = f"Binocular {SIGNAL_MODE} trigger"
        setup["trend_summary"] = "Chandelier/PVT/RSI"
        setup["created_at"] = datetime.now(timezone.utc).isoformat()
        setup["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES)
        ).isoformat()
        return setup
    except Exception as e:
        logger.error("[BINOCULAR-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_strategy_binocular_pending.py
git commit -m "feat: add detect_pending_setup, ties trigger+filters+builder together"
```

---

### Task 13: `check_setup_confirmation`

**Files:**
- Modify: `strategy.py` (add after Task 12's functions)
- Test: `tests/test_strategy_binocular_pending.py`

**Interfaces:**
- Consumes: `get_market_klines`, `calculate_binocular_trigger`, `detect_transition` (Task 5)
- Produces: `check_setup_confirmation(setup: dict) -> tuple[str, float | None]` — status is one of `"confirmed"`, `"expired"`, `"invalidated"`, `"waiting"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_strategy_binocular_pending.py`:

```python
from datetime import datetime, timezone, timedelta
from strategy import check_setup_confirmation


def _setup_dict(symbol="XRP_USDT", direction="LONG", entry=101.0, sl=100.0, created_at=None):
    if created_at is None:
        created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    return {
        "symbol": symbol, "direction": direction,
        "trigger_price": entry, "sl_price": sl,
        "created_at": created_at.isoformat(),
    }


def test_setup_confirms_on_entry_breakout(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open": [100.5, 100.6, 101.2], "high": [100.7, 100.8, 101.5],
        "low": [100.3, 100.4, 101.0], "close": [100.6, 100.7, 101.3],
        "volume": [1000.0] * 3,
    }, index=idx)
    df = pd.concat([df, df.iloc[[-1]]])
    monkeypatch.setattr(strategy, "get_market_klines", lambda *a, **k: df)

    setup = _setup_dict(entry=101.0, sl=100.0)
    status, fill = check_setup_confirmation(setup)
    assert status == "confirmed"
    assert fill == pytest.approx(101.0)


def test_same_candle_sl_blocks_confirmation(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open": [100.5, 100.6, 100.8], "high": [100.7, 100.8, 101.5],
        "low": [100.3, 100.4, 99.5],
        "close": [100.6, 100.7, 100.9],
        "volume": [1000.0] * 3,
    }, index=idx)
    df = pd.concat([df, df.iloc[[-1]]])
    monkeypatch.setattr(strategy, "get_market_klines", lambda *a, **k: df)

    setup = _setup_dict(entry=101.0, sl=100.0)
    status, fill = check_setup_confirmation(setup)
    assert status == "invalidated"
    assert fill is None


def test_setup_expires_after_n_candles(monkeypatch):
    monkeypatch.setattr(strategy, "PENDING_SIGNAL_EXPIRY_CANDLES", 2)
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open": [100.5, 100.6, 100.7], "high": [100.7, 100.8, 100.85],
        "low": [100.3, 100.4, 100.5], "close": [100.6, 100.7, 100.75],
        "volume": [1000.0] * 3,
    }, index=idx)
    df = pd.concat([df, df.iloc[[-1]]])
    monkeypatch.setattr(strategy, "get_market_klines", lambda *a, **k: df)

    old_created = datetime.now(timezone.utc) - timedelta(minutes=60)
    setup = _setup_dict(entry=101.0, sl=100.0, created_at=old_created)
    status, fill = check_setup_confirmation(setup)
    assert status == "expired"


def test_setup_invalidated_by_opposite_transition(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    df = make_15m_trend_df("SHORT", bars=260)
    monkeypatch.setattr(strategy, "get_market_klines", lambda *a, **k: df)

    setup = _setup_dict(direction="LONG", entry=10_000.0, sl=9_000.0)
    status, fill = check_setup_confirmation(setup)
    assert status in ("invalidated", "waiting")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -k confirmation -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `check_setup_confirmation` in `strategy.py`**

```python
def check_setup_confirmation(setup: dict) -> tuple[str, float | None]:
    symbol = setup["symbol"]
    direction = setup["direction"]
    entry = setup["trigger_price"]
    sl = setup["sl_price"]

    raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
    if raw is None or raw.empty:
        return "waiting", None

    closed = raw.iloc[:-1].copy()
    if closed.empty:
        return "waiting", None

    latest = closed.iloc[-1]
    high, low = float(latest["high"]), float(latest["low"])

    if direction == "LONG":
        sl_hit = low <= sl
        entry_hit = high > entry
    else:
        sl_hit = high >= sl
        entry_hit = low < entry

    if sl_hit:
        return "invalidated", None
    if entry_hit:
        return "confirmed", entry

    created_at = datetime.fromisoformat(setup["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0
    if age_minutes > PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES:
        return "expired", None

    if len(closed) >= 2:
        trigger = calculate_binocular_trigger(closed)
        opposite = detect_transition(trigger)
        if opposite is not None and opposite != direction:
            return "invalidated", None

    return "waiting", None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_binocular_pending.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_strategy_binocular_pending.py
git commit -m "feat: add check_setup_confirmation for the pending-breakout lifecycle"
```

---

### Task 14: `outcome_check.check_target_ladder`

**Files:**
- Modify: `outcome_check.py` (add after `check_tp_sl`)
- Test: `tests/test_outcome_target_ladder.py` (new file)

**Interfaces:**
- Produces: `check_target_ladder(direction, entry_price, sl_price, tp1_price, tp2_price, tp3_price, df, entry_candle_cutoff, close_fracs=(0.5, 0.3, 0.2), move_sl_to_breakeven_after_t1=True) -> dict | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_outcome_target_ladder.py`:

```python
import pandas as pd
import pytest

from outcome_check import check_target_ladder


def test_t1_then_breakeven_stop_is_a_small_win():
    idx = pd.date_range("2026-01-01", periods=5, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 101.2, 101.5, 100.5, 100.5],
        "low":  [100.0, 100.5, 100.2, 99.8, 99.8],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])

    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "win"
    assert result["final_stage"] == 1
    assert result["t1_hit_at"] == idx[1]
    assert result["t2_hit_at"] is None
    assert result["pnl_roi_pct"] == pytest.approx(0.5, abs=1e-6)


def test_sl_before_t1_is_a_full_loss():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 100.5, 100.5],
        "low":  [100.0, 98.5, 98.5],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "loss"
    assert result["final_stage"] == 0
    assert result["pnl_roi_pct"] == pytest.approx(-1.0, abs=1e-6)


def test_full_ladder_t1_t2_t3_all_hit():
    idx = pd.date_range("2026-01-01", periods=4, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 101.5, 102.5, 103.5],
        "low":  [100.0, 100.5, 101.0, 102.0],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "win"
    assert result["final_stage"] == 3
    assert result["t1_hit_at"] == idx[1]
    assert result["t2_hit_at"] == idx[2]
    assert result["pnl_roi_pct"] == pytest.approx(0.5 * 1.0 + 0.3 * 2.0 + 0.2 * 3.0, abs=1e-6)


def test_same_candle_sl_priority_over_target():
    idx = pd.date_range("2026-01-01", periods=2, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 101.5],
        "low":  [100.0, 98.0],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is not None
    assert result["status"] == "loss"
    assert result["final_stage"] == 0


def test_still_open_returns_none():
    idx = pd.date_range("2026-01-01", periods=2, freq="15min")
    df = pd.DataFrame({
        "high": [100.0, 100.3],
        "low":  [100.0, 99.8],
    }, index=idx)
    df_full = pd.concat([df, df.iloc[[-1]]])
    result = check_target_ladder(
        "LONG", entry_price=100.0, sl_price=99.0,
        tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        df=df_full, entry_candle_cutoff=idx[0],
    )
    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_outcome_target_ladder.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `check_target_ladder` in `outcome_check.py`**

Append to `outcome_check.py`:

```python
def check_target_ladder(
    direction: str,
    entry_price: float,
    sl_price: float,
    tp1_price: float,
    tp2_price: float,
    tp3_price: float,
    df: pd.DataFrame,
    entry_candle_cutoff,
    close_fracs: tuple[float, float, float] = (0.5, 0.3, 0.2),
    move_sl_to_breakeven_after_t1: bool = True,
) -> dict | None:
    """
    Walks closed candles after entry_candle_cutoff, SL-first same-candle
    tie-break (same convention as check_tp_sl). Realizes close_fracs[n] of
    the position at each target in sequence (T1 then T2 then T3, one stage
    per candle); reaching T3 fully closes. Moves the stop to entry_price
    once T1 fills, if move_sl_to_breakeven_after_t1.

    Returns None while still open, else:
    {"status": "win"|"loss", "pnl_roi_pct": float,
     "t1_hit_at": Timestamp|None, "t2_hit_at": Timestamp|None,
     "closed_at": Timestamp, "final_stage": 0-3}
    pnl_roi_pct is the price-move percent sum, NOT leverage-scaled --
    the caller applies LEVERAGE, matching how check_tp_sl leaves leverage
    scaling to main.py's _calculate_pnl_roi. status is "loss" only when
    SL is hit before T1 ever fills; every other close realizes >= 0%
    since T1+ has already locked in profit on part of the position.
    """
    targets = [tp1_price, tp2_price, tp3_price]
    current_sl = sl_price
    stage = 0
    remaining = 1.0
    realized_pct = 0.0
    t1_hit_at = None
    t2_hit_at = None

    def _price_move_pct(exit_price: float) -> float:
        if direction == "LONG":
            return (exit_price - entry_price) / entry_price * 100.0
        return (entry_price - exit_price) / entry_price * 100.0

    for i in range(len(df) - 1):
        ts = df.index[i]
        if ts <= entry_candle_cutoff:
            continue

        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        sl_hit = (low <= current_sl) if direction == "LONG" else (high >= current_sl)
        if sl_hit:
            realized_pct += remaining * _price_move_pct(current_sl)
            status = "loss" if stage == 0 else "win"
            return {
                "status": status, "pnl_roi_pct": round(realized_pct, 4),
                "t1_hit_at": t1_hit_at, "t2_hit_at": t2_hit_at,
                "closed_at": ts, "final_stage": stage,
            }

        target_hit = (high >= targets[stage]) if direction == "LONG" else (low <= targets[stage])
        if target_hit and stage < 3:
            realized_pct += close_fracs[stage] * _price_move_pct(targets[stage])
            remaining -= close_fracs[stage]
            if stage == 0:
                t1_hit_at = ts
                if move_sl_to_breakeven_after_t1:
                    current_sl = entry_price
            elif stage == 1:
                t2_hit_at = ts
            stage += 1

            if stage == 3:
                return {
                    "status": "win", "pnl_roi_pct": round(realized_pct, 4),
                    "t1_hit_at": t1_hit_at, "t2_hit_at": t2_hit_at,
                    "closed_at": ts, "final_stage": 3,
                }

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_outcome_target_ladder.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add outcome_check.py tests/test_outcome_target_ladder.py
git commit -m "feat: add check_target_ladder for 3-target partial-exit outcomes"
```

---

### Task 15: `main.py` — rewrite `scan_and_fire_signals` (two-phase loop)

**Files:**
- Modify: `main.py:118-260` (the entire `scan_and_fire_signals` function), `main.py:38-78` (top-level config import)

**Interfaces:**
- Consumes: `strategy.detect_pending_setup`, `strategy.check_setup_confirmation`, `strategy.Signal`, `strategy.valid_trade_geometry`, `strategy.direction_slot_available`, `strategy.position_size`, `db.save_armed_setup`, `db.get_armed_setups`, `db.mark_armed_setup_fired`, `db.mark_armed_setup_expired`, `db.mark_armed_setup_invalidated`, `db.expire_old_armed_setups`, `db.save_signal` (all existing or from Tasks 9-13)

- [ ] **Step 1: Add the `Signal` dataclass's 3 new optional fields**

In `strategy.py`, modify the `Signal` dataclass (currently `strategy.py:34-49`):

```python
@dataclass
class Signal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    tp_roi_pct: float
    sl_roi_pct: float
    timeframe_summary: str
    generated_at: datetime
    rr: float
    score: float
    entry_low: float
    entry_high: float
    tp2_price: float | None = None
    tp3_price: float | None = None
    position_size: float | None = None
```

- [ ] **Step 2: Update `main.py`'s top-level import block**

Replace the `from config import (...)` block (`main.py:38-78`) — drop `TARGET_ROI_PCT`, add the new Binocular names needed for logging:

```python
from config import (
    LKT,
    LEVERAGE,
    ENTRY_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SCAN_INTERVAL_MINUTES,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNALS_PER_SCAN,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    TOP_N_COINS,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    COINGLASS_API_KEY,
    STRATEGY_NAME,
    MAX_SL_ROI_PCT,
    DRY_RUN,
    DRY_RUN_SAVE_SIGNALS,
    STRATEGY_V1_ENABLED,
    SIGNAL_MODE,
    ENTRY_BUFFER_PCT,
    PENDING_SIGNAL_EXPIRY_CANDLES,
    SCALPER_V3_ENABLED,
    SCALPER_V3_TIMEFRAME,
    SCALPER_V3_SCAN_INTERVAL_MINUTES,
    SCALPER_V3_MAX_CONCURRENT_SIGNALS,
    SCALPER_V3_SIGNAL_COOLDOWN_MINUTES,
    SCALPER_V3_EXPIRE_HOURS,
    SCALPER_V3_MAX_DAILY_SIGNALS,
    SCALPER_V3_MIN_DAILY_SIGNAL_GAP_MINUTES,
    STRATEGY_NAME_V3,
    LIVE_ENABLED,
)
```

Also replace `from outcome_check import check_tp_sl` with `from outcome_check import check_tp_sl, check_target_ladder`.

- [ ] **Step 3: Rewrite `scan_and_fire_signals`**

Replace the entire function body (`main.py:118-260`) with:

```python
async def scan_and_fire_signals(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_armed_setups(now)

    # ── Phase 1: process existing pending (armed) setups ────────────
    armed = db.get_armed_setups(limit=len(coins) + 20)
    armed_symbols = {s["symbol"] for s in armed}

    active_signals = db.count_active_signals()
    active_long = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig = db.latest_signal_time()

    for setup in armed:
        status, fill_price = strategy.check_setup_confirmation(setup)

        if status == "expired":
            db.mark_armed_setup_expired(setup["id"])
            continue
        if status == "invalidated":
            db.mark_armed_setup_invalidated(setup["id"], reason="opposite_transition_or_sl")
            continue
        if status == "waiting":
            continue

        # status == "confirmed"
        slots = MAX_CONCURRENT_SIGNALS - active_signals
        daily_ok = signals_today < MAX_DAILY_SIGNALS
        gap_ok = (
            last_sig is None
            or (now - last_sig).total_seconds() >= MIN_DAILY_SIGNAL_GAP_MINUTES * 60
        )
        direction_ok = strategy.direction_slot_available(setup["direction"], active_long, active_short)

        if slots <= 0 or not daily_ok or not gap_ok or not direction_ok:
            db.mark_armed_setup_missed(setup["id"], reason="budget_or_slot_unavailable")
            continue

        tp_roi, sl_roi = strategy._roi_pct(setup["direction"], fill_price, setup["tp_price"], setup["sl_price"])
        sig = strategy.Signal(
            symbol=setup["symbol"],
            direction=setup["direction"],
            entry_price=fill_price,
            tp_price=setup["tp_price"],
            sl_price=setup["sl_price"],
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary=setup.get("setup_reason", ""),
            generated_at=now,
            rr=setup["rr"],
            score=setup["score"],
            entry_low=fill_price,
            entry_high=fill_price,
            tp2_price=setup.get("tp2_price"),
            tp3_price=setup.get("tp3_price"),
            position_size=setup.get("position_size"),
        )

        if not strategy.valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            db.mark_armed_setup_invalidated(setup["id"], reason="invalid_geometry_at_confirm")
            continue

        if DRY_RUN and not DRY_RUN_SAVE_SIGNALS:
            logger.info(
                "[DRY-RUN] Would confirm | %s %s @ %.6g TP1=%.6g TP2=%.6g TP3=%.6g SL=%.6g RR=%.2f",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price,
                sig.tp2_price, sig.tp3_price, sig.sl_price, sig.rr,
            )
            db.mark_armed_setup_fired(setup["id"], signal_id=-1)
            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1
            continue

        try:
            signal_id = db.save_signal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                tp_price=sig.tp_price,
                sl_price=sig.sl_price,
                leverage=sig.leverage,
                generated_at=sig.generated_at,
                strategy_name=STRATEGY_NAME,
                score=sig.score,
                rr=sig.rr,
                entry_timeframe=ENTRY_TF,
                trend_timeframe=ENTRY_TF,
                setup_reason=sig.timeframe_summary,
                tp2_price=sig.tp2_price,
                tp3_price=sig.tp3_price,
                position_size=sig.position_size,
            )
            db.mark_armed_setup_fired(setup["id"], signal_id)

            if not DRY_RUN:
                await tg.broadcast_signal(app, sig, signal_id)

            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1

            logger.info(
                "[SIGNAL] Confirmed #%d %s %s score=%.1f entry=%.6g tp1=%.6g tp2=%.6g tp3=%.6g sl=%.6g rr=%.2f",
                signal_id, sig.symbol, sig.direction, sig.score,
                sig.entry_price, sig.tp_price, sig.tp2_price, sig.tp3_price, sig.sl_price, sig.rr,
            )
        except Exception as e:
            logger.error("[SCAN] Failed to confirm setup for %s: %s", setup["symbol"], e, exc_info=True)

    # ── Phase 2: scan for new pending setups ────────────────────────
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    to_scan = [
        s for s in coins
        if s not in armed_symbols and not db.signal_exists_for_coin(s, cooldown_since)
    ]

    reject_maps = [dict() for _ in to_scan]
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(
                lambda i: strategy.detect_pending_setup(to_scan[i], reject_sink=reject_maps[i]),
                range(len(to_scan)),
            )),
        )

    new_setups = [s for s in results if s is not None]

    reject_counts: dict[str, int] = {}
    for m in reject_maps:
        for k, v in m.items():
            reject_counts[k] = reject_counts.get(k, 0) + v
    reject_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(reject_counts.items(), key=lambda kv: -kv[1])
    ) or "none"

    for setup in new_setups:
        try:
            db.save_armed_setup(setup)
            logger.info(
                "[PENDING] Armed %s %s entry=%.6g sl=%.6g tp1=%.6g score=%.1f rr=%.2f",
                setup["symbol"], setup["direction"], setup["trigger_price"],
                setup["sl_price"], setup["tp_price"], setup["score"], setup["rr"],
            )
        except Exception as e:
            logger.error("[SCAN] Failed to arm setup for %s: %s", setup["symbol"], e, exc_info=True)

    logger.info(
        "[SCAN] Done — %d armed processed, %d/%d coins scanned for new setups, %d new pending | rejects: %s",
        len(armed), len(to_scan), len(coins), len(new_setups), reject_summary,
    )
```

- [ ] **Step 4: Verify the module imports cleanly**

Run: `python -c "import main"`
Expected: no errors (this will exercise `strategy.py`, `config.py`, `database.py`, `outcome_check.py` import chains too).

- [ ] **Step 5: Commit**

```bash
git add main.py strategy.py
git commit -m "feat: rewrite scan_and_fire_signals for the two-phase pending-breakout loop"
```

---

### Task 16: `main.py` — rewrite `check_outcomes` and startup logging

**Files:**
- Modify: `main.py:265-346` (`_calculate_pnl_roi` and `check_outcomes`), `main.py:523-535` (startup log lines)

**Interfaces:**
- Consumes: `outcome_check.check_target_ladder` (Task 14), `db.mark_signal_tp2_hit` (Task 10), `db.mark_signal_breakeven_triggered` (existing), `tg.notify_target_progress` (Task 17)

- [ ] **Step 1: Rewrite `check_outcomes`**

Replace `main.py:287-346` (the body of `check_outcomes` only — **keep** the existing `_calculate_pnl_roi` helper at `main.py:265-284` unchanged, it's reused below) with:

```python
async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        entry_price = sig["entry_price"]
        sl_price = sig["sl_price"]
        tp1_price = sig["tp_price"]
        tp2_price = sig.get("tp2_price")
        tp3_price = sig.get("tp3_price")

        if tp2_price is None or tp3_price is None:
            # Pre-migration or non-Binocular row (e.g. a leftover v1 row) --
            # fall back to plain TP/SL so it can still resolve to
            # win/loss/expired instead of hanging forever.
            if not strategy.valid_trade_geometry(direction, entry_price, tp1_price, sl_price):
                db.update_signal_outcome(sig["id"], "expired", 0.0)
                continue
            generated = datetime.fromisoformat(sig["generated_at"])
            if generated.tzinfo is None:
                generated = generated.replace(tzinfo=timezone.utc)
            if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
                db.update_signal_outcome(sig["id"], "expired", 0.0)
                if not DRY_RUN:
                    await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
                continue
            elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
            fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3
            try:
                df = get_market_klines(symbol, ENTRY_TF, count=fetch_count)
                if df is None or df.empty or len(df) < 2:
                    continue
            except Exception as e:
                logger.warning("Could not fetch candles for %s: %s", symbol, e)
                continue
            entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)
            outcome = check_tp_sl(direction, entry_price, tp1_price, sl_price, df, entry_candle_cutoff)
            if outcome is None:
                continue
            pnl = _calculate_pnl_roi(direction, outcome, entry_price, tp1_price, sl_price)
            db.update_signal_outcome(sig["id"], outcome, pnl)
            if not DRY_RUN:
                await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
            continue

        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info("Signal %s expired (%s)", sig["id"], symbol)
            if not DRY_RUN:
                try:
                    await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
                except Exception as e:
                    logger.error("Failed to notify expiry for %s: %s", symbol, e)
            continue

        elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_market_klines(symbol, ENTRY_TF, count=fetch_count)
            if df is None or df.empty or len(df) < 2:
                continue
        except Exception as e:
            logger.warning("Could not fetch candles for %s: %s", symbol, e)
            continue

        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)

        result = check_target_ladder(
            direction, entry_price, sl_price, tp1_price, tp2_price, tp3_price,
            df, entry_candle_cutoff,
        )
        if result is None:
            continue

        pnl = result["pnl_roi_pct"] * LEVERAGE

        if result["t1_hit_at"] is not None and sig.get("tp1_hit_at") is None:
            db.mark_signal_tp1_hit(sig["id"], result["t1_hit_at"])
            db.mark_signal_breakeven_triggered(sig["id"], result["t1_hit_at"])
            if not DRY_RUN:
                await tg.notify_target_progress(app, {**sig}, stage=1)
        if result["t2_hit_at"] is not None and sig.get("tp2_hit_at") is None:
            db.mark_signal_tp2_hit(sig["id"], result["t2_hit_at"])
            if not DRY_RUN:
                await tg.notify_target_progress(app, {**sig}, stage=2)

        db.update_signal_outcome(sig["id"], result["status"], pnl)
        logger.info(
            "Signal %s %s (%s) stage=%d %+.1f%%",
            sig["id"], result["status"].upper(), symbol, result["final_stage"], pnl,
        )

        if not DRY_RUN:
            try:
                await tg.notify_outcome(app, {**sig, "status": result["status"], "pnl_roi": pnl, "final_stage": result["final_stage"]})
            except Exception as e:
                logger.error("Failed to notify %s for %s: %s", result["status"], symbol, e)
```

- [ ] **Step 2: Update startup logging**

Replace `main.py:524-528`:
```python
    logger.info("Starting MEXC Signal Bot")
    logger.info("Strategy: %s", STRATEGY_NAME)
    logger.info("Entry TF: %s", ENTRY_TF)
    logger.info("Target ROI: %.0f%%", TARGET_ROI_PCT)
    logger.info("Max SL ROI: %.0f%%", MAX_SL_ROI_PCT)
```
with:
```python
    logger.info("Starting MEXC Signal Bot")
    logger.info("Strategy: %s", STRATEGY_NAME)
    logger.info("Entry TF: %s", ENTRY_TF)
    logger.info("Signal mode: %s", SIGNAL_MODE)
    logger.info("Entry buffer: %.4f%%", ENTRY_BUFFER_PCT * 100)
    logger.info("Pending expiry: %d candles", PENDING_SIGNAL_EXPIRY_CANDLES)
    logger.info("Max SL ROI: %.0f%%", MAX_SL_ROI_PCT)
```

- [ ] **Step 3: Verify the module imports cleanly**

Run: `python -c "import main"`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: rewrite check_outcomes for 3-target partial-exit ladder"
```

---

### Task 17: `bot.py` — signal formatting, target progress, outcome labeling

**Files:**
- Modify: `bot.py:62-163` (`format_signal` through `notify_outcome`)
- Test: `tests/test_bot_formatting.py`

**Interfaces:**
- Produces: `notify_target_progress(app, signal_db: dict, stage: int) -> None`
- Modifies: `format_signal(signal, signal_id: int) -> str` (shows 3 targets, EMA ribbon state, VWAP/MTF when `SIGNAL_MODE=strict`, position size), `notify_outcome(app, signal_db: dict) -> None` (labels partial-ladder results)

- [ ] **Step 1: Write the failing tests**

Modify `tests/test_bot_formatting.py` — replace `_sample_signal` and add new tests:

```python
from datetime import datetime, timezone

import bot
from bot import format_signal
from strategy import Signal


def _sample_signal() -> Signal:
    return Signal(
        symbol="XRP_USDT",
        direction="LONG",
        entry_price=1.100000,
        tp_price=1.108250,
        sl_price=1.095200,
        leverage=20,
        tp_roi_pct=15.0,
        sl_roi_pct=8.7,
        timeframe_summary="Binocular confirmed trigger",
        generated_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        rr=1.72,
        score=82.5,
        entry_low=1.100000,
        entry_high=1.100000,
        tp2_price=1.116500,
        tp3_price=1.124750,
        position_size=450.25,
    )


def test_format_signal_contains_key_fields(monkeypatch):
    monkeypatch.setattr(bot, "STRATEGY_NAME", "Binocular Pending-Breakout v1")
    msg = format_signal(_sample_signal(), signal_id=12)

    assert "XRP/USDT" in msg
    assert "LONG" in msg
    assert "1.1" in msg
    assert "1:1.72" in msg
    assert "20x" in msg
    assert "Binocular Pending-Breakout v1" in msg
    assert "12" in msg


def test_format_signal_shows_all_three_targets():
    msg = format_signal(_sample_signal(), signal_id=13)
    assert "1.1082" in msg or "1.10825" in msg
    assert "1.1165" in msg
    assert "1.1247" in msg or "1.12475" in msg


def test_format_signal_shows_position_size():
    msg = format_signal(_sample_signal(), signal_id=14)
    assert "450.25" in msg or "450.2" in msg


def test_format_signal_short_uses_red_arrow():
    sig = _sample_signal()
    sig.direction = "SHORT"
    msg = format_signal(sig, signal_id=15)
    assert "SHORT" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_bot_formatting.py -v`
Expected: FAIL — `format_signal` doesn't reference `tp2_price`/`tp3_price`/`position_size` yet, so the new assertions fail.

- [ ] **Step 3: Rewrite `format_signal` and add `notify_target_progress`**

Replace `bot.py:62-80` (`format_signal` and `broadcast_signal`):

```python
def format_signal(signal, signal_id: int) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin  = signal.symbol.replace("_", "/")

    lines = [
        f"{escape(arrow)} — {_bold(coin)} Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    {_code(f'{signal.entry_price:,.6g}')}",
        f"🎯 T1 (50%): {_code(f'{signal.tp_price:,.6g}')}  {_italic(f'+{signal.tp_roi_pct:.1f}% gross ROI')}",
    ]
    if signal.tp2_price is not None:
        lines.append(f"🎯 T2 (30%): {_code(f'{signal.tp2_price:,.6g}')}")
    if signal.tp3_price is not None:
        lines.append(f"🎯 T3 (20%): {_code(f'{signal.tp3_price:,.6g}')}")
    lines.append(f"🛑 SL:       {_code(f'{signal.sl_price:,.6g}')}  {_italic(f'-{signal.sl_roi_pct:.1f}% gross ROI')}")
    lines.append(f"📊 RR:       {_code(f'1:{signal.rr:.3g}')} (to T1)")
    lines.append(f"⚡ Leverage: {_code(f'{signal.leverage}x')}  {_italic('Isolated')}")
    if signal.position_size is not None:
        lines.append(f"📦 Position size: {_code(f'{signal.position_size:,.4g}')}")
    lines.append(f"🧭 Setup:    {_italic(escape(signal.timeframe_summary))}")
    lines.append(f"📈 Strategy: {STRATEGY_NAME}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ {_code(signal.generated_at.astimezone(LKT).strftime('%Y-%m-%d %H:%M LKT'))}")
    lines.append(f"🆔 Signal ID: {_code(signal_id)}")
    lines.append(_italic("⚠️ Not financial advice. Use risk management."))
    return "\n".join(lines)


async def broadcast_signal(app: Application, signal, signal_id: int) -> None:
    msg = format_signal(signal, signal_id)
    await _send_html(app, msg)


async def notify_target_progress(app: Application, signal_db: dict, stage: int) -> None:
    """Sent when T1 (50% closed, SL moved to breakeven) or T2 (30% more
    closed) fills. Replies to the original signal message when available."""
    direction = signal_db["direction"]
    symbol = signal_db["symbol"].replace("_", "/")
    arrow = "🟢" if direction == "LONG" else "🔴"

    if stage == 1:
        label = "T1 hit — 50% closed, SL moved to breakeven"
    else:
        label = "T2 hit — 30% more closed"

    msg = "\n".join([
        f"📶 {_bold('Target Progress')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        _code(label),
        f"🆔 Signal ID: {_code(signal_db['id'])}",
    ])
    await _send_html(app, msg, reply_to_message_id=signal_db.get("signal_message_id"))
```

- [ ] **Step 4: Rewrite `notify_outcome`**

Replace `bot.py:142-163` (the existing `notify_outcome`):

```python
async def notify_outcome(app: Application, signal_db: dict) -> None:
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    status    = signal_db["status"]
    roi       = signal_db.get("pnl_roi") or 0.0
    final_stage = signal_db.get("final_stage")

    if status == "win" and final_stage == 3:
        emoji, label = "✅", f"FULL TARGET (T1+T2+T3) {roi:+.1f}%"
    elif status == "win" and final_stage == 2:
        emoji, label = "✅", f"T1+T2 HIT, THEN STOPPED {roi:+.1f}%"
    elif status == "win" and final_stage == 1:
        emoji, label = "✅", f"T1 HIT, BE STOP {roi:+.1f}%"
    elif status == "win":
        emoji, label = "✅", f"TARGET HIT {roi:+.1f}%"
    elif status == "loss":
        emoji, label = "❌", f"STOPPED OUT {roi:+.1f}%"
    else:
        emoji, label = "💤", "EXPIRED"

    arrow = "🟢" if direction == "LONG" else "🔴"
    msg = "\n".join([
        f"{emoji} {_bold('Signal Closed')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        f"Result: {_code(label)}",
        f"🆔 ID: {_code(signal_db['id'])}",
    ])
    await _send_html(app, msg)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_bot_formatting.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add bot.py tests/test_bot_formatting.py
git commit -m "feat: show 3 targets/position size in Telegram, add target-progress pings"
```

---

### Task 18: `bot.py` — update `cmd_status`

**Files:**
- Modify: `bot.py:205-260` (`cmd_status`)

- [ ] **Step 1: Replace the config import and message lines**

Replace `bot.py:208-222`:

```python
    from config import (
        STRATEGY_NAME,
        ENTRY_TF,
        SIGNAL_MODE, ENTRY_BUFFER_PCT, PENDING_SIGNAL_EXPIRY_CANDLES,
        MAX_SL_ROI_PCT,
        MIN_RR,
        SCAN_INTERVAL_MINUTES,
        OUTCOME_CHECK_MINUTES,
        MAX_CONCURRENT_SIGNALS, MAX_ACTIVE_LONG_SIGNALS, MAX_ACTIVE_SHORT_SIGNALS,
        SIGNAL_COOLDOWN_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        LEVERAGE, COINGLASS_API_KEY,
        TOP_N_COINS, COIN_POOL_MIN_VOLUME_USD, COIN_POOL_MIN_SELECTED,
        SIGNAL_EXPIRE_HOURS,
    )
```

Replace the two `TF:`/`Confirm:` lines and the `TP target:` line (currently `bot.py:243-247`):

```python
        f"TF:          {_code(ENTRY_TF)}  (SIGNAL_MODE={SIGNAL_MODE})",
        f"Entry buf:   {_code(f'{ENTRY_BUFFER_PCT*100:.3f}%')}  {_italic(f'expires after {PENDING_SIGNAL_EXPIRY_CANDLES} candles')}",
        f"SL cap:      {_code(f'{MAX_SL_ROI_PCT:.0f}% ROI')}",
        f"RR min:      {_code(f'1:{MIN_RR:.2g}')} (to T1)",
        f"Leverage:    {_code(f'{LEVERAGE}x  Isolated')}",
```

(Drop the old `TP target: ... TARGET_ROI_PCT` line — no fixed-%-target concept exists in this strategy; T1/T2/T3 are structural and shown per-signal instead.)

- [ ] **Step 2: Verify the module imports and runs**

Run: `python -c "import bot"`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add bot.py
git commit -m "feat: update /status to show SIGNAL_MODE, entry buffer, pending expiry"
```

---

### Task 19: `webui.py` — config and pending-setups panel (Python)

**Files:**
- Modify: `webui.py:232-260` (`get_strategy_config`), `webui.py:262-` (`build_payload`)

- [ ] **Step 1: Rewrite `get_strategy_config`**

Replace `webui.py:232-259`:

```python
def get_strategy_config() -> dict:
    """Return dashboard-safe strategy/runtime configuration for Binocular Pending-Breakout v1."""
    return {
        "strategy": _safe_config_value("STRATEGY_NAME", "Binocular Pending-Breakout v1"),
        "entry_tf": _safe_config_value("ENTRY_TF", "—"),
        "signal_mode": _safe_config_value("SIGNAL_MODE", "—"),
        "confirmation_timeframes": _safe_config_value("CONFIRMATION_TIMEFRAMES", "—"),
        "mtf_min_confirmations": _safe_config_value("MTF_MIN_CONFIRMATIONS", "—"),
        "entry_buffer_pct": _safe_config_value("ENTRY_BUFFER_PCT", "—"),
        "pending_signal_expiry_candles": _safe_config_value("PENDING_SIGNAL_EXPIRY_CANDLES", "—"),

        "account_balance": _safe_config_value("ACCOUNT_BALANCE", "—"),
        "risk_percent_per_trade": _safe_config_value("RISK_PERCENT_PER_TRADE", "—"),

        "top_n_coins": _safe_config_value("TOP_N_COINS", "—"),
        "min_volume_usd": _safe_config_value("COIN_POOL_MIN_VOLUME_USD", "—"),

        "max_sl_roi_pct": _safe_config_value("MAX_SL_ROI_PCT", "—"),
        "min_rr": _safe_config_value("MIN_RR", "—"),
        "leverage": _safe_config_value("LEVERAGE", "—"),

        "max_daily_signals": _safe_config_value("MAX_DAILY_SIGNALS", "—"),
        "min_daily_signal_gap_minutes": _safe_config_value("MIN_DAILY_SIGNAL_GAP_MINUTES", "—"),
        "max_concurrent_signals": _safe_config_value("MAX_CONCURRENT_SIGNALS", "—"),
        "max_active_long_signals": _safe_config_value("MAX_ACTIVE_LONG_SIGNALS", "—"),
        "max_active_short_signals": _safe_config_value("MAX_ACTIVE_SHORT_SIGNALS", "—"),
        "cooldown_minutes": _safe_config_value("SIGNAL_COOLDOWN_MINUTES", "—"),
        "scan_workers": _safe_config_value("SCAN_WORKERS", "—"),

        "crypto_futures_only": _safe_config_value("CRYPTO_FUTURES_ONLY", True),
        "dry_run": _safe_config_value("DRY_RUN", True),
    }


def get_pending_setups() -> list[dict]:
    import database as db
    return db.get_armed_setups(limit=50)
```

- [ ] **Step 2: Wire `pending_setups` into `build_payload`**

In `build_payload` (`webui.py:262-` onward), add a `"pending_setups": get_pending_setups(),` entry to the returned dict, alongside the existing `"recent"`/`"runtime"`/`"config"` keys.

- [ ] **Step 3: Verify the module imports**

Run: `python -c "import webui"`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add webui.py
git commit -m "feat: expose Binocular config and pending setups on the dashboard payload"
```

---

### Task 20: `webui.py` — dashboard JS update

**Files:**
- Modify: the inline dashboard JS in `webui.py` (search for `cfg-tf`, `cfg-confirm`, `cfg-confirm-sub` — the fields the 2026-07-29 migration introduced for the ribbon/trend-bar display)

- [ ] **Step 1: Locate the JS fields**

Run: `grep -n "cfg-tf\|cfg-confirm" webui.py` (or use the Grep tool) to find every place the dashboard JS reads `c.entry_tf`, a ribbon/trend-bar-specific field, or `c.trend_bar_pac_length`/`c.ribbon_baseline_len`/`c.ribbon_lookback_bars`.

- [ ] **Step 2: Update the JS to read the new fields**

Replace whatever JS currently sets the "Confirm" sub-label from `c.ribbon_baseline_len`/`c.trend_bar_pac_length` with code that reads `c.signal_mode` and, when `c.signal_mode === "strict"`, also `c.confirmation_timeframes`/`c.mtf_min_confirmations`. Example (adapt exact selector names to what Step 1 found):

```javascript
document.getElementById('cfg-tf').textContent = c.entry_tf + ' (' + c.signal_mode + ')';
var confirmSub = c.signal_mode === 'strict'
  ? 'VWAP + ' + c.mtf_min_confirmations + '/3 of ' + c.confirmation_timeframes
  : c.signal_mode === 'confirmed'
    ? 'EMA ribbon + EMA200'
    : 'raw trigger only';
document.getElementById('cfg-confirm-sub').textContent = confirmSub;
```

- [ ] **Step 3: Add a pending-setups list to the dashboard**

Add a small table/list section rendering `payload.pending_setups` (symbol, direction, entry, SL, T1, score, created_at) — reuse whatever list/table rendering pattern the dashboard already uses for `payload.recent` (recent signals).

- [ ] **Step 4: Manually verify in a browser**

Run: `python webui.py` (or restart the `mexc-dashboard` service if testing on the server), then open `http://localhost:6060/?token=<WEBUI_TOKEN>` and confirm:
- The strategy card shows `SIGNAL_MODE` correctly, no `undefined` anywhere.
- A "Pending Setups" section renders (empty is fine if none are armed yet).

- [ ] **Step 5: Commit**

```bash
git add webui.py
git commit -m "feat: update dashboard JS for SIGNAL_MODE and pending setups"
```

---

### Task 21: Legacy cleanup — remove old ribbon-flip code and superseded tests

**Files:**
- Modify: `strategy.py` (remove `calculate_trend_bar`, `_detect_ribbon_flip`, the old `_calculate_tp_sl`, `_score_candidate`, `evaluate_symbol`)
- Delete: `tests/test_ribbon_trendbar_indicators.py`, `tests/test_strategy_ribbon_trendbar.py`
- Modify: `tests/strategy_fixtures.py` (remove `make_ribbon_trendbar_df` if nothing else uses it after the deletions above)

- [ ] **Step 1: Confirm nothing outside `strategy.py` still calls the functions being removed**

Run: `grep -rn "calculate_trend_bar\|_detect_ribbon_flip\|evaluate_symbol" --include="*.py" .` (excluding `venv/`)
Expected: only matches inside `strategy.py` itself and the two test files being deleted in Step 3. If `scripts/backtest_simple_strategy.py` still calls `evaluate_symbol`, stop here — Task 22 must land first (it replaces those call sites).

- [ ] **Step 2: Remove the superseded functions from `strategy.py`**

Delete `calculate_trend_bar` (the function, not `calculate_ema_ribbon`), `_detect_ribbon_flip`, the old `_calculate_tp_sl`, `_score_candidate`, and the old `evaluate_symbol` (everything from the `# ── evaluate_symbol pipeline` comment through the end of the old `evaluate_symbol` function body) — these are fully superseded by `detect_pending_setup`/`check_setup_confirmation`/`_build_pending_setup`/`_score_pending_setup` from Tasks 11-13. Keep `valid_trade_geometry`, `direction_slot_available`, `_calc_rr`, `_roi_pct`, `_bump` — all still used by the new pipeline / `main.py`.

- [ ] **Step 3: Delete the superseded test files**

```bash
git rm tests/test_ribbon_trendbar_indicators.py tests/test_strategy_ribbon_trendbar.py
```

- [ ] **Step 4: Remove the now-unused `make_ribbon_trendbar_df` fixture**

In `tests/strategy_fixtures.py`, confirm no remaining test imports `make_ribbon_trendbar_df` (`grep -rn "make_ribbon_trendbar_df" tests/`), then delete that function from `tests/strategy_fixtures.py`. Keep `make_15m_trend_df` and `patch_klines` — both are reused by the new Binocular tests.

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests pass, no import errors, no leftover references to deleted functions.

- [ ] **Step 6: Commit**

```bash
git add strategy.py tests/strategy_fixtures.py
git commit -m "chore: remove ribbon-flip-only trigger code superseded by Binocular pipeline"
```

---

### Task 22: Rewrite `scripts/backtest_simple_strategy.py` for the two-phase/ladder model

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (whole-file rewrite of `backtest_symbol`, `Trade`/`BacktestStats`, module-level imports)

**Interfaces:**
- Consumes: `strategy.detect_pending_setup`, `strategy.check_setup_confirmation`, `outcome_check.check_target_ladder`

- [ ] **Step 1: Replace the module-level imports**

Replace:
```python
import strategy
from mexc_client import get_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS,
)
```
with:
```python
import strategy
from outcome_check import check_target_ladder
from mexc_client import get_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD,
    RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH, PENDING_SIGNAL_EXPIRY_CANDLES,
)
```

- [ ] **Step 2: Replace the `Trade` dataclass**

Replace the `Trade` dataclass fields to carry ladder outcomes:

```python
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
    final_stage: int = 0
    t1_hit: bool = False
    t2_hit: bool = False
    t3_hit: bool = False
    closed_at: str = ""
```

- [ ] **Step 3: Replace `backtest_symbol`**

Replace the entire `backtest_symbol` function:

```python
def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process. Two-phase simulation: an armed
    pending setup (from strategy.detect_pending_setup, as-of each bar)
    waits for a breakout confirmation (as strategy.check_setup_confirmation
    would live), then check_target_ladder walks the 3-target partial-exit
    ladder forward from the confirming bar. One setup/trade at a time,
    same as the single-timeframe walk-forward this script already used."""
    trades: list[Trade] = []

    df_full = get_klines_extended(symbol, ENTRY_TF, days)
    if df_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(f"[{symbol}] achieved history: {len(df_full)} x {ENTRY_TF} bars", flush=True)

    min_start = max(
        RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD,
        RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH,
    ) + 10

    original_get_market_klines = strategy.get_market_klines
    pending_setup: dict | None = None
    in_trade_until_idx = -1

    try:
        for i in range(min_start, len(df_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of = _with_forming_row(df_full, i, ENTRY_KLINE_COUNT)

            def _fake(sym: str, interval: str, count: int = 100, _df=as_of):
                if interval == ENTRY_TF:
                    return _df
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            if pending_setup is not None:
                status, fill_price = strategy.check_setup_confirmation(pending_setup)
                if status == "expired" or status == "invalidated":
                    pending_setup = None
                    continue
                if status == "waiting":
                    continue

                # confirmed
                entry_candle_cutoff = df_full.index[i]
                result = check_target_ladder(
                    pending_setup["direction"], fill_price, pending_setup["sl_price"],
                    pending_setup["tp_price"], pending_setup["tp2_price"], pending_setup["tp3_price"],
                    df_full, entry_candle_cutoff,
                )
                bars_held = 1
                if result is None:
                    # Ran off the end of available history -- treat as expired.
                    outcome, final_stage = "expired", 0
                    gross_roi_pct = 0.0
                    closed_at_str = str(df_full.index[i])
                else:
                    outcome = result["status"]
                    final_stage = result["final_stage"]
                    gross_roi_pct = result["pnl_roi_pct"]
                    closed_idx = df_full.index.get_loc(result["closed_at"])
                    bars_held = max(1, closed_idx - i)
                    closed_at_str = str(result["closed_at"])

                from config import LEVERAGE
                gross_roi = gross_roi_pct * LEVERAGE
                cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
                net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi

                trades.append(Trade(
                    symbol=symbol, direction=pending_setup["direction"], entry_price=fill_price,
                    tp_price=pending_setup["tp_price"], sl_price=pending_setup["sl_price"],
                    rr=pending_setup["rr"], outcome=outcome,
                    gross_roi_pct=round(gross_roi, 3), net_roi_pct=round(net_roi, 3),
                    final_stage=final_stage,
                    t1_hit=final_stage >= 1, t2_hit=final_stage >= 2, t3_hit=final_stage >= 3,
                    closed_at=closed_at_str,
                ))
                in_trade_until_idx = i + bars_held
                pending_setup = None
                continue

            setup = strategy.detect_pending_setup(symbol)
            if setup is not None:
                setup["created_at"] = df_full.index[i].isoformat()
                pending_setup = setup
    finally:
        strategy.get_market_klines = original_get_market_klines

    return trades
```

- [ ] **Step 4: Add T1/T2/T3 hit-rate and monthly-performance reporting to `BacktestStats.print_report`**

After the existing `_bucket_report("SHORT", shorts)` call in `print_report`, add:

```python
        t1_rate = sum(1 for t in self.trades if t.t1_hit) / n * 100
        t2_rate = sum(1 for t in self.trades if t.t2_hit) / n * 100
        t3_rate = sum(1 for t in self.trades if t.t3_hit) / n * 100
        print(f"\nT1 hit rate:          {t1_rate:.1f}%")
        print(f"T2 hit rate:          {t2_rate:.1f}%")
        print(f"T3 hit rate:          {t3_rate:.1f}%")

        print("\nMonthly performance:")
        from collections import defaultdict
        by_month: dict[str, list[Trade]] = defaultdict(list)
        for t in self.trades:
            if t.closed_at:
                month_key = t.closed_at[:7]  # "YYYY-MM"
                by_month[month_key].append(t)
        for month_key in sorted(by_month):
            _bucket_report(f"  {month_key}", by_month[month_key])
```

(`t.closed_at` is populated by Task 22 Step 3's `Trade(...)` construction — the `closed_at` field added to the `Trade` dataclass in Step 2.)

- [ ] **Step 5: Run a dry smoke test against the module (no network)**

Run: `python -c "import scripts.backtest_simple_strategy"`
Expected: no import errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "feat: rewrite backtest script for two-phase pending-breakout + target-ladder model"
```

---

### Task 23: Backtest `SIGNAL_MODE=strict` MTF support

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (extend the `_fake` closure and `backtest_symbol` for strict mode)

- [ ] **Step 1: Extend the fake-klines closure to serve confirmation timeframes**

In `backtest_symbol` (Task 22), before the main loop, add:

```python
    from config import SIGNAL_MODE, CONFIRMATION_TIMEFRAMES
    confirmation_dfs: dict[str, pd.DataFrame] = {}
    if SIGNAL_MODE == "strict":
        for tf in [t.strip() for t in CONFIRMATION_TIMEFRAMES.split(",") if t.strip()]:
            confirmation_dfs[tf] = get_klines_extended(symbol, tf, days)
```

Then replace the `_fake` closure inside the loop:

```python
            def _fake(sym: str, interval: str, count: int = 100, _df=as_of, _ts=df_full.index[i]):
                if interval == ENTRY_TF:
                    return _df
                if interval in confirmation_dfs and not confirmation_dfs[interval].empty:
                    tf_df = confirmation_dfs[interval]
                    as_of_tf = tf_df[tf_df.index <= _ts]
                    if as_of_tf.empty:
                        return pd.DataFrame()
                    return pd.concat([as_of_tf, as_of_tf.iloc[[-1]]])
                return pd.DataFrame()
```

- [ ] **Step 2: Verify the module still imports**

Run: `python -c "import scripts.backtest_simple_strategy"`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "feat: support SIGNAL_MODE=strict multi-timeframe confirmation in backtests"
```

---

### Task 24: Full verification and backtest comparison

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests pass, 0 failures, 0 errors.

- [ ] **Step 2: Verify every module imports cleanly**

Run: `python -c "import config; import strategy; import main; import bot; import webui; import database; import outcome_check"`
Expected: no errors.

- [ ] **Step 3: Run the backtest against the 2026-07-27 baseline's symbol set**

Run: `python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT WLD_USDT SOL_USDT SUI_USDT PEPE_USDT NEAR_USDT APT_USDT INJ_USDT --days 180`

Record: total trades, win rate, LONG vs SHORT win rate, T1/T2/T3 hit rates, net ROI, max drawdown, max consecutive losses.

- [ ] **Step 4: Compare against the 2026-07-27 baseline**

The 2026-07-27 "Binocular Trend Confluence v1" attempt (per its own spec) produced 44 trades over 6 months/10 symbols with a 30.8%/16.7% LONG/SHORT win-rate split. Write a short comparison note (trade count, win rate, LONG/SHORT split, net ROI) against this run's output. **Do not merge to `main` if this comparison looks worse or no better than the baseline** — that is exactly the failure mode this strategy was pulled for once already.

- [ ] **Step 5: Dry-run boot check**

Run: `DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py` for a few minutes (or one scan cycle), then stop it (Ctrl-C).
Expected: startup logs show `Strategy: Binocular Pending-Breakout v1`, `Signal mode: confirmed`, entry buffer, pending expiry candles, max SL ROI, dry-run enabled — no exceptions during a scan cycle or outcome-check cycle.

- [ ] **Step 6: Report the comparison to the user and await a merge decision**

Do not run `git push` to `main` or open a PR against `main` as part of this plan. Summarize the backtest comparison and let the user decide whether to proceed with merging.

---
