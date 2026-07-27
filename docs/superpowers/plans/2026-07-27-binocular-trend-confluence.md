# Binocular Trend Confluence v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bot's active strategy (`strategy.py`) with a new strategy derived from the "Binocular" Pine Script indicator — 15m Supply/Demand zones + 5m Chandelier Exit / PVT / dual-RSI momentum trigger — while keeping the BTC safety filter, `Signal`/`BtcContext` dataclasses, `evaluate_symbol` signature, database schema, and outcome-checking logic completely unchanged.

**Architecture:** `strategy.py` keeps its existing shape (indicators + pipeline functions in one file, no new modules). The old 15m-EMA200-trend-gate + 5m-EMA20-pullback pipeline is replaced by: (1) a zone builder that finds unbroken Supply/Demand pivot zones on 15m history, (2) a trigger detector that reads Chandelier Exit direction + PVT-vs-signal + dual-RSI on the latest closed 5m candle, confirmed by a breakout-buffer close, and (3) a confluence check requiring the trigger's direction to line up with an active opposing-type zone. TP/SL keep the bot's existing ROI-based sizing, just fed by the zone's boundary instead of the old pullback window. `build_btc_context`/`_btc_filter_ok`/`calculate_supertrend` and the config constants they use (`TREND_EMA_PERIOD`, `TREND_SUPERTREND_ATR_PERIOD`, `TREND_SUPERTREND_MULTIPLIER`) are untouched — they back the BTC filter, not the main trigger.

**Tech Stack:** Python, pandas, NumPy, pytest, monkeypatch-based fixtures (no network calls in tests).

## Global Constraints

- Only completed candles are ever evaluated — always `df.iloc[:-1]` before computing anything; never read the still-forming last candle (per `CLAUDE.md`).
- No TA-Lib or other paid/closed-source indicator dependency — NumPy/pandas only, matching the rest of `strategy.py`.
- `evaluate_symbol(symbol: str, btc_context: BtcContext | None = None, reject_sink: dict | None = None) -> Signal | None` signature must not change — `main.py`, `bot.py`, `webui.py`, `scripts/backtest_simple_strategy.py` all call it as-is.
- `Signal` and `BtcContext` dataclasses must not change shape.
- `build_btc_context`, `_btc_filter_ok`, `calculate_supertrend`, and their config constants (`TREND_EMA_PERIOD`, `TREND_SUPERTREND_ATR_PERIOD`, `TREND_SUPERTREND_MULTIPLIER`, `BTC_FILTER_SYMBOL`, `BTC_FILTER_TF`, `BTC_MAX_OPPOSING_MOVE_PCT`, `BTC_MAX_SINGLE_CANDLE_MOVE_PCT`, `BTC_MAX_THREE_CANDLE_MOVE_PCT`, `ENABLE_BTC_FILTER`) must not be touched.
- Never tighten a computed stop-loss artificially to force a signal through — reject instead (existing rule, unchanged).
- `DRY_RUN` defaults to `true` — never assume live trading is safe during this work.
- Server venv is `venv/`, not `.venv/` (only relevant if verification runs on the deployed server; local dev/test does not need this).
- Full spec: `docs/superpowers/specs/2026-07-27-binocular-trend-confluence-design.md`.

---

### Task 1: Cut the backup branch

**Files:** none (git operation only)

**Interfaces:** none

- [ ] **Step 1: Confirm current branch and clean working tree**

Run: `git status`
Expected: on `main` (or whatever branch currently holds the Supertrend Pullback v1 code), with only the changes already present at the start of this work (if any are pre-existing and unrelated, leave them alone — do not stash/discard anything you didn't create in this task).

- [ ] **Step 2: Cut the backup branch from current HEAD**

```bash
git branch backup/supertrend-pullback-v1
git push -u origin backup/supertrend-pullback-v1
```

- [ ] **Step 3: Verify the branch exists on origin and matches current `strategy.py`**

```bash
git fetch origin
git diff main origin/backup/supertrend-pullback-v1 -- strategy.py config.py
```
Expected: no diff output (branch is identical to current `main` at this point).

No commit for this task — it only creates a branch pointer, no working-tree changes.

---

### Task 2: `config.py` — remove old strategy settings, add new ones

**Files:**
- Modify: `config.py:69-95` (old strategy constants), `config.py:57-61` (`STRATEGY_NAME`)
- Modify: `.env.example:11-34` (old strategy env block)

**Interfaces:**
- Produces: `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `PVT_SIGNAL_LENGTH`, `PVT_SIGNAL_TYPE`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `ZONE_SWING_LENGTH`, `ZONE_ATR_PERIOD`, `ZONE_BOX_WIDTH`, `ZONE_PROXIMITY_ATR_MULT`, `ZONE_MAX_AGE_BARS`, `ENTRY_BUFFER_PCT` — all consumed by Task 5.
- Keeps unchanged: `TREND_EMA_PERIOD`, `TREND_SUPERTREND_ATR_PERIOD`, `TREND_SUPERTREND_MULTIPLIER`, `SL_ATR_BUFFER_MULTIPLIER`, `TREND_TF`, `ENTRY_TF`, `TREND_KLINE_COUNT`, `ENTRY_KLINE_COUNT`, `TARGET_ROI_PCT`, `MAX_SL_ROI_PCT`, `LEVERAGE`, `TP_PRICE_PCT`, `MAX_SL_PRICE_PCT`, `MIN_RR`, and every BTC-filter/coin-pool/scan-limit constant.
- Removes: `ENTRY_EMA_PERIOD`, `RSI_PERIOD`, `ATR_PERIOD`, `ENTRY_SUPERTREND_ATR_PERIOD`, `ENTRY_SUPERTREND_MULTIPLIER`, `VOLUME_MA_PERIOD`, `MIN_VOLUME_MULTIPLIER`, `PULLBACK_LOOKBACK_BARS`, `MAX_EMA_DISTANCE_PCT`, `MAX_CONFIRMATION_CANDLE_ATR`, `RSI_LONG_MIN`, `RSI_LONG_MAX`, `RSI_SHORT_MIN`, `RSI_SHORT_MAX`.

- [ ] **Step 1: Edit `config.py`**

Replace lines 57-95 (from the `STRATEGY_NAME` comment through `SL_ATR_BUFFER_MULTIPLIER`) with:

```python
# ── Strategy: Binocular Trend Confluence v1 ─────────────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Binocular Trend Confluence v1",
)

TREND_TF: str = os.getenv("TREND_TF", "15m")
ENTRY_TF: str = os.getenv("ENTRY_TF", "5m")

TREND_KLINE_COUNT: int = int(os.getenv("TREND_KLINE_COUNT", "260"))
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "120"))

# Still used by the BTC safety filter's own trend gate (build_btc_context),
# not by the main strategy's trigger -- do not remove.
TREND_EMA_PERIOD: int = int(os.getenv("TREND_EMA_PERIOD", "200"))
TREND_SUPERTREND_ATR_PERIOD: int = int(os.getenv("TREND_SUPERTREND_ATR_PERIOD", "10"))
TREND_SUPERTREND_MULTIPLIER: float = float(os.getenv("TREND_SUPERTREND_MULTIPLIER", "3.0"))

# 5m Chandelier Exit (trigger direction)
CHANDELIER_ATR_PERIOD: int = int(os.getenv("CHANDELIER_ATR_PERIOD", "10"))
CHANDELIER_MULTIPLIER: float = float(os.getenv("CHANDELIER_MULTIPLIER", "2.2"))

# 5m Price-Volume-Trend vs its smoothed signal (momentum confirmation)
PVT_SIGNAL_LENGTH: int = int(os.getenv("PVT_SIGNAL_LENGTH", "21"))
PVT_SIGNAL_TYPE: str = os.getenv("PVT_SIGNAL_TYPE", "SMA")  # "SMA" | "EMA"

# 5m dual-RSI regime filter
RSI_FAST_PERIOD: int = int(os.getenv("RSI_FAST_PERIOD", "25"))
RSI_SLOW_PERIOD: int = int(os.getenv("RSI_SLOW_PERIOD", "55"))

# Breakout confirmation buffer (close must clear the previous candle's
# high/low by this fraction)
ENTRY_BUFFER_PCT: float = float(os.getenv("ENTRY_BUFFER_PCT", "0.0002"))

# 15m Supply/Demand zone detection
ZONE_SWING_LENGTH: int = int(os.getenv("ZONE_SWING_LENGTH", "10"))
ZONE_ATR_PERIOD: int = int(os.getenv("ZONE_ATR_PERIOD", "50"))
ZONE_BOX_WIDTH: float = float(os.getenv("ZONE_BOX_WIDTH", "2.5"))
ZONE_PROXIMITY_ATR_MULT: float = float(os.getenv("ZONE_PROXIMITY_ATR_MULT", "0.5"))
ZONE_MAX_AGE_BARS: int = int(os.getenv("ZONE_MAX_AGE_BARS", "100"))

SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("SL_ATR_BUFFER_MULTIPLIER", "0.10"))
```

- [ ] **Step 2: Verify config still imports cleanly**

Run: `python -c "import config; print(config.STRATEGY_NAME, config.CHANDELIER_MULTIPLIER, config.ZONE_SWING_LENGTH, config.TREND_EMA_PERIOD)"`
Expected: prints `Binocular Trend Confluence v1 2.2 10 200` with no traceback.

- [ ] **Step 3: Update `.env.example`**

Replace lines 11-34 (the `# ── Simple Supertrend Pullback v1 ──` block) with:

```
# ── Binocular Trend Confluence v1 -- see config.py for full defaults ──
# 15m Supply/Demand zones + 5m Chandelier Exit direction, PVT-vs-signal
# momentum, and dual-RSI regime confirm entry (breakout-buffer close).
STRATEGY_NAME=Binocular Trend Confluence v1
TREND_TF=15m
ENTRY_TF=5m
TREND_EMA_PERIOD=200
TREND_SUPERTREND_ATR_PERIOD=10
TREND_SUPERTREND_MULTIPLIER=3.0
CHANDELIER_ATR_PERIOD=10
CHANDELIER_MULTIPLIER=2.2
PVT_SIGNAL_LENGTH=21
PVT_SIGNAL_TYPE=SMA
RSI_FAST_PERIOD=25
RSI_SLOW_PERIOD=55
ENTRY_BUFFER_PCT=0.0002
ZONE_SWING_LENGTH=10
ZONE_ATR_PERIOD=50
ZONE_BOX_WIDTH=2.5
ZONE_PROXIMITY_ATR_MULT=0.5
ZONE_MAX_AGE_BARS=100
SL_ATR_BUFFER_MULTIPLIER=0.10

# 15% ROI at 20x requires approximately 0.75% price movement.
# 10% stop ROI at 20x equals approximately 0.50% price movement.
TARGET_ROI_PCT=15.0
MAX_SL_ROI_PCT=10.0
LEVERAGE=20
MIN_RR=1.5
```

- [ ] **Step 4: Commit**

```bash
git add config.py .env.example
git commit -m "config: swap Supertrend Pullback settings for Binocular Trend Confluence"
```

---

### Task 3: New indicators — Chandelier Exit, PVT, pivot detection

**Files:**
- Modify: `strategy.py` (add functions after `calculate_supertrend`, i.e. after current line 122)
- Create: `tests/test_binocular_indicators.py`

**Interfaces:**
- Consumes: nothing new (only `pandas`/`numpy`, already imported in `strategy.py`)
- Produces:
  - `calculate_chandelier_exit(df: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame` with columns `chandelier_long_stop`, `chandelier_short_stop`, `chandelier_direction` (int, 1 or -1)
  - `calculate_pvt(df: pd.DataFrame) -> pd.Series`
  - `calculate_pvt_signal(pvt: pd.Series, length: int, ma_type: str) -> pd.Series`
  - `find_pivot_highs(df: pd.DataFrame, swing_length: int) -> pd.Series` (float value at confirmed pivot bars, `NaN` elsewhere, same index as `df`)
  - `find_pivot_lows(df: pd.DataFrame, swing_length: int) -> pd.Series` (mirrored)
- These are consumed by Task 4 (`build_zones`) and Task 5 (`_detect_trigger`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_binocular_indicators.py`:

```python
import numpy as np
import pandas as pd
import pytest

from strategy import (
    calculate_chandelier_exit,
    calculate_pvt,
    calculate_pvt_signal,
    find_pivot_highs,
    find_pivot_lows,
)


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_chandelier_exit_bullish_direction():
    df = _trend_df(60, step=0.8)
    chand = calculate_chandelier_exit(df, atr_period=10, multiplier=2.2)
    assert chand["chandelier_direction"].iloc[-1] == 1


def test_chandelier_exit_bearish_direction():
    df = _trend_df(60, step=-0.8)
    chand = calculate_chandelier_exit(df, atr_period=10, multiplier=2.2)
    assert chand["chandelier_direction"].iloc[-1] == -1


def test_chandelier_exit_does_not_use_future_data():
    df = _trend_df(60, step=0.8)
    full = calculate_chandelier_exit(df, atr_period=10, multiplier=2.2)
    partial = calculate_chandelier_exit(df.iloc[:40].copy(), atr_period=10, multiplier=2.2)
    for i in range(40):
        assert full["chandelier_direction"].iloc[i] == partial["chandelier_direction"].iloc[i]
        assert full["chandelier_long_stop"].iloc[i] == pytest.approx(
            partial["chandelier_long_stop"].iloc[i], abs=1e-9
        )


def test_pvt_accumulates_correctly():
    df = pd.DataFrame({
        "close": [100.0, 110.0, 99.0],
        "volume": [1000.0, 1000.0, 1000.0],
    })
    pvt = calculate_pvt(df)
    assert pvt.iloc[0] == pytest.approx(0.0, abs=1e-9)
    assert pvt.iloc[1] == pytest.approx(100.0, abs=1e-9)
    assert pvt.iloc[2] == pytest.approx(0.0, abs=1e-6)


def test_pvt_signal_sma():
    pvt = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="SMA")
    expected = [1.0, 1.5, 2.0, 3.0, 4.0]
    for got, want in zip(signal.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_pvt_signal_ema():
    pvt = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="EMA")
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    for got, want in zip(signal.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_pivot_high_detection():
    n = 7
    df = pd.DataFrame({
        "high":  [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0],
        "low":   [0.5, 1.5, 2.5, 9.5, 2.5, 1.5, 0.5],
        "close": [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0],
        "open":  [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0],
        "volume": [1000.0] * n,
    })
    pivots = find_pivot_highs(df, swing_length=3)
    assert pivots.iloc[3] == pytest.approx(10.0)
    assert pivots.drop(index=3).isna().all()


def test_pivot_low_detection():
    n = 7
    df = pd.DataFrame({
        "high":  [10.5, 9.5, 8.5, 2.0, 8.5, 9.5, 10.5],
        "low":   [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0],
        "close": [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0],
        "open":  [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0],
        "volume": [1000.0] * n,
    })
    pivots = find_pivot_lows(df, swing_length=3)
    assert pivots.iloc[3] == pytest.approx(1.0)
    assert pivots.drop(index=3).isna().all()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -v`
Expected: `ImportError` — `calculate_chandelier_exit` etc. do not exist yet.

- [ ] **Step 3: Add the indicator functions to `strategy.py`**

Insert immediately after `calculate_supertrend` (after current line 122), before the `# ── evaluate_symbol pipeline ──` section:

```python
def calculate_chandelier_exit(df: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame:
    close = df["close"].to_numpy()
    atr = (multiplier * calculate_atr(df, atr_period)).to_numpy()
    highest_close = df["close"].rolling(atr_period, min_periods=1).max().to_numpy()
    lowest_close = df["close"].rolling(atr_period, min_periods=1).min().to_numpy()

    n = len(df)
    long_stop = np.zeros(n)
    short_stop = np.zeros(n)
    direction = np.ones(n, dtype=int)

    for i in range(n):
        raw_long = highest_close[i] - atr[i]
        raw_short = lowest_close[i] + atr[i]
        if i == 0:
            long_stop[i] = raw_long
            short_stop[i] = raw_short
            direction[i] = 1
            continue

        long_stop_prev = long_stop[i - 1]
        short_stop_prev = short_stop[i - 1]
        long_stop[i] = max(raw_long, long_stop_prev) if close[i - 1] > long_stop_prev else raw_long
        short_stop[i] = min(raw_short, short_stop_prev) if close[i - 1] < short_stop_prev else raw_short

        if close[i] > short_stop_prev:
            direction[i] = 1
        elif close[i] < long_stop_prev:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    return pd.DataFrame(
        {
            "chandelier_long_stop": long_stop,
            "chandelier_short_stop": short_stop,
            "chandelier_direction": direction,
        },
        index=df.index,
    )


def calculate_pvt(df: pd.DataFrame) -> pd.Series:
    pct_change = df["close"].pct_change().fillna(0.0)
    return (pct_change * df["volume"]).cumsum().rename("pvt")


def calculate_pvt_signal(pvt: pd.Series, length: int, ma_type: str) -> pd.Series:
    if ma_type == "EMA":
        return pvt.ewm(span=length, adjust=False).mean()
    return pvt.rolling(length, min_periods=1).mean()


def find_pivot_highs(df: pd.DataFrame, swing_length: int) -> pd.Series:
    high = df["high"]
    n = len(df)
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(swing_length, n - swing_length):
        window = high.iloc[i - swing_length: i + swing_length + 1]
        if high.iloc[i] == window.max():
            result.iloc[i] = high.iloc[i]
    return result


def find_pivot_lows(df: pd.DataFrame, swing_length: int) -> pd.Series:
    low = df["low"]
    n = len(df)
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(swing_length, n - swing_length):
        window = low.iloc[i - swing_length: i + swing_length + 1]
        if low.iloc[i] == window.min():
            result.iloc[i] = low.iloc[i]
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -v`
Expected: all tests PASS. If `test_chandelier_exit_bullish_direction`/`_bearish_direction` don't land as expected, the 60-bar/step=0.8 trend is generous relative to `atr_period=10`; this is a numeric-constant check, not a logic bug — re-verify the loop against the Pine reference in the spec before changing constants.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add Chandelier Exit, PVT, and pivot-detection indicators"
```

---

### Task 4: Zone builder (Supply/Demand zones with BOS tracking)

**Files:**
- Modify: `strategy.py` (add `build_zones` after the Task 3 functions)
- Modify: `tests/test_binocular_indicators.py` (append zone tests)

**Interfaces:**
- Consumes: `calculate_atr` (existing), `find_pivot_highs`, `find_pivot_lows` (Task 3)
- Produces: `build_zones(df: pd.DataFrame, swing_length: int, atr_period: int, box_width: float, max_age_bars: int) -> list[dict]`. Each dict has keys `type` (`"supply"` or `"demand"`), `top` (float), `bottom` (float), `formed_index` (int, positional index into `df`), `bos` (bool), `age_bars` (int, `len(df) - 1 - formed_index`). Only zones with `age_bars <= max_age_bars` are returned. Consumed by Task 5's `_find_confluence_zone`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_binocular_indicators.py`:

```python
from strategy import build_zones


def _zone_test_df(pivot_type: str) -> pd.DataFrame:
    """
    21 bars, flat at 100 except a centered pivot at index 10: a low of 90
    (pivot_type='low') or a high of 110 (pivot_type='high'), tapering
    linearly over the 10 bars either side -- guarantees index 10 is the
    unique extreme within any +-10 window.
    """
    n = 21
    mid = 10
    extreme = 90.0 if pivot_type == "low" else 110.0
    closes = np.full(n, 100.0)
    for offset in range(-10, 11):
        taper = 1.0 - abs(offset) / 10.0
        closes[mid + offset] = 100.0 + (extreme - 100.0) * taper
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.1
    lows = np.minimum(opens, closes) - 0.1
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_build_zones_creates_demand_and_supply():
    df_low = _zone_test_df("low")
    zones_low = build_zones(df_low, swing_length=10, atr_period=10, box_width=2.5, max_age_bars=100)
    assert any(z["type"] == "demand" for z in zones_low)

    df_high = _zone_test_df("high")
    zones_high = build_zones(df_high, swing_length=10, atr_period=10, box_width=2.5, max_age_bars=100)
    assert any(z["type"] == "supply" for z in zones_high)


def test_zone_marked_bos_after_close_through():
    df = _zone_test_df("low")
    # Append bars closing well below the demand zone -- must flip bos=True.
    extra_idx = pd.RangeIndex(len(df), len(df) + 5)
    extra = pd.DataFrame({
        "open": [85.0] * 5, "high": [85.5] * 5, "low": [79.5] * 5,
        "close": [80.0] * 5, "volume": [1000.0] * 5,
    }, index=extra_idx)
    df_extended = pd.concat([df, extra]).reset_index(drop=True)

    zones = build_zones(df_extended, swing_length=10, atr_period=10, box_width=2.5, max_age_bars=100)
    demand_zones = [z for z in zones if z["type"] == "demand"]
    assert demand_zones
    assert demand_zones[0]["bos"] is True


def test_overlapping_zones_are_skipped():
    df = _zone_test_df("low")
    zones = build_zones(df, swing_length=10, atr_period=10, box_width=2.5, max_age_bars=100)
    demand_zones = [z for z in zones if z["type"] == "demand"]
    # Only one pivot low exists in the fixture -- exactly one demand zone.
    assert len(demand_zones) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_binocular_indicators.py -v -k zone`
Expected: `ImportError` — `build_zones` does not exist yet.

- [ ] **Step 3: Add `build_zones` to `strategy.py`**

Insert after `find_pivot_lows`:

```python
def build_zones(
    df: pd.DataFrame,
    swing_length: int,
    atr_period: int,
    box_width: float,
    max_age_bars: int,
) -> list[dict]:
    atr = calculate_atr(df, atr_period)
    pivot_highs = find_pivot_highs(df, swing_length)
    pivot_lows = find_pivot_lows(df, swing_length)

    zones: list[dict] = []
    n = len(df)

    for i in range(n):
        atr_i = float(atr.iloc[i])
        atr_buffer = atr_i * (box_width / 10.0)

        if not np.isnan(pivot_highs.iloc[i]):
            top = float(pivot_highs.iloc[i])
            bottom = top - atr_buffer
            poi = (top + bottom) / 2.0
            overlap = any(
                z["type"] == "supply" and not z["bos"]
                and abs(poi - (z["top"] + z["bottom"]) / 2.0) <= 2 * atr_i
                for z in zones
            )
            if not overlap:
                zones.append({"type": "supply", "top": top, "bottom": bottom, "formed_index": i, "bos": False})

        if not np.isnan(pivot_lows.iloc[i]):
            bottom = float(pivot_lows.iloc[i])
            top = bottom + atr_buffer
            poi = (top + bottom) / 2.0
            overlap = any(
                z["type"] == "demand" and not z["bos"]
                and abs(poi - (z["top"] + z["bottom"]) / 2.0) <= 2 * atr_i
                for z in zones
            )
            if not overlap:
                zones.append({"type": "demand", "top": top, "bottom": bottom, "formed_index": i, "bos": False})

        close_i = float(df["close"].iloc[i])
        for z in zones:
            if z["bos"] or z["formed_index"] >= i:
                continue
            if z["type"] == "supply" and close_i >= z["top"]:
                z["bos"] = True
            elif z["type"] == "demand" and close_i <= z["bottom"]:
                z["bos"] = True

    latest_index = n - 1
    for z in zones:
        z["age_bars"] = latest_index - z["formed_index"]

    return [z for z in zones if z["age_bars"] <= max_age_bars]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_binocular_indicators.py -v`
Expected: all tests PASS, including the Task 3 ones.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_binocular_indicators.py
git commit -m "feat: add Supply/Demand zone builder with BOS invalidation"
```

---

### Task 5: Rewrite `evaluate_symbol`'s trigger/confluence/TP-SL pipeline

**Files:**
- Modify: `strategy.py:126-457` (import block through `evaluate_symbol`, keep `build_btc_context`/`_btc_filter_ok` at the bottom unchanged)

**Interfaces:**
- Consumes: `calculate_chandelier_exit`, `calculate_pvt`, `calculate_pvt_signal`, `calculate_rsi` (existing), `build_zones` (Tasks 3-4); config constants from Task 2.
- Produces (all in `strategy.py`, replacing the old `_detect_trend`/`_detect_pullback_and_confirmation`/`_calculate_tp_sl`/`_score_candidate`):
  - `_detect_trigger(df_5m: pd.DataFrame) -> tuple[str | None, str, dict]` — `(direction_or_None, reject_reason, details)`. `details` keys: `close`, `prev_high`, `prev_low`, `pvt`, `pvt_signal`, `rsi_fast`, `rsi_slow`, `chandelier_direction`.
  - `_find_confluence_zone(zones: list[dict], direction: str, price: float, atr: float, proximity_mult: float) -> dict | None`
  - `_calculate_tp_sl(direction: str, entry: float, zone: dict, atr_zone: float) -> tuple[float, float] | None` (same shape/contract as the old function it replaces)
  - `_score_candidate(direction: str, details: dict, zone: dict, rr: float) -> float`
  - `_reason_bucket(reason: str) -> str` (rewritten mapping)
  - `evaluate_symbol(symbol, btc_context=None, reject_sink=None) -> Signal | None` (same signature, new pipeline body)
- `valid_trade_geometry`, `direction_slot_available`, `_calc_rr`, `_roi_pct`, `_bump`, `build_btc_context`, `_btc_filter_ok`, `Signal`, `BtcContext` are **unchanged** — do not modify them in this task.

- [ ] **Step 1: Update the module docstring and import block**

Replace lines 1-7:

```python
"""
Binocular Trend Confluence v1.

15m Supply/Demand zones (pivot-based, BOS-tracked) provide structural
confluence; 5m Chandelier Exit direction + Price-Volume-Trend-vs-signal
momentum + dual-RSI(fast/slow) regime, confirmed by a breakout-buffer
close, drive entries. Only completed candles are ever used. See
docs/superpowers/specs/2026-07-27-binocular-trend-confluence-design.md.
"""
```

Replace the import block (current lines 126-139):

```python
from market_data import get_market_klines
from config import (
    TREND_TF, ENTRY_TF, TREND_KLINE_COUNT, ENTRY_KLINE_COUNT,
    CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER,
    PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE,
    RSI_FAST_PERIOD, RSI_SLOW_PERIOD,
    ENTRY_BUFFER_PCT,
    ZONE_SWING_LENGTH, ZONE_ATR_PERIOD, ZONE_BOX_WIDTH,
    ZONE_PROXIMITY_ATR_MULT, ZONE_MAX_AGE_BARS,
    SL_ATR_BUFFER_MULTIPLIER, LEVERAGE, TP_PRICE_PCT, MAX_SL_PRICE_PCT, MIN_RR,
    TREND_EMA_PERIOD, TREND_SUPERTREND_ATR_PERIOD, TREND_SUPERTREND_MULTIPLIER,
    ENABLE_BTC_FILTER, BTC_FILTER_SYMBOL, BTC_FILTER_TF,
    BTC_MAX_OPPOSING_MOVE_PCT, BTC_MAX_SINGLE_CANDLE_MOVE_PCT, BTC_MAX_THREE_CANDLE_MOVE_PCT,
)
```

(`TREND_EMA_PERIOD`/`TREND_SUPERTREND_ATR_PERIOD`/`TREND_SUPERTREND_MULTIPLIER` are needed because `build_btc_context`, further down the file, still uses them — unchanged from before.)

- [ ] **Step 2: Delete the old trend/pullback/scoring functions**

Delete these functions entirely (they are fully superseded): `_ema_slope_ok`, `_detect_trend`, `_detect_pullback_and_confirmation`, `_calculate_tp_sl`, `_score_candidate`. Keep `valid_trade_geometry`, `direction_slot_available`, `_calc_rr`, `_roi_pct`, `_bump` exactly as they are.

- [ ] **Step 3: Add the new trigger/confluence/TP-SL/scoring functions**

Insert in place of the deleted functions (same relative position, before `_reason_bucket`):

```python
def _detect_trigger(df_5m: pd.DataFrame) -> tuple[str | None, str, dict]:
    close = df_5m["close"]
    chand = calculate_chandelier_exit(df_5m, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df_5m)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(close, RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(close, RSI_SLOW_PERIOD)

    dir_last = int(chand["chandelier_direction"].iloc[-1])
    close_last = float(close.iloc[-1])
    prev_high = float(df_5m["high"].iloc[-2])
    prev_low = float(df_5m["low"].iloc[-2])
    pvt_last = float(pvt.iloc[-1])
    pvt_signal_last = float(pvt_signal.iloc[-1])
    rsi_fast_last = float(rsi_fast.iloc[-1])
    rsi_slow_last = float(rsi_slow.iloc[-1])

    details = {
        "close": close_last,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "pvt": pvt_last,
        "pvt_signal": pvt_signal_last,
        "rsi_fast": rsi_fast_last,
        "rsi_slow": rsi_slow_last,
        "chandelier_direction": dir_last,
    }

    if dir_last == 1:
        if not (pvt_last > pvt_signal_last):
            return None, "no PVT bullish momentum", details
        if not (rsi_fast_last > rsi_slow_last):
            return None, "RSI regime not bullish", details
        if not (close_last > prev_high * (1 + ENTRY_BUFFER_PCT)):
            return None, "no breakout confirmation", details
        return "LONG", "", details

    if not (pvt_last < pvt_signal_last):
        return None, "no PVT bearish momentum", details
    if not (rsi_fast_last < rsi_slow_last):
        return None, "RSI regime not bearish", details
    if not (close_last < prev_low * (1 - ENTRY_BUFFER_PCT)):
        return None, "no breakout confirmation", details
    return "SHORT", "", details


def _find_confluence_zone(
    zones: list[dict], direction: str, price: float, atr: float, proximity_mult: float
) -> dict | None:
    zone_type = "demand" if direction == "LONG" else "supply"
    tolerance = atr * proximity_mult
    candidates = [
        z for z in zones
        if z["type"] == zone_type and not z["bos"]
        and (z["bottom"] - tolerance) <= price <= (z["top"] + tolerance)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z["formed_index"])


def _calculate_tp_sl(direction: str, entry: float, zone: dict, atr_zone: float) -> tuple[float, float] | None:
    if direction == "LONG":
        tp = entry * (1 + TP_PRICE_PCT)
        structural_sl = zone["bottom"] - atr_zone * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl >= entry:
            return None
        if (entry - structural_sl) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl
    else:
        tp = entry * (1 - TP_PRICE_PCT)
        structural_sl = zone["top"] + atr_zone * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl <= entry:
            return None
        if (structural_sl - entry) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl


def _score_candidate(direction: str, details: dict, zone: dict, rr: float) -> float:
    zone_mid = (zone["top"] + zone["bottom"]) / 2.0
    zone_half_width = max((zone["top"] - zone["bottom"]) / 2.0, 1e-9)
    proximity_ratio = min(1.0, abs(details["close"] - zone_mid) / zone_half_width)
    score = 25.0 * (1.0 - proximity_ratio)

    pvt_gap = abs(details["pvt"] - details["pvt_signal"])
    pvt_scale = max(abs(details["pvt_signal"]), 1.0)
    pvt_quality = min(1.0, pvt_gap / (pvt_scale * 0.05))
    score += 25.0 * pvt_quality

    if direction == "LONG":
        clearance = (details["close"] - details["prev_high"]) / details["prev_high"]
    else:
        clearance = (details["prev_low"] - details["close"]) / details["prev_low"]
    breakout_quality = min(1.0, max(0.0, clearance / (ENTRY_BUFFER_PCT * 10)))
    score += 20.0 * breakout_quality

    rsi_fast = details["rsi_fast"]
    ideal_lo, ideal_hi = (55.0, 62.0) if direction == "LONG" else (38.0, 45.0)
    if ideal_lo <= rsi_fast <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi_fast - ideal_lo), abs(rsi_fast - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 10.0 * rsi_quality

    rr_quality = min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0 else (1.0 if rr >= MIN_RR else 0.0)
    score += 10.0 * rr_quality

    freshness = 1.0 - min(1.0, zone["age_bars"] / max(ZONE_MAX_AGE_BARS, 1))
    score += 10.0 * freshness

    return round(min(100.0, max(0.0, score)), 1)
```

- [ ] **Step 4: Rewrite `_reason_bucket`**

Replace the existing `_reason_bucket` function body:

```python
def _reason_bucket(reason: str) -> str:
    """Collapse the free-text trigger reject reason into a stable category
    so scan-level rejects can be aggregated and counted."""
    if "PVT" in reason:
        return "no_pvt_momentum"
    if "RSI regime" in reason:
        return "no_rsi_regime"
    if "breakout confirmation" in reason:
        return "no_breakout_confirmation"
    return "trigger_other"
```

- [ ] **Step 5: Rewrite `evaluate_symbol`**

Replace the entire function body (keep the `def evaluate_symbol(...)` signature and the outer `try/except` unchanged):

```python
def evaluate_symbol(
    symbol: str,
    btc_context: "BtcContext | None" = None,
    reject_sink: dict | None = None,
) -> Signal | None:
    try:
        raw_15m = get_market_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        raw_5m = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if raw_15m is None or raw_15m.empty or raw_5m is None or raw_5m.empty:
            logger.debug("[REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed_15m = raw_15m.iloc[:-1].copy()
        closed_5m = raw_5m.iloc[:-1].copy()

        if len(closed_15m) < ZONE_ATR_PERIOD + ZONE_SWING_LENGTH * 2 + 10:
            logger.debug("[REJECT] %s insufficient 15m candle history", symbol)
            _bump(reject_sink, "insufficient_history")
            return None
        if len(closed_5m) < RSI_SLOW_PERIOD + 20:
            logger.debug("[REJECT] %s insufficient 5m candle history", symbol)
            _bump(reject_sink, "insufficient_history")
            return None

        direction, reason, details = _detect_trigger(closed_5m)
        if direction is None:
            logger.debug("[REJECT] %s %s", symbol, reason)
            _bump(reject_sink, _reason_bucket(reason))
            return None

        zones = build_zones(closed_15m, ZONE_SWING_LENGTH, ZONE_ATR_PERIOD, ZONE_BOX_WIDTH, ZONE_MAX_AGE_BARS)
        atr_zone_last = float(calculate_atr(closed_15m, ZONE_ATR_PERIOD).iloc[-1])
        zone = _find_confluence_zone(zones, direction, details["close"], atr_zone_last, ZONE_PROXIMITY_ATR_MULT)
        if zone is None:
            logger.debug("[REJECT] %s no zone confluence", symbol)
            _bump(reject_sink, "no_zone_confluence")
            return None

        if ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context()
            if ctx is None:
                logger.debug("[REJECT] %s BTC context unavailable", symbol)
                _bump(reject_sink, "btc_context_unavailable")
                return None
            btc_ok, btc_reason = _btc_filter_ok(direction, ctx)
            if not btc_ok:
                logger.debug("[REJECT] %s %s %s", symbol, direction, btc_reason)
                _bump(reject_sink, "btc_filter")
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl(direction, entry, zone, atr_zone_last)
        if tp_sl is None:
            logger.debug("[REJECT] %s structural stop too wide", symbol)
            _bump(reject_sink, "stop_too_wide")
            return None
        tp, sl = tp_sl

        if not valid_trade_geometry(direction, entry, tp, sl):
            logger.debug("[REJECT] %s invalid trade geometry", symbol)
            _bump(reject_sink, "invalid_geometry")
            return None

        rr = _calc_rr(direction, entry, tp, sl)
        if rr < MIN_RR:
            logger.debug("[REJECT] %s RR %.2f below %.2f", symbol, rr, MIN_RR)
            _bump(reject_sink, "rr_below_min")
            return None

        tp_roi, sl_roi = _roi_pct(direction, entry, tp, sl)
        score = _score_candidate(direction, details, zone, rr)

        logger.info(
            "[CANDIDATE] %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
            symbol, direction, score, entry, tp, sl, rr,
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary="15m demand/supply zone + 5m Chandelier/PVT/RSI breakout",
            generated_at=datetime.now(timezone.utc),
            rr=round(rr, 2),
            score=score,
            entry_low=entry,
            entry_high=entry,
        )
    except Exception as e:
        logger.error("[EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
```

- [ ] **Step 6: Sanity-check imports**

Run: `python -c "import strategy"`
Expected: no traceback. This only confirms syntax/imports resolve — no fixtures exist yet to exercise `evaluate_symbol` (that's Task 6).

- [ ] **Step 7: Commit**

```bash
git add strategy.py
git commit -m "feat: rewrite evaluate_symbol as zone-confluence + Chandelier/PVT/RSI trigger"
```

---

### Task 6: New fixtures + strategy-level tests

**Files:**
- Modify: `tests/strategy_fixtures.py` (add `make_15m_zone_df`, `make_5m_trigger_df`)
- Create: `tests/test_strategy_binocular.py`
- Delete: `tests/test_strategy_supertrend_pullback.py`

**Interfaces:**
- Consumes: `evaluate_symbol`, `valid_trade_geometry` (Task 5); `patch_klines` (existing, unchanged)
- Produces: `make_15m_zone_df(direction="LONG", bars=200, base_price=100.0, zone_price=None) -> pd.DataFrame`, `make_5m_trigger_df(direction="LONG", bars=90, base_price=90.0) -> pd.DataFrame` — used only by this task's own tests and Task 7's BTC-filter tests.

- [ ] **Step 1: Delete the superseded test file**

```bash
git rm tests/test_strategy_supertrend_pullback.py
```

- [ ] **Step 2: Add new fixture builders to `tests/strategy_fixtures.py`**

Append to the end of the file:

```python
def make_15m_zone_df(
    direction: str = "LONG",
    bars: int = 200,
    base_price: float = 100.0,
    zone_price: float | None = None,
) -> pd.DataFrame:
    """
    A flat 15m series with exactly one clean, un-broken pivot at
    `zone_price`: a pivot LOW (demand zone) for LONG, a pivot HIGH (supply
    zone) for SHORT. Tapers linearly to/from `zone_price` over
    ZONE_SWING_LENGTH=10 bars either side of the centered pivot, then
    returns to and stays at `base_price` -- the zone is never revisited,
    so it never gets marked BOS. Ends with one extra duplicated row so
    callers can safely `iloc[:-1]`.
    """
    if zone_price is None:
        zone_price = base_price - 10.0 if direction == "LONG" else base_price + 10.0

    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
    mid = bars // 2
    depth = zone_price - base_price

    closes = np.full(bars, base_price)
    for offset in range(-10, 11):
        taper = 1.0 - abs(offset) / 10.0
        closes[mid + offset] = base_price + depth * taper

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.1
    lows = np.minimum(opens, closes) - 0.1
    volumes = np.full(bars, 1000.0)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_5m_trigger_df(
    direction: str = "LONG",
    bars: int = 90,
    base_price: float = 90.0,
    push_step: float = 0.05,
) -> pd.DataFrame:
    """
    A 5m series: tight chop for the first `bars-20` candles (keeps the
    Chandelier Exit stops close to price), then a clean `push_step`-per-bar
    directional push for the final 20 candles with ramping volume -- flips
    Chandelier direction, pushes PVT past its signal average, and skews
    RSI(25) past RSI(55). Ends with one extra duplicated row so callers can
    safely `iloc[:-1]`.

    Numeric constants here are reasoned, not hand-executed against pandas
    -- same convention as make_5m_pullback_df above. If Chandelier
    direction, PVT-vs-signal, or RSI-fast-vs-slow don't land as expected
    for the intended direction, widen `push_step`, extend the push window,
    or steepen the volume ramp below and re-run; that is expected TDD
    iteration, not a defect in the test itself.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")
    flat_n = bars - 20

    closes = np.empty(bars)
    closes[:flat_n] = base_price + np.sin(np.arange(flat_n) * 0.5) * 0.05
    for k in range(20):
        closes[flat_n + k] = closes[flat_n - 1] + sign * push_step * (k + 1)

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    volumes = np.full(bars, 500.0)
    volumes[flat_n:] = np.linspace(800.0, 3000.0, 20)

    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    if direction == "LONG":
        highs[-1] = max(highs[-1], closes[-1] + 0.05)
    else:
        lows[-1] = min(lows[-1], closes[-1] - 0.05)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])
```

- [ ] **Step 3: Write the failing strategy-level tests**

Create `tests/test_strategy_binocular.py`:

```python
import numpy as np
import pandas as pd

import strategy
from strategy import evaluate_symbol, valid_trade_geometry
from tests.strategy_fixtures import make_15m_zone_df, make_5m_trigger_df, patch_klines


def test_detect_trigger_rejects_long_when_chandelier_bearish(monkeypatch):
    # Isolates the chandelier-direction gate: PVT/RSI/breakout all pass
    # (this fixture is already tuned in Step 5 to satisfy them for LONG),
    # but chandelier direction is forced bearish -- must not fire LONG.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    def _fake_chandelier(df, atr_period, multiplier):
        return pd.DataFrame(
            {
                "chandelier_long_stop": 0.0,
                "chandelier_short_stop": 0.0,
                "chandelier_direction": -1,
            },
            index=df.index,
        )

    monkeypatch.setattr(strategy, "calculate_chandelier_exit", _fake_chandelier)

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None


def test_detect_trigger_rejects_without_pvt_momentum(monkeypatch):
    # Isolates the PVT-vs-signal gate: chandelier direction comes from the
    # real (tuned-bullish) fixture, but PVT is forced below its signal.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    monkeypatch.setattr(
        strategy, "calculate_pvt",
        lambda df: pd.Series(np.linspace(10.0, 0.0, len(df)), index=df.index),
    )
    monkeypatch.setattr(
        strategy, "calculate_pvt_signal",
        lambda pvt, length, ma_type: pd.Series(5.0, index=pvt.index),
    )

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None
    assert "PVT" in reason


def test_detect_trigger_rejects_without_rsi_regime(monkeypatch):
    # Isolates the dual-RSI gate: chandelier/PVT come from the real
    # (tuned-bullish) fixture, but both RSI periods are forced equal.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    monkeypatch.setattr(
        strategy, "calculate_rsi",
        lambda series, period: pd.Series(50.0, index=series.index),
    )

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None
    assert "RSI regime" in reason


def test_detect_trigger_rejects_without_breakout_confirmation(monkeypatch):
    # Isolates the breakout-buffer gate: chandelier/PVT/RSI all pass, but
    # an impossibly large buffer means the close can never clear it.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    monkeypatch.setattr(strategy, "ENTRY_BUFFER_PCT", 10.0)

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None
    assert "breakout confirmation" in reason


def test_long_signal_valid(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= 1.5
    assert sig.score > 0.0


def test_long_trade_geometry(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("LONG", sig.entry_price, sig.tp_price, sig.sl_price)


def test_invalid_geometry_rejected():
    assert valid_trade_geometry("LONG", 100.0, 99.0, 101.0) is False
    assert valid_trade_geometry("SHORT", 100.0, 101.0, 99.0) is False
    assert valid_trade_geometry("LONG", 0.0, 101.0, 99.0) is False


def test_long_rejected_without_zone_confluence(monkeypatch):
    # Zone sits at 50, but the 5m trigger fires around 90 -- no confluence.
    df_15m = make_15m_zone_df("LONG", zone_price=50.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_stop_too_wide(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MAX_SL_PRICE_PCT", 1e-9)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_rr_too_low(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None


def test_active_last_candle_is_ignored(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    # Corrupt only the forming (last, duplicated) candle -- evaluate_symbol
    # must still fire using the last COMPLETED candle underneath it.
    df_5m.iloc[-1, df_5m.columns.get_loc("close")] = 1.0
    df_5m.iloc[-1, df_5m.columns.get_loc("high")] = 1.0
    df_5m.iloc[-1, df_5m.columns.get_loc("low")] = 0.5
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")
    assert sig is not None
    assert sig.direction == "LONG"


def test_short_signal_valid(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price
    assert sig.rr >= 1.5


def test_short_trade_geometry(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("SHORT", sig.entry_price, sig.tp_price, sig.sl_price)


def test_short_rejected_without_zone_confluence(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=150.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_when_rr_too_low(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None
```

- [ ] **Step 4: Run the new tests and record the honest pass/fail state**

`evaluate_symbol` and `_detect_trigger` were already implemented in Task 5, so this isn't a red/green TDD cycle in the strict sense — it's the first real exercise of that implementation against realistic fixture data. Run: `python -m pytest tests/test_strategy_binocular.py -v`.
Expected: `test_invalid_geometry_rejected`, the four `_detect_trigger`-isolation tests above, and the `MIN_RR`/`MAX_SL_PRICE_PCT`-monkeypatched rejection tests should already pass (none of them depend on the `make_15m_zone_df`/`make_5m_trigger_df` fixtures producing an exact firing signal). `test_long_signal_valid`/`test_short_signal_valid`/geometry/`no_zone_confluence` tests may fail if the fixture constants don't yet produce a firing signal end-to-end.

- [ ] **Step 5: Tune fixture constants until the signal-valid tests pass**

If `test_long_signal_valid` fails, add a throwaway debug script to see which gate is rejecting:

```bash
python -c "
import strategy
from tests.strategy_fixtures import make_15m_zone_df, make_5m_trigger_df

df_15m = make_15m_zone_df('LONG', zone_price=90.0).iloc[:-1]
df_5m = make_5m_trigger_df('LONG', base_price=90.0).iloc[:-1]
direction, reason, details = strategy._detect_trigger(df_5m)
print('trigger:', direction, reason, details)

zones = strategy.build_zones(df_15m, strategy.ZONE_SWING_LENGTH, strategy.ZONE_ATR_PERIOD, strategy.ZONE_BOX_WIDTH, strategy.ZONE_MAX_AGE_BARS)
print('zones:', zones)
"
```

Adjust `push_step`, the volume ramp in `make_5m_trigger_df`, or the taper depth in `make_15m_zone_df` based on what the debug output shows (e.g. if `pvt <= pvt_signal`, steepen the volume ramp; if `chandelier_direction` never flips, increase `push_step` or extend the push window past 20 bars) and re-run Step 4. This is the same iterative-tuning convention already used by `make_5m_pullback_df` in this file (see its own docstring).

- [ ] **Step 6: Run the full test file once more to confirm everything passes**

Run: `python -m pytest tests/test_strategy_binocular.py -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/strategy_fixtures.py tests/test_strategy_binocular.py
git commit -m "test: add Binocular strategy fixtures and signal/reject tests"
```
(The `tests/test_strategy_supertrend_pullback.py` deletion from Step 1 rides
along in this same commit if you haven't committed separately in between.)

---

### Task 7: Update `tests/test_btc_filter.py` to use the new fixtures

**Files:**
- Modify: `tests/test_btc_filter.py`

**Interfaces:**
- Consumes: `make_15m_zone_df`, `make_5m_trigger_df` (Task 6) in place of `make_15m_trend_df`/`make_5m_pullback_df`. `BtcContext`, `evaluate_symbol`, `build_btc_context` are unchanged.

- [ ] **Step 1: Replace the fixture imports and calls**

In `tests/test_btc_filter.py`, change the import line:

```python
from tests.strategy_fixtures import make_15m_zone_df, make_5m_trigger_df, make_15m_trend_df, patch_klines
```

Then in each of `test_long_allowed_when_btc_bullish`, `test_long_blocked_when_btc_bearish`, `test_signal_blocked_during_extreme_btc_move`, replace:
```python
df_15m = make_15m_trend_df("LONG")
df_5m = make_5m_pullback_df("LONG")
```
with:
```python
df_15m = make_15m_zone_df("LONG", zone_price=90.0)
df_5m = make_5m_trigger_df("LONG", base_price=90.0)
```

In `test_short_allowed_when_btc_bearish` and `test_short_blocked_when_btc_bullish`, replace:
```python
df_15m = make_15m_trend_df("SHORT")
df_5m = make_5m_pullback_df("SHORT")
```
with:
```python
df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
```

Leave `test_btc_active_candle_is_ignored` untouched — it only calls `make_15m_trend_df` to build BTC's own context data for `build_btc_context`, which is unrelated to the main strategy's zone/trigger fixtures and still works as-is (that's why `make_15m_trend_df` stays imported above).

- [ ] **Step 2: Run the BTC filter tests**

Run: `python -m pytest tests/test_btc_filter.py -v`
Expected: all tests PASS. If `test_long_allowed_when_btc_bullish`/`test_short_allowed_when_btc_bearish` return `None` instead of a `Signal`, the zone/trigger fixtures aren't producing a valid candidate before the BTC filter even runs — re-use the debug snippet from Task 6 Step 5 to check `_detect_trigger`/`build_zones` output against these exact fixture parameters.

- [ ] **Step 3: Commit**

```bash
git add tests/test_btc_filter.py
git commit -m "test: point BTC-filter tests at the new Binocular strategy fixtures"
```

---

### Task 8: Update `bot.py` and `webui.py` display fields

**Files:**
- Modify: `bot.py:208-223,244-246`
- Modify: `webui.py:232-267`

**Interfaces:**
- Consumes: `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `ZONE_SWING_LENGTH`, `ZONE_BOX_WIDTH`, `ENTRY_BUFFER_PCT` from `config` (Task 2). No new functions produced.

- [ ] **Step 1: Fix `bot.py`'s `cmd_status` import list**

In `bot.py`, replace the import block (current lines 208-223):

```python
    from config import (
        STRATEGY_NAME,
        TREND_TF, ENTRY_TF,
        RSI_FAST_PERIOD, RSI_SLOW_PERIOD,
        CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER,
        MAX_SL_ROI_PCT, TARGET_ROI_PCT,
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

- [ ] **Step 2: Fix `bot.py`'s message body lines**

Replace lines 244-246 (`f"Trend TF: ..."`, `f"Entry TF: ..."`, `f"RSI ranges: ..."`):

```python
        f"Zone TF:     {_code(TREND_TF)}  (Supply/Demand zones)",
        f"Trigger TF:  {_code(ENTRY_TF)}  (Chandelier {CHANDELIER_ATR_PERIOD}/{CHANDELIER_MULTIPLIER} + PVT + dual-RSI)",
        f"RSI regime:  {_code(f'fast({RSI_FAST_PERIOD}) vs slow({RSI_SLOW_PERIOD})')}",
```

- [ ] **Step 3: Fix `webui.py`'s `get_strategy_config`**

Replace lines 232-246 (the docstring through `min_volume_multiplier`):

```python
def get_strategy_config() -> dict:
    """Return dashboard-safe strategy/runtime configuration for Binocular Trend Confluence v1."""
    return {
        "strategy": _safe_config_value("STRATEGY_NAME", "Binocular Trend Confluence v1"),
        "trend_tf": _safe_config_value("TREND_TF", "—"),
        "entry_tf": _safe_config_value("ENTRY_TF", "—"),
        "chandelier_atr_period": _safe_config_value("CHANDELIER_ATR_PERIOD", "—"),
        "chandelier_multiplier": _safe_config_value("CHANDELIER_MULTIPLIER", "—"),
        "pvt_signal_length": _safe_config_value("PVT_SIGNAL_LENGTH", "—"),
        "rsi_fast_period": _safe_config_value("RSI_FAST_PERIOD", "—"),
        "rsi_slow_period": _safe_config_value("RSI_SLOW_PERIOD", "—"),
        "zone_swing_length": _safe_config_value("ZONE_SWING_LENGTH", "—"),
        "zone_box_width": _safe_config_value("ZONE_BOX_WIDTH", "—"),
        "zone_proximity_atr_mult": _safe_config_value("ZONE_PROXIMITY_ATR_MULT", "—"),
        "entry_buffer_pct": _safe_config_value("ENTRY_BUFFER_PCT", "—"),
```

(Leave the rest of the function — `top_n_coins` through `dry_run` — untouched; only the strategy-specific fields above change.)

- [ ] **Step 4: Verify both modules import cleanly**

Run: `python -c "import bot; import webui"`
Expected: no traceback. (`bot.py` may print a warning if `TELEGRAM_TOKEN` is unset in your local `.env` — that's expected outside the server and not a failure of this task.)

- [ ] **Step 5: Commit**

```bash
git add bot.py webui.py
git commit -m "fix: update /status and dashboard fields for Binocular Trend Confluence v1"
```

---

### Task 9: Fix `scripts/backtest_simple_strategy.py`

**Files:**
- Modify: `scripts/backtest_simple_strategy.py:1-2,33-39,256`

**Interfaces:**
- Consumes: `ZONE_ATR_PERIOD`, `ZONE_SWING_LENGTH`, `RSI_SLOW_PERIOD` from `config` (Task 2) in place of `TREND_EMA_PERIOD, ENTRY_EMA_PERIOD, PULLBACK_LOOKBACK_BARS`.

- [ ] **Step 1: Update the module docstring**

Replace line 2:

```python
Backtest utility for Binocular Trend Confluence v1.
```

- [ ] **Step 2: Fix the config import**

Replace lines 33-39:

```python
from config import (
    ENTRY_TF, TREND_TF, BTC_FILTER_SYMBOL, BTC_FILTER_TF,
    SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    ZONE_ATR_PERIOD, ZONE_SWING_LENGTH, RSI_SLOW_PERIOD,
    TREND_KLINE_COUNT, ENTRY_KLINE_COUNT,
)
```

- [ ] **Step 3: Fix the `min_start` computation**

Replace line 256:

```python
    min_start = max(ZONE_ATR_PERIOD + ZONE_SWING_LENGTH * 2 + 10, RSI_SLOW_PERIOD + 20)
```

- [ ] **Step 4: Verify the script imports cleanly**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import backtest_simple_strategy"`
Expected: no traceback.

- [ ] **Step 5: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "fix: update backtest script's min_start for the new strategy's warm-up requirements"
```

---

### Task 10: Full verification pass

**Files:** none (verification only)

**Interfaces:** none

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests PASS, including `tests/test_indicators.py` (unaffected — `calculate_supertrend` is still used by `build_btc_context`), `tests/test_binocular_indicators.py`, `tests/test_strategy_binocular.py`, `tests/test_btc_filter.py`, and every other pre-existing test file (`tests/test_scalper_v3_strategy.py`, `tests/test_mexc_client.py`, etc. — untouched by this work).

- [ ] **Step 2: Verify every affected module imports cleanly**

Run: `python -c "import config; import strategy; import main; import bot; import webui; import database"`
Expected: no traceback.

- [ ] **Step 3: Run the backtest against a handful of symbols**

Run: `python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT --days 14`
Expected: runs to completion, prints achieved history length and trade statistics (may be zero trades — that's acceptable for a first run against live market data with a brand-new strategy; the point is no crash and no future-data-leakage errors).

- [ ] **Step 4: Dry-run boot check**

Run: `DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py` (interrupt with Ctrl+C after confirming clean startup logs — this is a local sanity check, not a long-running session)
Expected: startup logs show `Strategy: Binocular Trend Confluence v1`, no import errors, no unhandled exceptions in the first scan cycle.

- [ ] **Step 5: Confirm the backup branch is intact**

Run: `git log --oneline origin/backup/supertrend-pullback-v1 -1` and `git diff origin/backup/supertrend-pullback-v1 -- strategy.py config.py | head -5` (compare against the pre-Task-2 version, not current `main` — some diff is expected now since `main` has moved on)
Expected: the backup branch's commit is unchanged since Task 1 and still contains the old `strategy.py`/`config.py` in full.

No commit for this task — verification only, no code changes.
