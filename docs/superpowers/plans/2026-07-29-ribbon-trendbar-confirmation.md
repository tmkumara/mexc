# Ribbon-Flip + Trend-Bar Confirmation v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bot's active strategy (`strategy.py`) with the user's actual manual trading rule: a 6-EMA ribbon flip ("arrow 1") confirmed within a bounded lookback by a Price-Action-Channel "Trend Bar" ("arrow 2"). Single 15m timeframe, no BTC filter, no zone confluence, no Chandelier/PVT/dual-RSI — this fully replaces the just-deployed Binocular Trend Confluence v1 strategy, which fired ~0 signals live and showed a fragile LONG/SHORT asymmetry once loosened.

**Architecture:** `strategy.py` keeps its single-file shape. Two new pure indicator functions (`calculate_ema_ribbon`, `calculate_trend_bar`) plus one stateless detector (`_detect_ribbon_flip`, walks backward over a bounded window to find a recent flip with no persisted arm state — same "recompute fresh every scan" philosophy the bot has used since the first migration) replace the entire zone/Chandelier/PVT/dual-RSI/BTC-filter pipeline. `evaluate_symbol`'s signature and the `Signal` dataclass are unchanged; `BtcContext` is deleted. TP/SL keep the existing ROI framework, SL sourced from the swing extreme over the flip-to-confirmation window instead of a zone boundary. Six files outside `strategy.py` have confirmed real dependencies on what's being removed (found via an explicit cross-reference search, not assumed) and are fixed as part of this plan: `main.py`, `bot.py`, `webui.py` (Python **and** inline JS/HTML), `mexc_ws_client.py`, `scripts/backtest_simple_strategy.py`. `backtest/engine.py` needs no change (it only imports `calculate_supertrend`, which stays).

**Tech Stack:** Python, pandas, NumPy, pytest, monkeypatch-based fixtures (no network calls in tests).

## Global Constraints

- Only completed candles are ever evaluated — always `df.iloc[:-1]` before computing anything; never read the still-forming last candle.
- No TA-Lib or other paid/closed-source indicator dependency — NumPy/pandas only.
- `evaluate_symbol(symbol: str, btc_context=None, reject_sink: dict | None = None) -> Signal | None` — signature must not change (the `btc_context` parameter stays, unused, so every existing caller keeps working unmodified).
- `Signal` dataclass must not change shape. `BtcContext` dataclass is deleted — nothing constructs one anymore.
- Never tighten a computed stop-loss artificially to force a signal through — reject instead (existing rule, unchanged).
- `calculate_ema`, `calculate_rsi`, `calculate_atr`, `calculate_supertrend` all stay in `strategy.py` even though the main pipeline no longer calls some of them — `calculate_atr` is used internally by the new SL buffer, `calculate_supertrend` is imported directly by the unrelated `backtest/engine.py` (Super Scalper v3's backtester), `calculate_rsi` has its own generic test coverage. None of the four have any coupling to the code being removed; do not delete any of them.
- `DRY_RUN` defaults to `true` — never assume live trading is safe during this work.
- Full spec: `docs/superpowers/specs/2026-07-29-ribbon-trendbar-confirmation-design.md`.

---

### Task 1: Cut the backup branch

**Files:** none (git operation only)

- [ ] **Step 1: Confirm current branch and clean working tree**

Run: `git status` and `git log --oneline -3`
Expected: on `main`, HEAD is the currently-deployed Binocular Trend Confluence v1 code (commit `3365c11` or later — whatever `main` is at when this task starts).

- [ ] **Step 2: Cut the backup branch from current HEAD**

```bash
git branch backup/binocular-trend-confluence-v1
git push -u origin backup/binocular-trend-confluence-v1
```

- [ ] **Step 3: Verify the branch exists on origin and matches current `strategy.py`**

```bash
git fetch origin
git diff main origin/backup/binocular-trend-confluence-v1 -- strategy.py config.py
```
Expected: no diff output.

No commit for this task.

---

### Task 2: `config.py` — remove old strategy settings, add ribbon/trend-bar settings

**Files:**
- Modify: `config.py:57-96` (strategy section)
- Modify: `.env.example` (strategy block)

**Interfaces:**
- Produces: `RIBBON_MA1_LEN`, `RIBBON_MA2_LEN`, `RIBBON_MA3_LEN`, `RIBBON_MA4_LEN`, `RIBBON_MA5_LEN`, `RIBBON_BASELINE_LEN`, `RIBBON_LOOKBACK_BARS`, `TREND_BAR_PAC_LENGTH`, `ATR_PERIOD` — all consumed by Task 3/4.
- Keeps unchanged: `ENTRY_KLINE_COUNT`, `SL_ATR_BUFFER_MULTIPLIER`, `TARGET_ROI_PCT`, `MAX_SL_ROI_PCT`, `LEVERAGE`, `TP_PRICE_PCT`, `MAX_SL_PRICE_PCT`, `MIN_RR`, and every scan/coin-pool/cooldown/expiry constant.
- Changes: `ENTRY_TF` default `"5m"` → `"15m"`.
- Removes: `TREND_TF`, `TREND_KLINE_COUNT`, `TREND_EMA_PERIOD`, `TREND_SUPERTREND_ATR_PERIOD`, `TREND_SUPERTREND_MULTIPLIER`, `ENABLE_BTC_FILTER`, `BTC_FILTER_SYMBOL`, `BTC_FILTER_TF`, `BTC_MAX_OPPOSING_MOVE_PCT`, `BTC_MAX_SINGLE_CANDLE_MOVE_PCT`, `BTC_MAX_THREE_CANDLE_MOVE_PCT`, `CHANDELIER_ATR_PERIOD`, `CHANDELIER_MULTIPLIER`, `PVT_SIGNAL_LENGTH`, `PVT_SIGNAL_TYPE`, `RSI_FAST_PERIOD`, `RSI_SLOW_PERIOD`, `ZONE_SWING_LENGTH`, `ZONE_ATR_PERIOD`, `ZONE_BOX_WIDTH`, `ZONE_PROXIMITY_ATR_MULT`, `ZONE_MAX_AGE_BARS`, `ENTRY_BUFFER_PCT`.

- [ ] **Step 1: Edit `config.py`**

Replace lines 57-96 (from the `STRATEGY_NAME` comment through the `ZONE_MAX_AGE_BARS` line) with:

```python
# ── Strategy: Ribbon-Flip Trend-Bar Confirmation v1 ─────────────────
STRATEGY_NAME: str = os.getenv(
    "STRATEGY_NAME",
    "Ribbon-Flip Trend-Bar Confirmation v1",
)

ENTRY_TF: str = os.getenv("ENTRY_TF", "15m")
ENTRY_KLINE_COUNT: int = int(os.getenv("ENTRY_KLINE_COUNT", "120"))

# 6-EMA ribbon (Pine script defaults) -- "arrow 1"
RIBBON_MA1_LEN: int = int(os.getenv("RIBBON_MA1_LEN", "30"))
RIBBON_MA2_LEN: int = int(os.getenv("RIBBON_MA2_LEN", "35"))
RIBBON_MA3_LEN: int = int(os.getenv("RIBBON_MA3_LEN", "40"))
RIBBON_MA4_LEN: int = int(os.getenv("RIBBON_MA4_LEN", "45"))
RIBBON_MA5_LEN: int = int(os.getenv("RIBBON_MA5_LEN", "50"))
RIBBON_BASELINE_LEN: int = int(os.getenv("RIBBON_BASELINE_LEN", "60"))

# How many bars back a ribbon flip may have happened and still count as
# "recent enough" to arm a setup -- bounds the "wait for arrow 2" step
# without persisted arm state (recomputed fresh every scan).
RIBBON_LOOKBACK_BARS: int = int(os.getenv("RIBBON_LOOKBACK_BARS", "12"))

# Price-Action-Channel "Trend Bar" confirmation -- "arrow 2"
TREND_BAR_PAC_LENGTH: int = int(os.getenv("TREND_BAR_PAC_LENGTH", "50"))

# ATR period used for the structural-SL buffer and candidate scoring.
ATR_PERIOD: int = int(os.getenv("ATR_PERIOD", "14"))

SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("SL_ATR_BUFFER_MULTIPLIER", "0.10"))
```

- [ ] **Step 2: Verify config still imports cleanly**

Run: `python -c "import config; print(config.STRATEGY_NAME, config.ENTRY_TF, config.RIBBON_BASELINE_LEN, config.RIBBON_LOOKBACK_BARS, config.TREND_BAR_PAC_LENGTH)"`
Expected: prints `Ribbon-Flip Trend-Bar Confirmation v1 15m 60 12 50` with no traceback.

- [ ] **Step 3: Update `.env.example`**

Find the `# ── Binocular Trend Confluence v1 ──` block (added by the previous migration) and replace it with:

```
# ── Ribbon-Flip Trend-Bar Confirmation v1 -- see config.py for full defaults ──
# 6-EMA ribbon (30/35/40/45/50 vs 60) flips direction ("arrow 1"); a
# Price-Action-Channel Trend Bar must confirm the same direction within
# RIBBON_LOOKBACK_BARS ("arrow 2") to fire.
STRATEGY_NAME=Ribbon-Flip Trend-Bar Confirmation v1
ENTRY_TF=15m
ENTRY_KLINE_COUNT=120
RIBBON_MA1_LEN=30
RIBBON_MA2_LEN=35
RIBBON_MA3_LEN=40
RIBBON_MA4_LEN=45
RIBBON_MA5_LEN=50
RIBBON_BASELINE_LEN=60
RIBBON_LOOKBACK_BARS=12
TREND_BAR_PAC_LENGTH=50
ATR_PERIOD=14
SL_ATR_BUFFER_MULTIPLIER=0.10

# 15% ROI at 20x requires approximately 0.75% price movement.
# 10% stop ROI at 20x equals approximately 0.50% price movement.
TARGET_ROI_PCT=15.0
MAX_SL_ROI_PCT=10.0
LEVERAGE=20
MIN_RR=1.5
```

Also remove any `ENABLE_BTC_FILTER`/`BTC_FILTER_*`/`BTC_MAX_*` lines further down the file if present (search the whole file, not just the strategy block — the BTC filter section may be a separate `# ── BTC market safety filter ──` block elsewhere in `.env.example`).

- [ ] **Step 4: Commit**

```bash
git add config.py .env.example
git commit -m "config: swap Binocular Trend Confluence settings for Ribbon-Flip Trend-Bar Confirmation"
```

---

### Task 3: New indicators — EMA ribbon, Trend Bar, ribbon-flip detection

**Files:**
- Modify: `strategy.py` (add functions after `calculate_supertrend`, i.e. after current line 123, before `calculate_chandelier_exit` which this task's Task 4 sibling will later delete)
- Create: `tests/test_ribbon_trendbar_indicators.py`

**Interfaces:**
- Consumes: `calculate_ema` (existing, unchanged)
- Produces:
  - `calculate_ema_ribbon(df: pd.DataFrame, lengths: tuple[int, int, int, int, int], baseline_length: int) -> pd.DataFrame` with columns `ma1, ma2, ma3, ma4, ma5, baseline`
  - `calculate_trend_bar(df: pd.DataFrame, pac_length: int) -> pd.Series` — values `"green"`, `"red"`, or `"gray"` per bar, same index as `df`
  - `_detect_ribbon_flip(df: pd.DataFrame, lengths: tuple[int, int, int, int, int], baseline_length: int, lookback_bars: int) -> tuple[str | None, int | None]` — `(direction, flip_index)` or `(None, None)`
- These are consumed by Task 4's rewritten `evaluate_symbol`/`_calculate_tp_sl`/`_score_candidate`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ribbon_trendbar_indicators.py`:

```python
import numpy as np
import pandas as pd
import pytest

from strategy import calculate_ema_ribbon, calculate_trend_bar, _detect_ribbon_flip

LENGTHS = (30, 35, 40, 45, 50)
BASELINE = 60


def _flat_df(n: int, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open": np.full(n, price), "high": np.full(n, price + 0.1),
        "low": np.full(n, price - 0.1), "close": np.full(n, price),
        "volume": np.full(n, 1000.0),
    })


def test_ema_ribbon_returns_all_six_series():
    df = _flat_df(80)
    ribbon = calculate_ema_ribbon(df, LENGTHS, BASELINE)
    for col in ("ma1", "ma2", "ma3", "ma4", "ma5", "baseline"):
        assert col in ribbon.columns
        assert len(ribbon[col]) == len(df)
    # A flat series converges every EMA to the same price.
    assert ribbon["ma1"].iloc[-1] == pytest.approx(100.0, abs=1e-6)
    assert ribbon["baseline"].iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_trend_bar_green_when_candle_above_channel():
    n = 60
    df = _flat_df(n, price=100.0)
    # Push the final candle's entire range far above the trailing PAC.
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [110.0, 111.0, 109.5, 110.5]
    trend_bar = calculate_trend_bar(df, pac_length=50)
    assert trend_bar.iloc[-1] == "green"


def test_trend_bar_red_when_candle_below_channel():
    n = 60
    df = _flat_df(n, price=100.0)
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [90.0, 90.5, 89.0, 89.5]
    trend_bar = calculate_trend_bar(df, pac_length=50)
    assert trend_bar.iloc[-1] == "red"


def test_trend_bar_gray_when_candle_straddles_channel():
    df = _flat_df(60, price=100.0)
    trend_bar = calculate_trend_bar(df, pac_length=50)
    # A flat series never clears its own trailing channel -- always gray.
    assert trend_bar.iloc[-1] == "gray"


def test_trend_bar_does_not_use_future_data():
    n = 60
    df = _flat_df(n, price=100.0)
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [110.0, 111.0, 109.5, 110.5]
    full = calculate_trend_bar(df, pac_length=50)
    partial = calculate_trend_bar(df.iloc[:40].copy(), pac_length=50)
    for i in range(40):
        assert full.iloc[i] == partial.iloc[i]


def test_detect_ribbon_flip_finds_recent_bullish_flip():
    # 60 flat bars, then a strong 20-bar push -- flips the ribbon bullish.
    n = 80
    closes = np.full(n, 100.0)
    closes[60:] = 100.0 + np.arange(1, 21) * 3.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction == "LONG"
    assert flip_index is not None
    assert flip_index >= n - 1 - 12


def test_detect_ribbon_flip_finds_recent_bearish_flip():
    n = 80
    closes = np.full(n, 100.0)
    closes[60:] = 100.0 - np.arange(1, 21) * 3.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction == "SHORT"
    assert flip_index is not None


def test_detect_ribbon_flip_rejects_when_flip_outside_lookback_window():
    # Same bullish setup as above, but query with lookback_bars=1 -- the
    # flip (which needs several bars of the push to complete) will be
    # older than a 1-bar window even though the ribbon IS currently aligned.
    n = 80
    closes = np.full(n, 100.0)
    closes[60:] = 100.0 + np.arange(1, 21) * 3.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=1)
    assert direction is None


def test_detect_ribbon_flip_rejects_when_ribbon_not_currently_aligned():
    df = _flat_df(80, price=100.0)
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction is None
    assert flip_index is None


def test_detect_ribbon_flip_finds_latest_flip_after_a_revert():
    # Flip bullish, revert to bearish, flip bullish again -- must return
    # the SECOND flip's index, not the first.
    n = 140
    closes = np.full(n, 100.0)
    closes[40:70] = 100.0 + np.arange(1, 31) * 3.0     # first bullish push
    closes[70:100] = closes[69] - np.arange(1, 31) * 3.0  # revert bearish
    closes[100:130] = closes[99] + np.arange(1, 31) * 3.0  # second bullish push
    closes[130:] = closes[129]
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction == "LONG"
    # The second flip must be found -- well past the midpoint of the series.
    assert flip_index > 100
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ribbon_trendbar_indicators.py -v`
Expected: `ImportError` — none of the three functions exist yet.

- [ ] **Step 3: Add the indicator functions to `strategy.py`**

Insert immediately after `calculate_supertrend` (after current line 123), before the `calculate_chandelier_exit` function (which Task 4 deletes):

```python
def calculate_ema_ribbon(
    df: pd.DataFrame, lengths: tuple[int, int, int, int, int], baseline_length: int
) -> pd.DataFrame:
    close = df["close"]
    ma1, ma2, ma3, ma4, ma5 = (calculate_ema(close, length) for length in lengths)
    baseline = calculate_ema(close, baseline_length)
    return pd.DataFrame(
        {"ma1": ma1, "ma2": ma2, "ma3": ma3, "ma4": ma4, "ma5": ma5, "baseline": baseline},
        index=df.index,
    )


def calculate_trend_bar(df: pd.DataFrame, pac_length: int) -> pd.Series:
    pac_hi = calculate_ema(df["high"], pac_length)
    pac_lo = calculate_ema(df["low"], pac_length)
    high, low = df["high"], df["low"]

    color = pd.Series("gray", index=df.index, dtype=object)
    color[(low > pac_hi) & (high > pac_hi)] = "green"
    color[(high < pac_lo) & (low < pac_lo)] = "red"
    return color


def _detect_ribbon_flip(
    df: pd.DataFrame,
    lengths: tuple[int, int, int, int, int],
    baseline_length: int,
    lookback_bars: int,
) -> tuple[str | None, int | None]:
    ribbon = calculate_ema_ribbon(df, lengths, baseline_length)
    ma1, ma2, ma3, ma4, ma5, baseline = (
        ribbon["ma1"], ribbon["ma2"], ribbon["ma3"], ribbon["ma4"], ribbon["ma5"], ribbon["baseline"]
    )
    bullish = (ma1 > baseline) & (ma2 > baseline) & (ma3 > baseline) & (ma4 > baseline) & (ma5 > baseline)
    bearish = (ma1 < baseline) & (ma2 < baseline) & (ma3 < baseline) & (ma4 < baseline) & (ma5 < baseline)

    n = len(df)
    last = n - 1
    stop = max(last - lookback_bars, 0)

    if bool(bullish.iloc[last]):
        for j in range(last, stop - 1, -1):
            if not bool(bullish.iloc[j]):
                break
            if j == 0 or not bool(bullish.iloc[j - 1]):
                return "LONG", j
        return None, None

    if bool(bearish.iloc[last]):
        for j in range(last, stop - 1, -1):
            if not bool(bearish.iloc[j]):
                break
            if j == 0 or not bool(bearish.iloc[j - 1]):
                return "SHORT", j
        return None, None

    return None, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ribbon_trendbar_indicators.py -v`
Expected: all 10 tests PASS. If `test_detect_ribbon_flip_finds_recent_bullish_flip`/`_bearish_flip` or the revert test don't pass with the exact fixture data given, this is a numeric-constant check (push magnitude/duration), not a logic bug — verify the `_detect_ribbon_flip` loop matches the pseudocode above exactly before adjusting the test fixture's push size.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_ribbon_trendbar_indicators.py
git commit -m "feat: add EMA ribbon, Trend Bar, and ribbon-flip detection indicators"
```

---

### Task 4: Rewrite `strategy.py`'s pipeline

**Files:**
- Modify: `strategy.py` (module docstring, import block, delete zone/Chandelier/PVT/BTC-filter code, rewrite `_calculate_tp_sl`/`_score_candidate`/`evaluate_symbol`, delete `_reason_bucket`)

**Interfaces:**
- Consumes: `calculate_ema_ribbon`, `calculate_trend_bar`, `_detect_ribbon_flip` (Task 3); config constants from Task 2.
- Produces (replacing everything from `calculate_chandelier_exit` through `_btc_filter_ok`):
  - `_calculate_tp_sl(direction: str, entry: float, df: pd.DataFrame, flip_index: int, atr_last: float) -> tuple[float, float] | None`
  - `_score_candidate(direction: str, details: dict, rr: float) -> float`
  - `evaluate_symbol(symbol, btc_context=None, reject_sink=None) -> Signal | None` (same signature, new pipeline body)
- `Signal` dataclass, `valid_trade_geometry`, `direction_slot_available`, `_calc_rr`, `_roi_pct`, `_bump` are **unchanged** — do not modify them in this task.
- `BtcContext` dataclass, `build_btc_context`, `_btc_filter_ok`, `build_zones`, `find_pivot_highs`, `find_pivot_lows`, `_find_confluence_zone`, `calculate_chandelier_exit`, `calculate_pvt`, `calculate_pvt_signal`, `_detect_trigger`, `_reason_bucket` are all **deleted** in this task.

- [ ] **Step 1: Update the module docstring**

Replace lines 1-9:

```python
"""
Ribbon-Flip Trend-Bar Confirmation v1.

A 6-EMA ribbon (30/35/40/45/50 vs a 60-period baseline) flipping fully
bullish or bearish is "arrow 1"; a Price-Action-Channel Trend Bar
confirming the same direction within RIBBON_LOOKBACK_BARS of that flip is
"arrow 2". If the ribbon reverts before the Trend Bar confirms, the setup
is invalidated -- recomputed fresh every scan, no persisted arm state.
Only completed candles are ever used. See
docs/superpowers/specs/2026-07-29-ribbon-trendbar-confirmation-design.md.
"""
```

- [ ] **Step 2: Delete the `BtcContext` dataclass**

Delete lines 41-47 (the `@dataclass class BtcContext:` block).

- [ ] **Step 3: Delete the superseded indicator functions**

Delete these functions entirely (all fully superseded, no longer used anywhere): `calculate_chandelier_exit`, `calculate_pvt`, `calculate_pvt_signal`, `find_pivot_highs`, `find_pivot_lows`, `build_zones`. Leave `calculate_ema`, `calculate_rsi`, `calculate_atr`, `calculate_supertrend`, and the three functions Task 3 just added (`calculate_ema_ribbon`, `calculate_trend_bar`, `_detect_ribbon_flip`) exactly where they are.

- [ ] **Step 4: Replace the import block**

Replace the `# ── evaluate_symbol pipeline ──` import block (currently importing `TREND_TF, ENTRY_TF, TREND_KLINE_COUNT, ENTRY_KLINE_COUNT, CHANDELIER_ATR_PERIOD, ...` etc.) with:

```python
from market_data import get_market_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT,
    RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN,
    RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS,
    TREND_BAR_PAC_LENGTH, ATR_PERIOD,
    SL_ATR_BUFFER_MULTIPLIER, LEVERAGE, TP_PRICE_PCT, MAX_SL_PRICE_PCT, MIN_RR,
)
```

- [ ] **Step 5: Delete `_detect_trigger` and `_find_confluence_zone`**

Delete both functions entirely (superseded by `_detect_ribbon_flip` + the Trend Bar check inlined into `evaluate_symbol`).

- [ ] **Step 6: Replace `_calculate_tp_sl`**

```python
def _calculate_tp_sl(
    direction: str, entry: float, df: pd.DataFrame, flip_index: int, atr_last: float
) -> tuple[float, float] | None:
    window_low = float(df["low"].iloc[flip_index:].min())
    window_high = float(df["high"].iloc[flip_index:].max())

    if direction == "LONG":
        tp = entry * (1 + TP_PRICE_PCT)
        structural_sl = window_low - atr_last * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl >= entry:
            return None
        if (entry - structural_sl) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl
    else:
        tp = entry * (1 - TP_PRICE_PCT)
        structural_sl = window_high + atr_last * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl <= entry:
            return None
        if (structural_sl - entry) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl
```

- [ ] **Step 7: Replace `_score_candidate`**

```python
def _score_candidate(direction: str, details: dict, rr: float) -> float:
    atr = max(details["atr"], 1e-9)

    separation = abs(details["ma5_last"] - details["baseline_last"])
    alignment_quality = min(1.0, separation / (atr * 2.0))
    score = 40.0 * alignment_quality

    freshness = 1.0 - min(1.0, details["bars_since_flip"] / max(RIBBON_LOOKBACK_BARS, 1))
    score += 20.0 * freshness

    if direction == "LONG":
        clearance = (details["low_last"] - details["pac_hi_last"]) / atr
    else:
        clearance = (details["pac_lo_last"] - details["high_last"]) / atr
    trend_bar_quality = min(1.0, max(0.0, clearance / 2.0))
    score += 20.0 * trend_bar_quality

    rr_quality = min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0 else (1.0 if rr >= MIN_RR else 0.0)
    score += 20.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)
```

- [ ] **Step 8: Delete `_reason_bucket`**

Delete the function entirely. The new pipeline has only two trigger-stage rejects (`no_ribbon_flip`, `no_trend_bar_confirmation`) with no free-text reason to categorize, so `evaluate_symbol` bumps `reject_sink` with literal string keys directly instead of parsing a reason string through a bucketing function.

- [ ] **Step 9: Rewrite `evaluate_symbol`**

Replace the entire function body (keep the `def evaluate_symbol(...)` signature and the outer `try/except` unchanged):

```python
def evaluate_symbol(
    symbol: str,
    btc_context=None,
    reject_sink: dict | None = None,
) -> Signal | None:
    try:
        raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if raw is None or raw.empty:
            logger.debug("[REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed = raw.iloc[:-1].copy()

        if len(closed) < RIBBON_BASELINE_LEN + RIBBON_LOOKBACK_BARS + 10:
            logger.debug("[REJECT] %s insufficient candle history", symbol)
            _bump(reject_sink, "insufficient_history")
            return None

        lengths = (RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN)
        direction, flip_index = _detect_ribbon_flip(closed, lengths, RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS)
        if direction is None:
            logger.debug("[REJECT] %s no ribbon flip", symbol)
            _bump(reject_sink, "no_ribbon_flip")
            return None

        trend_bar = calculate_trend_bar(closed, TREND_BAR_PAC_LENGTH)
        current_color = trend_bar.iloc[-1]
        expected_color = "green" if direction == "LONG" else "red"
        if current_color != expected_color:
            logger.debug("[REJECT] %s no trend bar confirmation", symbol)
            _bump(reject_sink, "no_trend_bar_confirmation")
            return None

        atr_last = float(calculate_atr(closed, ATR_PERIOD).iloc[-1])
        entry = float(closed["close"].iloc[-1])

        tp_sl = _calculate_tp_sl(direction, entry, closed, flip_index, atr_last)
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

        ribbon = calculate_ema_ribbon(closed, lengths, RIBBON_BASELINE_LEN)
        pac_hi = calculate_ema(closed["high"], TREND_BAR_PAC_LENGTH)
        pac_lo = calculate_ema(closed["low"], TREND_BAR_PAC_LENGTH)
        score_details = {
            "ma5_last": float(ribbon["ma5"].iloc[-1]),
            "baseline_last": float(ribbon["baseline"].iloc[-1]),
            "atr": atr_last,
            "bars_since_flip": (len(closed) - 1) - flip_index,
            "low_last": float(closed["low"].iloc[-1]),
            "high_last": float(closed["high"].iloc[-1]),
            "pac_hi_last": float(pac_hi.iloc[-1]),
            "pac_lo_last": float(pac_lo.iloc[-1]),
        }
        score = _score_candidate(direction, score_details, rr)

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
            timeframe_summary="EMA ribbon flip + Trend Bar confirmation",
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

- [ ] **Step 10: Delete `build_btc_context` and `_btc_filter_ok`**

Delete the `# ── BTC market safety filter ──` section header and both functions entirely — nothing left in `strategy.py` after this task references BTC at all.

- [ ] **Step 11: Sanity-check imports**

Run: `python -c "import strategy"`
Expected: no traceback. No fixtures exist yet to exercise `evaluate_symbol` end-to-end — that's Task 5.

- [ ] **Step 12: Commit**

```bash
git add strategy.py
git commit -m "feat: rewrite evaluate_symbol as ribbon-flip + Trend-Bar confirmation, drop zone/Chandelier/PVT/BTC-filter"
```

---

### Task 5: New fixtures + strategy-level tests

**Files:**
- Modify: `tests/strategy_fixtures.py` (remove `make_15m_zone_df`/`make_5m_trigger_df`, add new ribbon/trend-bar fixture + simplify `patch_klines` to single-timeframe)
- Create: `tests/test_strategy_ribbon_trendbar.py`
- Delete: `tests/test_binocular_indicators.py`, `tests/test_strategy_binocular.py`, `tests/test_btc_filter.py`

**Interfaces:**
- Consumes: `evaluate_symbol`, `valid_trade_geometry` (Task 4)
- Produces: `make_ribbon_trendbar_df(direction="LONG", bars=200, base_price=100.0, push_bars=80, push_step=0.3) -> pd.DataFrame`, `patch_klines(monkeypatch, strategy_module, df) -> None` (simplified to single-timeframe — no more interval branching needed since only `ENTRY_TF` is ever fetched now)

- [ ] **Step 1: Delete the three superseded test files**

```bash
git rm tests/test_binocular_indicators.py tests/test_strategy_binocular.py tests/test_btc_filter.py
```

- [ ] **Step 2: Rewrite `tests/strategy_fixtures.py`**

Remove `make_15m_zone_df` and `make_5m_trigger_df` entirely (their only consumers were the three deleted test files). Replace `patch_klines` (now single-timeframe, no interval branching needed) and add the new fixture builder. `make_15m_trend_df` stays untouched (still potentially useful for generic trend-series tests, no reason to remove it, costs nothing to keep).

```python
def patch_klines(monkeypatch, strategy_module, df: pd.DataFrame) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to a single fixture."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        return df

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)


def make_ribbon_trendbar_df(
    direction: str = "LONG",
    bars: int = 200,
    base_price: float = 100.0,
    push_bars: int = 80,
    push_step: float = 0.3,
) -> pd.DataFrame:
    """
    A single-timeframe series: flat/ranging for the first
    `bars - push_bars` candles (keeps the EMA ribbon compressed near
    `base_price`), then a clean `push_step`-per-bar directional push for
    the final `push_bars` candles -- flips the 6-EMA ribbon (all 5 short
    EMAs cross the 60-period baseline) partway through the push, and by
    the end of the push the candle range has cleared far enough beyond
    the 50-period Price-Action-Channel for the Trend Bar to confirm too.
    Ends with one extra duplicated row so callers can safely `iloc[:-1]`.

    Numeric constants here are reasoned, not hand-executed against
    pandas -- same convention as every other fixture in this file. If
    the ribbon flip or Trend Bar confirmation don't land within
    RIBBON_LOOKBACK_BARS of each other for the intended direction, widen
    `push_step`, extend `push_bars`, or narrow the flat-period range and
    re-run; that is expected TDD iteration, not a defect in the test
    itself. Use the debug snippet in the task brief to see exactly where
    `_detect_ribbon_flip`/`calculate_trend_bar` land before adjusting
    constants blindly.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
    flat_n = bars - push_bars

    closes = np.empty(bars)
    closes[:flat_n] = base_price + np.sin(np.arange(flat_n) * 0.3) * 0.2
    for k in range(push_bars):
        closes[flat_n + k] = closes[flat_n - 1] + sign * push_step * (k + 1)

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    wick = 0.1
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - wick
    volumes = np.full(bars, 1000.0)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])
```

- [ ] **Step 3: Write the failing strategy-level tests**

Create `tests/test_strategy_ribbon_trendbar.py`:

```python
import strategy
from strategy import evaluate_symbol, valid_trade_geometry
from tests.strategy_fixtures import make_ribbon_trendbar_df, patch_klines


def test_long_signal_valid(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= 1.5
    assert sig.score > 0.0


def test_long_trade_geometry(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("LONG", sig.entry_price, sig.tp_price, sig.sl_price)


def test_invalid_geometry_rejected():
    assert valid_trade_geometry("LONG", 100.0, 99.0, 101.0) is False
    assert valid_trade_geometry("SHORT", 100.0, 101.0, 99.0) is False
    assert valid_trade_geometry("LONG", 0.0, 101.0, 99.0) is False


def test_risk_formula_matches_roi_targets():
    from config import TP_PRICE_PCT, MAX_SL_PRICE_PCT
    import pytest

    assert TP_PRICE_PCT == pytest.approx(0.0075, abs=1e-9)
    assert MAX_SL_PRICE_PCT == pytest.approx(0.005, abs=1e-9)


def test_long_rejected_without_ribbon_flip(monkeypatch):
    # A flat series never flips the ribbon.
    from tests.strategy_fixtures import make_15m_trend_df
    df = make_15m_trend_df("LONG", bars=200)
    patch_klines(monkeypatch, strategy, df)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_without_trend_bar_confirmation(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    # Force every bar gray -- ribbon may flip, Trend Bar never confirms.
    monkeypatch.setattr(
        strategy, "calculate_trend_bar",
        lambda df, pac_length: __import__("pandas").Series("gray", index=df.index, dtype=object),
    )

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_ribbon_reverts_before_confirmation(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    # Force _detect_ribbon_flip to report no current alignment (as if the
    # ribbon reverted before this scan) -- isolates the "reverted" path
    # without needing to hand-construct a revert-and-confirm fixture.
    monkeypatch.setattr(strategy, "_detect_ribbon_flip", lambda *a, **k: (None, None))

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_stop_too_wide(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "MAX_SL_PRICE_PCT", 1e-9)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_rr_too_low(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None


def test_active_last_candle_is_ignored(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    # Corrupt only the forming (last, duplicated) candle -- evaluate_symbol
    # must still fire using the last COMPLETED candle underneath it.
    df.iloc[-1, df.columns.get_loc("close")] = 1.0
    df.iloc[-1, df.columns.get_loc("high")] = 1.0
    df.iloc[-1, df.columns.get_loc("low")] = 0.5
    patch_klines(monkeypatch, strategy, df)

    sig = evaluate_symbol("TEST_USDT")
    assert sig is not None
    assert sig.direction == "LONG"


def test_short_signal_valid(monkeypatch):
    df = make_ribbon_trendbar_df("SHORT")
    patch_klines(monkeypatch, strategy, df)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price
    assert sig.rr >= 1.5


def test_short_trade_geometry(monkeypatch):
    df = make_ribbon_trendbar_df("SHORT")
    patch_klines(monkeypatch, strategy, df)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("SHORT", sig.entry_price, sig.tp_price, sig.sl_price)


def test_short_rejected_when_rr_too_low(monkeypatch):
    df = make_ribbon_trendbar_df("SHORT")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None
```

- [ ] **Step 4: Run the new tests and record the honest pass/fail state**

`evaluate_symbol`/`_detect_ribbon_flip`/`calculate_trend_bar` were already implemented in Tasks 3-4, so this is the first real exercise of that implementation against realistic fixture data, not a strict red/green cycle. Run: `python -m pytest tests/test_strategy_ribbon_trendbar.py -v`.
Expected: `test_invalid_geometry_rejected`, `test_risk_formula_matches_roi_targets`, `test_long_rejected_without_ribbon_flip`, `test_long_rejected_without_trend_bar_confirmation`, `test_long_rejected_when_ribbon_reverts_before_confirmation`, `test_long_rejected_when_stop_too_wide`, `test_long_rejected_when_rr_too_low`, `test_short_rejected_when_rr_too_low` should already pass (none depend on `make_ribbon_trendbar_df` producing an exact firing signal). `test_long_signal_valid`/`test_short_signal_valid`/geometry/`test_active_last_candle_is_ignored` may fail if the fixture constants don't yet produce a firing signal end-to-end.

- [ ] **Step 5: Tune fixture constants until the signal-valid tests pass**

If `test_long_signal_valid` fails, use a debug script to see which gate is rejecting:

```bash
python -c "
import strategy
from tests.strategy_fixtures import make_ribbon_trendbar_df

df = make_ribbon_trendbar_df('LONG').iloc[:-1]
lengths = (strategy.RIBBON_MA1_LEN, strategy.RIBBON_MA2_LEN, strategy.RIBBON_MA3_LEN, strategy.RIBBON_MA4_LEN, strategy.RIBBON_MA5_LEN)
direction, flip_index = strategy._detect_ribbon_flip(df, lengths, strategy.RIBBON_BASELINE_LEN, strategy.RIBBON_LOOKBACK_BARS)
print('ribbon flip:', direction, flip_index, 'of', len(df), 'bars')

trend_bar = strategy.calculate_trend_bar(df, strategy.TREND_BAR_PAC_LENGTH)
print('trend bar last 15:', trend_bar.iloc[-15:].tolist())
"
```

Adjust `push_step`, `push_bars`, or the flat-period amplitude in `make_ribbon_trendbar_df` based on what the debug output shows (e.g. if `direction` is `None`, the ribbon never fully flipped within the lookback window — widen `push_step` or shorten `push_bars` relative to `RIBBON_LOOKBACK_BARS`; if the ribbon flips but the Trend Bar never turns green, the push isn't clearing the 50-period PAC channel — widen `push_step` or extend `push_bars`) and re-run Step 4. This is the same iterative-tuning convention already used by every other fixture in this file.

- [ ] **Step 6: Run the full test file once more to confirm everything passes**

Run: `python -m pytest tests/test_strategy_ribbon_trendbar.py -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/strategy_fixtures.py tests/test_strategy_ribbon_trendbar.py
git commit -m "test: add ribbon/trend-bar fixtures and strategy tests, delete superseded Binocular tests"
```
(The three `git rm` deletions from Step 1 ride along in this same commit if not already committed separately.)

---

### Task 6: Fix dependents — `main.py`, `bot.py`, `webui.py`, `mexc_ws_client.py`

**Files:**
- Modify: `main.py:38-79` (import block), `main.py:149` (btc_context), `main.py:196` (evaluate_symbol call), `main.py:230-245` (save_signal call), `main.py:529` (startup log)
- Modify: `bot.py:208-223` (cmd_status import), `bot.py:244-246` (status message lines)
- Modify: `webui.py:232-267` (get_strategy_config), `webui.py:786-789` (HTML card labels), `webui.py:970-979` (renderConfig JS)
- Modify: `mexc_ws_client.py:396,419` (run_ws_test dev helper)

**Interfaces:**
- Consumes: `RIBBON_BASELINE_LEN`, `RIBBON_LOOKBACK_BARS`, `TREND_BAR_PAC_LENGTH`, `RIBBON_MA1_LEN..MA5_LEN`, `ENTRY_TF` from `config` (Task 2). No new functions produced.

- [ ] **Step 1: Fix `main.py`'s import block**

Remove `TREND_TF` from the `from config import (...)` block (`main.py:38-79`). Keep everything else in that block unchanged (it imports many unrelated constants).

- [ ] **Step 2: Remove the BTC context call and its pass-through**

Delete `main.py:149`: `btc_context = strategy.build_btc_context()`.

Find the `ThreadPoolExecutor`/`executor.map` call that currently reads:
```python
lambda i: strategy.evaluate_symbol(to_scan[i], btc_context, reject_sink=reject_maps[i]),
```
Change it to:
```python
lambda i: strategy.evaluate_symbol(to_scan[i], reject_sink=reject_maps[i]),
```
(`btc_context` defaults to `None` in `evaluate_symbol`'s signature, so omitting it is equivalent and cleaner than passing `None` explicitly.)

- [ ] **Step 3: Fix the `save_signal` call**

Replace `main.py:238`:
```python
                trend_timeframe=TREND_TF,
```
with:
```python
                trend_timeframe=ENTRY_TF,
```
(Same value as `entry_timeframe` on the line above it — no database schema change, the column just always mirrors the single timeframe now.)

- [ ] **Step 4: Fix the startup log**

Delete `main.py:529`: `logger.info("Trend TF: %s", TREND_TF)`. The `Entry TF` line immediately below it stays.

- [ ] **Step 5: Run a quick import + syntax check on `main.py`**

Run: `python -c "import ast; ast.parse(open('main.py').read())"` (full `import main` needs `.env`/network context not necessarily available locally — a syntax/reference-free parse is a safe first check; Task 8 does the full import check).

- [ ] **Step 6: Fix `bot.py`'s `cmd_status` import block**

Replace the import block (`bot.py:208-223`):

```python
    from config import (
        STRATEGY_NAME,
        ENTRY_TF,
        RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS, TREND_BAR_PAC_LENGTH,
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

- [ ] **Step 7: Fix `bot.py`'s status message lines**

Replace lines 244-246 (`"Zone TF: ..."`, `"Trigger TF: ..."`, `"RSI regime: ..."`):

```python
        f"TF:          {_code(ENTRY_TF)}  (Ribbon 30/35/40/45/50 vs {RIBBON_BASELINE_LEN})",
        f"Confirm:     {_code(f'Trend Bar (PAC {TREND_BAR_PAC_LENGTH}) within {RIBBON_LOOKBACK_BARS} bars')}",
```

- [ ] **Step 8: Fix `webui.py`'s `get_strategy_config`**

Replace lines 232-246 (docstring through `entry_buffer_pct`):

```python
def get_strategy_config() -> dict:
    """Return dashboard-safe strategy/runtime configuration for Ribbon-Flip Trend-Bar Confirmation v1."""
    return {
        "strategy": _safe_config_value("STRATEGY_NAME", "Ribbon-Flip Trend-Bar Confirmation v1"),
        "entry_tf": _safe_config_value("ENTRY_TF", "—"),
        "ribbon_baseline_len": _safe_config_value("RIBBON_BASELINE_LEN", "—"),
        "ribbon_lookback_bars": _safe_config_value("RIBBON_LOOKBACK_BARS", "—"),
        "trend_bar_pac_length": _safe_config_value("TREND_BAR_PAC_LENGTH", "—"),
```

Delete the `"enable_btc_filter": _safe_config_value("ENABLE_BTC_FILTER", False),` line further down (currently `webui.py:264`). Leave every other field (`top_n_coins` through `dry_run`) untouched.

- [ ] **Step 9: Fix `webui.py`'s dashboard HTML card labels**

Replace lines 787-788:

```html
    <div class="card"><div class="card-label">Ribbon Baseline</div><div class="card-value purple" id="cfg-quality">—</div><div class="card-small">EMA length (arrow 1)</div></div>
    <div class="card"><div class="card-label">Trend Bar</div><div class="card-value green" id="cfg-confirm">—</div><div class="card-small" id="cfg-confirm-sub">—</div></div>
```

- [ ] **Step 10: Fix `webui.py`'s `renderConfig()` JS**

Replace lines 973-976:

```js
  set("cfg-tf", `${c.entry_tf}`);
  set("cfg-quality", `EMA(${c.ribbon_baseline_len})`);
  set("cfg-confirm", `PAC(${c.trend_bar_pac_length})`);
  set("cfg-confirm-sub", `Ribbon flip within ${c.ribbon_lookback_bars} bars, confirmed by Trend Bar`);
```

- [ ] **Step 11: Fix `mexc_ws_client.py`'s dev helper**

Replace `mexc_ws_client.py:396`:
```python
    from config import WS_TEST_SYMBOLS, ENTRY_TF, TREND_TF, CANDLE_CACHE_LIMIT
```
with:
```python
    from config import WS_TEST_SYMBOLS, ENTRY_TF, CANDLE_CACHE_LIMIT
```
And `mexc_ws_client.py:419`:
```python
        app_intervals=[ENTRY_TF, TREND_TF],
```
with:
```python
        app_intervals=[ENTRY_TF],
```

- [ ] **Step 12: Verify all four modules import cleanly**

Run: `python -c "import config; import strategy; import main; import bot; import webui; import mexc_ws_client"`
Expected: no traceback. (`bot.py` may print a warning if `TELEGRAM_TOKEN` is unset locally — expected outside the server, not a failure.)

- [ ] **Step 13: Commit**

```bash
git add main.py bot.py webui.py mexc_ws_client.py
git commit -m "fix: update main.py/bot.py/webui.py/mexc_ws_client.py for Ribbon-Flip Trend-Bar Confirmation v1"
```

---

### Task 7: Rewrite `scripts/backtest_simple_strategy.py`

**Files:**
- Modify: `scripts/backtest_simple_strategy.py` (module docstring, config import, `backtest_symbol`, `main`, argparse description)

**Interfaces:**
- Consumes: `ENTRY_TF`, `RIBBON_BASELINE_LEN`, `RIBBON_LOOKBACK_BARS` from `config` (Task 2), `evaluate_symbol` (Task 4, same signature).
- Simplification, not addition: this script's design (fake `get_market_klines` across three interval routes, fetch a shared BTC dataframe) collapses to a single-timeframe fetch with one branch.

- [ ] **Step 1: Update the module docstring**

Replace line 2:
```python
Backtest utility for Ribbon-Flip Trend-Bar Confirmation v1.
```

- [ ] **Step 2: Replace the config import**

Replace the current import block (`ENTRY_TF, TREND_TF, BTC_FILTER_SYMBOL, BTC_FILTER_TF, SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES, _TF_MINUTES, ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT, ZONE_ATR_PERIOD, ZONE_SWING_LENGTH, RSI_SLOW_PERIOD`) with:

```python
from config import (
    ENTRY_TF, SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS,
)
```

- [ ] **Step 3: Simplify `backtest_symbol`**

Replace the current signature and body's data-fetching/faking section. Current signature is `backtest_symbol(symbol: str, days: int, df_btc_full: pd.DataFrame) -> list[Trade]` fetching both `TREND_TF` and `ENTRY_TF` series and faking three interval routes. New version:

```python
def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state, since process pool workers
    don't share memory."""
    trades: list[Trade] = []

    df_full = get_klines_extended(symbol, ENTRY_TF, days)

    if df_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(f"[{symbol}] achieved history: {len(df_full)} x {ENTRY_TF} bars", flush=True)

    min_start = RIBBON_BASELINE_LEN + RIBBON_LOOKBACK_BARS + 10
    in_trade_until_idx = -1

    original_get_market_klines = strategy.get_market_klines

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

            sig = strategy.evaluate_symbol(symbol)

            if sig is None:
                continue

            outcome, bars_held = _simulate_outcome(
                sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, df_full, i,
            )
            exit_price = sig.tp_price if outcome == "win" else (
                sig.sl_price if outcome == "loss" else float(df_full["close"].iloc[min(i + bars_held, len(df_full) - 1)])
            )
            gross_roi, net_roi = _roi_with_costs(sig.direction, sig.entry_price, exit_price, outcome)

            trades.append(Trade(
                symbol=symbol, direction=sig.direction, entry_price=sig.entry_price,
                tp_price=sig.tp_price, sl_price=sig.sl_price, rr=sig.rr,
                outcome=outcome, gross_roi_pct=gross_roi, net_roi_pct=net_roi,
            ))

            in_trade_until_idx = i + bars_held
    finally:
        strategy.get_market_klines = original_get_market_klines

    return trades
```

Note: `ENTRY_KLINE_COUNT` must be imported too (add it to the Step 2 import block: `from config import (ENTRY_TF, ENTRY_KLINE_COUNT, SIGNAL_EXPIRE_HOURS, ...)`). `_with_forming_row` currently takes `(df, upto_idx, window_count)` — reuse it unchanged, just called once per bar now instead of twice (15m + BTC) plus a third (5m) branch that no longer exists. `_find_as_of_index` is no longer needed anywhere in this file (it existed solely to align the 15m/BTC timeframes to the 5m walk-forward index) — delete it if nothing else references it (grep first to confirm).

- [ ] **Step 4: Simplify `main()`**

Replace the current `main()` body (BTC dataframe fetch + `ProcessPoolExecutor` submitting `backtest_symbol(symbol, args.days, df_btc_full)`):

```python
def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Ribbon-Flip Trend-Bar Confirmation v1")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=30, help="requested lookback in days (best-effort, paginated via start/end)")
    parser.add_argument("--workers", type=int, default=6, help="parallel worker processes, one symbol each")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- paginated via MEXC start/end)")

    stats = BacktestStats()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(backtest_symbol, symbol, args.days): symbol
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
```

- [ ] **Step 5: Verify the script imports cleanly**

Run: `python -c "import sys; sys.path.insert(0, 'scripts'); import backtest_simple_strategy"`
Expected: no traceback.

- [ ] **Step 6: Commit**

```bash
git add scripts/backtest_simple_strategy.py
git commit -m "refactor: simplify backtest script to single-timeframe, no-BTC-filter"
```

---

### Task 8: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -v`
Expected: all tests PASS, including `tests/test_indicators.py` (unaffected — `calculate_supertrend`/`calculate_rsi`/`calculate_atr`/`calculate_ema` all still exist), `tests/test_ribbon_trendbar_indicators.py`, `tests/test_strategy_ribbon_trendbar.py`, and every other pre-existing test file (`tests/test_scalper_v3_strategy.py`, `tests/test_mexc_client.py`, `tests/test_outcome_check.py`, `tests/test_outcome_replay.py`, `tests/test_relative_strength.py`, `tests/test_super_scalper_v3.py`, `tests/test_bot_formatting.py`, `tests/test_database_direction_counts.py`, etc. — untouched by this work).

- [ ] **Step 2: Verify every affected module imports cleanly**

Run: `python -c "import config; import strategy; import main; import bot; import webui; import database; import mexc_ws_client; import backtest.engine"`
Expected: no traceback. The `backtest.engine` import specifically confirms Task 4's decision to keep `calculate_supertrend` in `strategy.py` was correct — this is the file the earlier cross-reference check found depends on it.

- [ ] **Step 3: Run the backtest against a handful of symbols**

Run: `python scripts/backtest_simple_strategy.py --symbols XRP_USDT DOGE_USDT WLD_USDT --days 60`
Expected: runs to completion, prints achieved history length and trade statistics. Given this strategy has a much shorter warm-up requirement than the previous one (no 15m zone series, no BTC fetch), this should run noticeably faster than the previous strategy's backtest did.

- [ ] **Step 4: Dry-run boot check**

Run: `DRY_RUN=true DRY_RUN_SAVE_SIGNALS=false python main.py` (interrupt with Ctrl+C after confirming clean startup logs — local sanity check only, not a long-running session; do not let this run against real Telegram credentials for more than a few seconds if a real `.env` is present, per the lesson from the previous migration's live-bot Telegram polling conflict)
Expected: startup logs show `Strategy: Ribbon-Flip Trend-Bar Confirmation v1`, `Entry TF: 15m`, no `Trend TF` line, no import errors, no unhandled exceptions in the first scan cycle.

- [ ] **Step 5: Confirm the backup branch is intact**

Run: `git log --oneline origin/backup/binocular-trend-confluence-v1 -1` and compare `strategy.py`/`config.py` against it (`git diff origin/backup/binocular-trend-confluence-v1 -- strategy.py config.py | head -5` — some diff is expected now since `main` has moved on; the point is confirming the branch itself still exists and its commit is unchanged since Task 1).

No commit for this task — verification only, no code changes.
