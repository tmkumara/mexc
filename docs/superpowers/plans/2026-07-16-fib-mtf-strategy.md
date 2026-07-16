# Fibonacci MTF Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and backtest a standalone Fibonacci Multi-Timeframe strategy (4H trend + 1H golden-zone retracement pullback) as a pure research artifact on its own branch, producing a 60-day backtest report — no changes to the live bot.

**Architecture:** A new `fib_strategy.py` mirrors the shape of the existing `strategy.py` (indicators → trend filter → entry confirmation → TP/SL → BTC safety filter → single `evaluate_symbol_fib()` entry point) but is fully standalone, with its own `config_fib.py`. A new `scripts/backtest_fib_strategy.py` reuses the proven bounded-window + `ProcessPoolExecutor` backtest harness pattern from `scripts/backtest_simple_strategy.py` (importing its `get_klines_extended` pagination helper directly rather than duplicating it).

**Tech Stack:** Python, pandas, numpy, pytest, `concurrent.futures.ProcessPoolExecutor`, existing `mexc_client`/`market_data` modules.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-16-fib-mtf-strategy-design.md` — every requirement below traces to a section there.
- `main.py`, `strategy.py`, `config.py`, `bot.py`, `webui.py`, `database.py` are **never modified** in this plan. Verified at the end via `git diff main -- main.py strategy.py config.py bot.py webui.py database.py` being empty.
- `fib_strategy.py` may import pure, stateless indicator helpers from `strategy.py` (`calculate_ema`, `calculate_rsi`, `calculate_atr`) — nothing else from `strategy.py`.
- All new `.env` keys are prefixed `FIB_` so they cannot collide with the live bot's existing config keys in the same `.env` file.
- Every candidate signal must satisfy: LONG `sl < entry < tp`, SHORT `tp < entry < sl`; `rr >= MIN_RR`; SL price distance `<= MAX_SL_ROI_PCT/100/LEVERAGE`.
- Only completed candles are ever evaluated (`iloc[:-1]`) on both the 4H and 1H timeframes — no lookahead.
- Branch: `feature/fib-mtf-strategy`, cut from `main`.

---

## Task 1: Branch setup and `config_fib.py`

**Files:**
- Create: `config_fib.py`
- Test: `tests/test_config_fib.py`

**Interfaces:**
- Produces: module `config_fib` with attributes `STRATEGY_NAME, TREND_TF, ENTRY_TF, TREND_EMA_PERIOD, SWING_FRACTAL_BARS, SWING_LOOKBACK_BARS, ZONE_LOOKBACK_BARS, ZONE_LOWER, ZONE_UPPER, TP_EXTENSION_LEVEL, SL_ATR_BUFFER_MULTIPLIER, RSI_PERIOD, RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX, VOLUME_MA_PERIOD, MIN_VOLUME_MULTIPLIER, ATR_PERIOD, LEVERAGE, MAX_SL_ROI_PCT, MAX_SL_PRICE_PCT, MIN_RR, ENABLE_BTC_FILTER, BTC_FILTER_SYMBOL, BTC_FILTER_TF, BTC_MAX_OPPOSING_MOVE_PCT, BTC_MAX_SINGLE_CANDLE_MOVE_PCT, BTC_MAX_THREE_CANDLE_MOVE_PCT, TREND_KLINE_COUNT, ENTRY_KLINE_COUNT, SIGNAL_EXPIRE_HOURS, ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT`, all `int`/`float`/`str`/`bool` as named.

- [ ] **Step 1: Create the branch**

```bash
git checkout main
git pull
git checkout -b feature/fib-mtf-strategy
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_config_fib.py`:

```python
import config_fib


def test_defaults_loaded():
    assert config_fib.TREND_TF == "4h"
    assert config_fib.ENTRY_TF == "1h"
    assert config_fib.TREND_EMA_PERIOD == 200
    assert config_fib.SWING_FRACTAL_BARS == 5
    assert config_fib.SWING_LOOKBACK_BARS == 60
    assert config_fib.ZONE_LOOKBACK_BARS == 10
    assert config_fib.ZONE_LOWER == 0.5
    assert config_fib.ZONE_UPPER == 0.618
    assert config_fib.TP_EXTENSION_LEVEL == 1.272
    assert config_fib.LEVERAGE == 20
    assert config_fib.MIN_RR == 1.5
    assert config_fib.ENABLE_BTC_FILTER is True
    assert config_fib.BTC_FILTER_TF == "4h"


def test_derived_max_sl_price_pct():
    expected = config_fib.MAX_SL_ROI_PCT / 100.0 / config_fib.LEVERAGE
    assert abs(config_fib.MAX_SL_PRICE_PCT - expected) < 1e-12
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_config_fib.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'config_fib'`

- [ ] **Step 4: Write `config_fib.py`**

```python
import os
from dotenv import load_dotenv

load_dotenv()

# ── Strategy: Fibonacci MTF (backtest research only) ─────────────────
STRATEGY_NAME: str = os.getenv("FIB_STRATEGY_NAME", "Fibonacci MTF Pullback (research)")

TREND_TF: str = os.getenv("FIB_TREND_TF", "4h")
ENTRY_TF: str = os.getenv("FIB_ENTRY_TF", "1h")

TREND_KLINE_COUNT: int = int(os.getenv("FIB_TREND_KLINE_COUNT", "260"))
ENTRY_KLINE_COUNT: int = int(os.getenv("FIB_ENTRY_KLINE_COUNT", "150"))

TREND_EMA_PERIOD: int = int(os.getenv("FIB_TREND_EMA_PERIOD", "200"))

SWING_FRACTAL_BARS: int = int(os.getenv("FIB_SWING_FRACTAL_BARS", "5"))
SWING_LOOKBACK_BARS: int = int(os.getenv("FIB_SWING_LOOKBACK_BARS", "60"))
ZONE_LOOKBACK_BARS: int = int(os.getenv("FIB_ZONE_LOOKBACK_BARS", "10"))
ZONE_LOWER: float = float(os.getenv("FIB_ZONE_LOWER", "0.5"))
ZONE_UPPER: float = float(os.getenv("FIB_ZONE_UPPER", "0.618"))
TP_EXTENSION_LEVEL: float = float(os.getenv("FIB_TP_EXTENSION_LEVEL", "1.272"))
SL_ATR_BUFFER_MULTIPLIER: float = float(os.getenv("FIB_SL_ATR_BUFFER_MULTIPLIER", "0.15"))

RSI_PERIOD: int = int(os.getenv("FIB_RSI_PERIOD", "14"))
RSI_LONG_MIN: float = float(os.getenv("FIB_RSI_LONG_MIN", "50"))
RSI_LONG_MAX: float = float(os.getenv("FIB_RSI_LONG_MAX", "68"))
RSI_SHORT_MIN: float = float(os.getenv("FIB_RSI_SHORT_MIN", "32"))
RSI_SHORT_MAX: float = float(os.getenv("FIB_RSI_SHORT_MAX", "50"))

VOLUME_MA_PERIOD: int = int(os.getenv("FIB_VOLUME_MA_PERIOD", "20"))
MIN_VOLUME_MULTIPLIER: float = float(os.getenv("FIB_MIN_VOLUME_MULTIPLIER", "1.3"))

ATR_PERIOD: int = int(os.getenv("FIB_ATR_PERIOD", "14"))

LEVERAGE: int = int(os.getenv("FIB_LEVERAGE", "20"))
MAX_SL_ROI_PCT: float = float(os.getenv("FIB_MAX_SL_ROI_PCT", "10.0"))
MAX_SL_PRICE_PCT: float = MAX_SL_ROI_PCT / 100.0 / LEVERAGE
MIN_RR: float = float(os.getenv("FIB_MIN_RR", "1.5"))

# ── BTC market safety filter (4H variant) ─────────────────────────────
ENABLE_BTC_FILTER: bool = os.getenv("FIB_ENABLE_BTC_FILTER", "true").lower() == "true"
BTC_FILTER_SYMBOL: str = os.getenv("FIB_BTC_FILTER_SYMBOL", "BTC_USDT")
BTC_FILTER_TF: str = os.getenv("FIB_BTC_FILTER_TF", "4h")
BTC_MAX_OPPOSING_MOVE_PCT: float = float(os.getenv("FIB_BTC_MAX_OPPOSING_MOVE_PCT", "0.20"))
BTC_MAX_SINGLE_CANDLE_MOVE_PCT: float = float(os.getenv("FIB_BTC_MAX_SINGLE_CANDLE_MOVE_PCT", "0.60"))
BTC_MAX_THREE_CANDLE_MOVE_PCT: float = float(os.getenv("FIB_BTC_MAX_THREE_CANDLE_MOVE_PCT", "1.20"))

# ── Backtest-only: outcome expiry + fee/slippage estimates ────────────
# 1H-based swings resolve slower than the live bot's 5m scalp strategy,
# so the expiry window is generously longer (48x 1H bars = 48h).
SIGNAL_EXPIRE_HOURS: float = float(os.getenv("FIB_SIGNAL_EXPIRE_HOURS", "48"))

ESTIMATED_ENTRY_FEE_PCT: float = float(os.getenv("FIB_ESTIMATED_ENTRY_FEE_PCT", "0.02"))
ESTIMATED_EXIT_FEE_PCT: float = float(os.getenv("FIB_ESTIMATED_EXIT_FEE_PCT", "0.02"))
ESTIMATED_SLIPPAGE_PCT: float = float(os.getenv("FIB_ESTIMATED_SLIPPAGE_PCT", "0.01"))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_config_fib.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add config_fib.py tests/test_config_fib.py
git commit -m "feat: add standalone config module for Fibonacci MTF strategy"
```

---

## Task 2: Swing detection and Fibonacci price math

**Files:**
- Create: `fib_strategy.py` (module docstring + imports + dataclasses + math functions only in this task)
- Create: `tests/fib_fixtures.py`
- Test: `tests/test_fib_strategy.py`

**Interfaces:**
- Consumes: nothing from other tasks yet.
- Produces:
  - `FibLeg` dataclass: `leg_start: float, leg_end: float, leg_start_idx: int, leg_end_idx: int`
  - `find_swings(df: pd.DataFrame, fractal_bars: int) -> pd.DataFrame` — returns `df` copy with added bool columns `swing_high`, `swing_low`.
  - `find_last_impulse_leg(df_with_swings: pd.DataFrame, direction: str, lookback_bars: int) -> FibLeg | None`
  - `fib_retracement_price(leg_start: float, leg_end: float, direction: str, level: float) -> float`
  - `fib_extension_price(leg_start: float, leg_end: float, direction: str, ratio: float) -> float`

- [ ] **Step 1: Write the failing tests**

Create `tests/fib_fixtures.py`:

```python
"""
Deterministic OHLCV fixture builders for Fibonacci MTF strategy tests.

Numeric constants here are reasoned, not hand-executed against pandas --
if a test using these fails for the wrong reason (RSI/volume landing
outside the expected band), adjust the constants below and re-run. That is
expected TDD iteration, not a defect in the test itself (same convention
as tests/strategy_fixtures.py).
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


def make_1h_fib_df(
    direction: str = "LONG",
    bars: int = 140,
    leg_start: float = 90.0,
    leg_end: float = 110.0,
    confirm_break: float = 0.6,
    confirm_volume_mult: float = 1.6,
) -> pd.DataFrame:
    """
    A 1H series ending in: quiet padding, a clean impulse leg (swing
    low/high at leg_start/leg_end over 20 bars), a 10-bar retracement into
    the middle of the 0.5-0.618 Fibonacci zone, a 9-bar hold near the zone,
    then a confirmation candle that closes back out of the zone in the
    trend direction with a volume spike. Ends with one duplicated row so
    callers can `iloc[:-1]` to drop the "forming" candle.

    Story layout (40 bars, LONG shown -- SHORT mirrors leg_start/leg_end):
      story[0:10]   decline from padding level down to leg_start (swing low)
      story[10:20]  rise from leg_start up to leg_end (swing high)
      story[20:30]  retracement from leg_end down to zone midpoint
      story[30:39]  hold near the zone midpoint (small zigzag)
      story[39]     confirmation candle, closes back out of the zone
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="1h")

    span = abs(leg_end - leg_start)
    zone_mid = leg_end - sign * 0.559 * span   # midpoint of the 0.5-0.618 zone
    zone_edge = leg_end - sign * 0.5 * span    # the 0.5 level (nearer to leg_end)

    # pad + decline together form ONE continuous monotonic run down to
    # leg_start -- built as a single linspace then sliced, so there is no
    # value discontinuity (and therefore no spurious extra swing high) at
    # the pad/decline boundary.
    n_pad = bars - 40
    combined_decline = np.linspace(leg_start + sign * 3.0, leg_start, n_pad + 10)
    pad = combined_decline[:n_pad]
    decline = combined_decline[n_pad:]
    rise = np.linspace(leg_start, leg_end, 11)[1:]
    retrace = np.linspace(leg_end, zone_mid, 11)[1:]

    zigzag = np.array([0.15, -0.15] * 5)[:9] * sign
    hold = zone_mid + zigzag

    confirm_close = zone_edge + sign * confirm_break
    closes = np.concatenate([pad, decline, rise, retrace, hold, [confirm_close]])

    opens = np.empty(bars)
    opens[0] = closes[0] - sign * 0.05
    opens[1:] = closes[:-1]

    volumes = np.full(bars, 1000.0)
    volumes[-1] = 1000.0 * confirm_volume_mult

    highs = np.maximum(opens, closes) + 0.1
    lows = np.minimum(opens, closes) - 0.1

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def patch_fib_klines(monkeypatch, fib_strategy_module, df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> None:
    """Route fib_strategy.get_market_klines(symbol, interval, count) to fixtures by interval."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        if interval == "4h":
            return df_4h
        if interval == "1h":
            return df_1h
        raise ValueError(f"unexpected interval {interval!r} in test")

    monkeypatch.setattr(fib_strategy_module, "get_market_klines", _fake)
```

Create `tests/test_fib_strategy.py`:

```python
import numpy as np
import pandas as pd

import fib_strategy
from fib_strategy import (
    FibLeg, find_swings, find_last_impulse_leg,
    fib_retracement_price, fib_extension_price,
)
from tests.fib_fixtures import make_1h_fib_df


def test_fractal_swing_detection_finds_known_pivots():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    # The rise segment peaks at story index 19 (leg_end); the decline
    # segment bottoms at story index 9 (leg_start). Story starts at
    # len(closed) - 40.
    story_start = len(closed) - 40
    leg_start_pos = story_start + 9
    leg_end_pos = story_start + 19

    assert bool(swings["swing_low"].iloc[leg_start_pos]) is True
    assert bool(swings["swing_high"].iloc[leg_end_pos]) is True


def test_fib_levels_computed_correctly_for_known_leg():
    # LONG leg: 90 -> 110, span 20.
    price_0 = fib_retracement_price(90.0, 110.0, "LONG", 0.0)
    price_50 = fib_retracement_price(90.0, 110.0, "LONG", 0.5)
    price_618 = fib_retracement_price(90.0, 110.0, "LONG", 0.618)
    price_100 = fib_retracement_price(90.0, 110.0, "LONG", 1.0)

    assert price_0 == 110.0
    assert price_50 == 100.0
    assert abs(price_618 - 97.64) < 1e-9
    assert price_100 == 90.0

    ext_1272 = fib_extension_price(90.0, 110.0, "LONG", 1.272)
    assert abs(ext_1272 - 115.44) < 1e-9


def test_fib_levels_computed_correctly_for_short_leg():
    # SHORT leg: 110 -> 90, span 20 (leg_start=110 high, leg_end=90 low).
    price_0 = fib_retracement_price(110.0, 90.0, "SHORT", 0.0)
    price_50 = fib_retracement_price(110.0, 90.0, "SHORT", 0.5)
    price_100 = fib_retracement_price(110.0, 90.0, "SHORT", 1.0)

    assert price_0 == 90.0
    assert price_50 == 100.0
    assert price_100 == 110.0

    ext_1272 = fib_extension_price(110.0, 90.0, "SHORT", 1.272)
    assert abs(ext_1272 - 84.56) < 1e-9


def test_find_last_impulse_leg_long():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    leg = find_last_impulse_leg(swings, "LONG", lookback_bars=60)

    assert leg is not None
    assert abs(leg.leg_start - 90.0) < 0.01
    assert abs(leg.leg_end - 110.0) < 0.01
    assert leg.leg_start_idx < leg.leg_end_idx


def test_find_last_impulse_leg_short():
    df = make_1h_fib_df("SHORT", leg_start=110.0, leg_end=90.0)
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    leg = find_last_impulse_leg(swings, "SHORT", lookback_bars=60)

    assert leg is not None
    assert abs(leg.leg_start - 110.0) < 0.01
    assert abs(leg.leg_end - 90.0) < 0.01


def test_find_last_impulse_leg_returns_none_without_swings():
    idx = pd.date_range("2026-01-01", periods=30, freq="1h")
    flat = pd.DataFrame(
        {"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1000.0},
        index=idx,
    )
    swings = find_swings(flat, fractal_bars=5)
    leg = find_last_impulse_leg(swings, "LONG", lookback_bars=60)
    assert leg is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fib_strategy'`

- [ ] **Step 3: Create `fib_strategy.py` with the math functions**

```python
"""
Fibonacci Multi-Timeframe Strategy (backtest research only).

4H trend (EMA200 + slope) gates direction; 1H fractal-swing Fibonacci
retracement into the 0.5-0.618 golden zone + rejection candle + RSI +
volume confirms entry. TP at a Fibonacci extension of the impulse leg, SL
just beyond the leg's origin swing point. Only completed candles are ever
used. Fully standalone from strategy.py -- see
docs/superpowers/specs/2026-07-16-fib-mtf-strategy-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class FibLeg:
    leg_start: float
    leg_end: float
    leg_start_idx: int
    leg_end_idx: int


@dataclass
class FibSignal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    rr: float
    score: float
    leg_start: float
    leg_end: float
    zone_lower: float
    zone_upper: float
    generated_at: datetime


@dataclass
class BtcContext4h:
    close: float
    ema_200: float
    one_candle_move_pct: float
    three_candle_move_pct: float


# ── swing + fibonacci math ────────────────────────────────────────────

def find_swings(df: pd.DataFrame, fractal_bars: int) -> pd.DataFrame:
    """Marks bars whose high/low is the strict max/min among itself and
    `fractal_bars // 2` bars on each side. A swing at index i only becomes
    knowable once the `half` bars after it have closed -- naturally
    lookahead-safe as long as this is only ever called on already-completed
    candles."""
    half = fractal_bars // 2
    n = len(df)
    swing_high = [False] * n
    swing_low = [False] * n
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    for i in range(half, n - half):
        window_high = highs[i - half: i + half + 1]
        window_low = lows[i - half: i + half + 1]
        if highs[i] == window_high.max():
            swing_high[i] = True
        if lows[i] == window_low.min():
            swing_low[i] = True

    result = df.copy()
    result["swing_high"] = swing_high
    result["swing_low"] = swing_low
    return result


def find_last_impulse_leg(df_with_swings: pd.DataFrame, direction: str, lookback_bars: int) -> FibLeg | None:
    """Anchors on the most recent swing extreme in the trend direction
    (swing high for LONG, swing low for SHORT), then pairs it with the most
    recent swing of the opposite kind before it. Anchoring on the most
    recent swing HIGH (not "most recent swing low") makes this robust to a
    retracement itself creating a new, more-recent minor swing low after
    the leg's true end -- that new low is simply ignored since we never
    search for swing lows after the anchor point."""
    window = df_with_swings.iloc[-lookback_bars:] if len(df_with_swings) > lookback_bars else df_with_swings
    swing_high_positions = np.flatnonzero(window["swing_high"].to_numpy())
    swing_low_positions = np.flatnonzero(window["swing_low"].to_numpy())

    if len(swing_high_positions) == 0 or len(swing_low_positions) == 0:
        return None

    if direction == "LONG":
        end_pos = swing_high_positions[-1]
        candidates = swing_low_positions[swing_low_positions < end_pos]
        if len(candidates) == 0:
            return None
        start_pos = candidates[-1]
        leg_start = float(window["low"].iloc[start_pos])
        leg_end = float(window["high"].iloc[end_pos])
    else:
        end_pos = swing_low_positions[-1]
        candidates = swing_high_positions[swing_high_positions < end_pos]
        if len(candidates) == 0:
            return None
        start_pos = candidates[-1]
        leg_start = float(window["high"].iloc[start_pos])
        leg_end = float(window["low"].iloc[end_pos])

    start_idx = df_with_swings.index.get_loc(window.index[start_pos])
    end_idx = df_with_swings.index.get_loc(window.index[end_pos])
    return FibLeg(leg_start=leg_start, leg_end=leg_end, leg_start_idx=int(start_idx), leg_end_idx=int(end_idx))


def fib_retracement_price(leg_start: float, leg_end: float, direction: str, level: float) -> float:
    """`level` in [0, 1]: 0 = leg_end (no retracement), 1 = leg_start (full
    retracement back to the leg's origin)."""
    if direction == "LONG":
        return leg_end - (leg_end - leg_start) * level
    return leg_end + (leg_start - leg_end) * level


def fib_extension_price(leg_start: float, leg_end: float, direction: str, ratio: float) -> float:
    """`ratio` > 1 projects the leg_start->leg_end move beyond leg_end in
    the trend-continuation direction, measured from leg_start (standard
    Fibonacci extension convention)."""
    span = leg_end - leg_start
    return leg_start + span * ratio
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: PASS (6 passed). If `test_fractal_swing_detection_finds_known_pivots` or
`test_find_last_impulse_leg_long`/`_short` fail because the fixture's pivot
isn't landing exactly where expected, adjust `make_1h_fib_df`'s segment
constants (e.g. widen the gap between `decline`'s start and `leg_start`, or
adjust `zigzag` amplitude) and re-run -- this is expected fixture tuning,
not a bug in `find_swings`/`find_last_impulse_leg`.

- [ ] **Step 5: Commit**

```bash
git add fib_strategy.py tests/fib_fixtures.py tests/test_fib_strategy.py
git commit -m "feat: add swing detection and Fibonacci price math for MTF strategy"
```

---

## Task 3: 4H trend filter and 1H entry confirmation

**Files:**
- Modify: `fib_strategy.py`
- Modify: `tests/test_fib_strategy.py`

**Interfaces:**
- Consumes: `FibLeg`, `find_swings`, `find_last_impulse_leg`, `fib_retracement_price` (Task 2); `config_fib` module (Task 1); `calculate_ema`, `calculate_rsi`, `calculate_atr` imported from `strategy` (pure indicator helpers).
- Produces:
  - `_ema_slope_ok(ema: pd.Series, direction: str, tolerance: float = 1e-9) -> bool`
  - `_detect_trend_4h(df_4h: pd.DataFrame) -> str | None`
  - `_detect_fib_entry(df_1h: pd.DataFrame, direction: str) -> tuple[bool, str, dict]` — `dict` keys on success: `close, leg_start, leg_end, zone_low, zone_high, rsi, atr, volume_ratio`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fib_strategy.py`:

```python
from fib_strategy import _detect_trend_4h, _detect_fib_entry
from tests.fib_fixtures import make_4h_trend_df


def test_detect_trend_4h_long():
    df = make_4h_trend_df("LONG")
    closed = df.iloc[:-1]
    assert _detect_trend_4h(closed) == "LONG"


def test_detect_trend_4h_short():
    df = make_4h_trend_df("SHORT")
    closed = df.iloc[:-1]
    assert _detect_trend_4h(closed) == "SHORT"


def test_detect_trend_4h_none_when_flat():
    idx = pd.date_range("2026-01-01", periods=220, freq="4h")
    flat = pd.DataFrame(
        {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1000.0},
        index=idx,
    )
    assert _detect_trend_4h(flat) is None


def test_detect_fib_entry_long_valid():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_fib_entry(closed, "LONG")

    assert ok is True, reason
    assert abs(details["leg_start"] - 90.0) < 0.01
    assert abs(details["leg_end"] - 110.0) < 0.01
    assert details["zone_low"] < details["zone_high"]


def test_detect_fib_entry_short_valid():
    df = make_1h_fib_df("SHORT", leg_start=110.0, leg_end=90.0)
    closed = df.iloc[:-1]
    ok, reason, details = _detect_fib_entry(closed, "SHORT")

    assert ok is True, reason
    assert abs(details["leg_start"] - 110.0) < 0.01
    assert abs(details["leg_end"] - 90.0) < 0.01


def test_detect_fib_entry_rejected_without_swing_leg():
    idx = pd.date_range("2026-01-01", periods=140, freq="1h")
    flat = pd.DataFrame(
        {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1000.0},
        index=idx,
    )
    ok, reason, _ = _detect_fib_entry(flat, "LONG")
    assert ok is False
    assert reason == "no valid impulse leg found"


def test_detect_fib_entry_rejected_without_zone_touch():
    # confirm_break large enough that the retracement never actually
    # dips into the 0.5-0.618 zone before the confirmation candle --
    # simulate by using a leg with a huge span so the fixture's fixed
    # retracement offset lands short of the zone.
    df = make_1h_fib_df("LONG", leg_start=10.0, leg_end=1000.0)
    closed = df.iloc[:-1]
    ok, reason, _ = _detect_fib_entry(closed, "LONG")
    assert ok is False
    assert reason in ("no valid impulse leg found", "no zone touch")


def test_detect_fib_entry_rejected_when_rsi_out_of_band(monkeypatch):
    # The fixture's confirmation candle otherwise passes zone-touch and
    # direction checks -- force RSI into deep-oversold territory (well
    # outside the LONG band) to isolate the RSI gate specifically.
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]

    def _fake_rsi(series, period):
        return pd.Series(20.0, index=series.index)

    monkeypatch.setattr(fib_strategy, "calculate_rsi", _fake_rsi)

    ok, reason, _ = _detect_fib_entry(closed, "LONG")
    assert ok is False
    assert reason.startswith("RSI")


def test_detect_fib_entry_rejected_when_volume_too_low(monkeypatch):
    df = make_1h_fib_df("LONG")
    # df has bars+1 rows (last one duplicated as the "forming" candle);
    # index -2 is the real confirmation candle that survives `iloc[:-1]`.
    df.iloc[-2, df.columns.get_loc("volume")] = 1.0
    closed = df.iloc[:-1]

    ok, reason, _ = _detect_fib_entry(closed, "LONG")
    assert ok is False
    assert reason.startswith("volume ratio")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: FAIL with `ImportError: cannot import name '_detect_trend_4h'`

- [ ] **Step 3: Add trend + entry confirmation to `fib_strategy.py`**

Append to `fib_strategy.py` (after the math functions from Task 2):

```python
from strategy import calculate_ema, calculate_rsi, calculate_atr
import config_fib


def _ema_slope_ok(ema: pd.Series, direction: str, tolerance: float = 1e-9) -> bool:
    current = ema.iloc[-1]
    three_bars_ago = ema.iloc[-4]
    if direction == "LONG":
        return current >= three_bars_ago - tolerance
    return current <= three_bars_ago + tolerance


def _detect_trend_4h(df_4h: pd.DataFrame) -> str | None:
    ema200 = calculate_ema(df_4h["close"], config_fib.TREND_EMA_PERIOD)
    close = float(df_4h["close"].iloc[-1])

    if close > float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "LONG"):
        return "LONG"
    if close < float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "SHORT"):
        return "SHORT"
    return None


def _detect_fib_entry(df_1h: pd.DataFrame, direction: str) -> tuple[bool, str, dict]:
    rsi = calculate_rsi(df_1h["close"], config_fib.RSI_PERIOD)
    atr = calculate_atr(df_1h, config_fib.ATR_PERIOD)

    swings = find_swings(df_1h, config_fib.SWING_FRACTAL_BARS)
    leg = find_last_impulse_leg(swings, direction, config_fib.SWING_LOOKBACK_BARS)
    if leg is None:
        return False, "no valid impulse leg found", {}

    price_a = fib_retracement_price(leg.leg_start, leg.leg_end, direction, config_fib.ZONE_LOWER)
    price_b = fib_retracement_price(leg.leg_start, leg.leg_end, direction, config_fib.ZONE_UPPER)
    zone_low, zone_high = min(price_a, price_b), max(price_a, price_b)

    recent = df_1h.iloc[-(config_fib.ZONE_LOOKBACK_BARS + 1):-1]
    touched = bool(((recent["low"] <= zone_high) & (recent["high"] >= zone_low)).any())
    if not touched:
        return False, "no zone touch", {}

    close = float(df_1h["close"].iloc[-1])
    open_ = float(df_1h["open"].iloc[-1])
    rsi_last = float(rsi.iloc[-1])
    atr_last = float(atr.iloc[-1])
    vol_last = float(df_1h["volume"].iloc[-1])
    vol_avg = float(df_1h["volume"].iloc[-(config_fib.VOLUME_MA_PERIOD + 1):-1].mean())

    if direction == "LONG":
        if not (close > zone_high and close > open_):
            return False, "confirmation candle did not close back above zone", {}
    else:
        if not (close < zone_low and close < open_):
            return False, "confirmation candle did not close back below zone", {}

    rsi_min, rsi_max = (
        (config_fib.RSI_LONG_MIN, config_fib.RSI_LONG_MAX) if direction == "LONG"
        else (config_fib.RSI_SHORT_MIN, config_fib.RSI_SHORT_MAX)
    )
    if not (rsi_min <= rsi_last <= rsi_max):
        return False, f"RSI {rsi_last:.1f} outside {direction.lower()} range", {}

    if vol_avg <= 0 or not (vol_last >= config_fib.MIN_VOLUME_MULTIPLIER * vol_avg):
        ratio = (vol_last / vol_avg) if vol_avg else 0.0
        return False, f"volume ratio {ratio:.2f} below {config_fib.MIN_VOLUME_MULTIPLIER}", {}

    details = {
        "close": close,
        "leg_start": leg.leg_start,
        "leg_end": leg.leg_end,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "rsi": rsi_last,
        "atr": atr_last,
        "volume_ratio": vol_last / vol_avg if vol_avg else 0.0,
    }
    return True, "", details
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: PASS (15 passed). If RSI or volume checks reject the fixture's
confirmation candle unexpectedly, adjust `make_1h_fib_df`'s `confirm_break`,
`confirm_volume_mult`, or the `zigzag` amplitude in the hold segment and
re-run -- expected fixture tuning per the file's own docstring caveat.

- [ ] **Step 5: Commit**

```bash
git add fib_strategy.py tests/test_fib_strategy.py
git commit -m "feat: add 4H trend filter and 1H Fibonacci entry confirmation"
```

---

## Task 4: TP/SL, geometry, RR, and scoring

**Files:**
- Modify: `fib_strategy.py`
- Modify: `tests/test_fib_strategy.py`

**Interfaces:**
- Consumes: `config_fib` (Task 1); `details` dict shape from `_detect_fib_entry` (Task 3).
- Produces:
  - `_valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool`
  - `_calc_rr(entry: float, tp: float, sl: float) -> float`
  - `_calculate_tp_sl_fib(direction: str, entry: float, details: dict) -> tuple[float, float] | None`
  - `_score_candidate_fib(direction: str, details: dict, rr: float) -> float`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fib_strategy.py`:

```python
from fib_strategy import (
    _valid_trade_geometry, _calc_rr, _calculate_tp_sl_fib, _score_candidate_fib,
)


def test_long_trade_geometry():
    assert _valid_trade_geometry("LONG", entry=100.0, tp=105.0, sl=98.0) is True
    assert _valid_trade_geometry("LONG", entry=100.0, tp=95.0, sl=98.0) is False


def test_short_trade_geometry():
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=95.0, sl=102.0) is True
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=105.0, sl=102.0) is False


def test_invalid_geometry_rejected_on_zero_price():
    assert _valid_trade_geometry("LONG", entry=0.0, tp=105.0, sl=98.0) is False


def test_calc_rr():
    rr = _calc_rr(entry=100.0, tp=106.0, sl=98.0)
    assert abs(rr - 3.0) < 1e-9


def test_calculate_tp_sl_fib_long_valid():
    details = {"leg_start": 90.0, "leg_end": 110.0, "atr": 0.5}
    result = _calculate_tp_sl_fib("LONG", entry=100.5, details=details)

    assert result is not None
    tp, sl = result
    assert abs(tp - 115.44) < 1e-6           # 90 + 20*1.272
    assert sl < 90.0                          # leg_start minus ATR buffer
    assert tp > 100.5 > sl


def test_calculate_tp_sl_fib_short_valid():
    details = {"leg_start": 110.0, "leg_end": 90.0, "atr": 0.5}
    result = _calculate_tp_sl_fib("SHORT", entry=99.4, details=details)

    assert result is not None
    tp, sl = result
    assert abs(tp - 84.56) < 1e-6             # 110 - 20*1.272
    assert sl > 110.0
    assert tp < 99.4 < sl


def test_calculate_tp_sl_fib_rejects_stop_too_wide():
    # ATR buffer alone would blow past MAX_SL_PRICE_PCT for a tiny entry
    # price -- confirms the width guard works, not just the geometry sign.
    details = {"leg_start": 50.0, "leg_end": 110.0, "atr": 500.0}
    result = _calculate_tp_sl_fib("LONG", entry=100.5, details=details)
    assert result is None


def test_score_candidate_fib_within_bounds():
    details = {
        "close": 100.5, "zone_low": 97.64, "zone_high": 100.0,
        "rsi": 58.0, "volume_ratio": 1.8,
    }
    score = _score_candidate_fib("LONG", details, rr=2.5)
    assert 0.0 <= score <= 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: FAIL with `ImportError: cannot import name '_valid_trade_geometry'`

- [ ] **Step 3: Add TP/SL, geometry, RR, and scoring to `fib_strategy.py`**

Append to `fib_strategy.py`:

```python
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


def _calculate_tp_sl_fib(direction: str, entry: float, details: dict) -> tuple[float, float] | None:
    leg_start = details["leg_start"]
    leg_end = details["leg_end"]
    atr_last = details["atr"]

    tp = fib_extension_price(leg_start, leg_end, direction, config_fib.TP_EXTENSION_LEVEL)

    if direction == "LONG":
        sl = leg_start - atr_last * config_fib.SL_ATR_BUFFER_MULTIPLIER
        if sl >= entry:
            return None
        if (entry - sl) / entry > config_fib.MAX_SL_PRICE_PCT:
            return None
    else:
        sl = leg_start + atr_last * config_fib.SL_ATR_BUFFER_MULTIPLIER
        if sl <= entry:
            return None
        if (sl - entry) / entry > config_fib.MAX_SL_PRICE_PCT:
            return None

    return tp, sl


def _score_candidate_fib(direction: str, details: dict, rr: float) -> float:
    score = 30.0  # 4H trend alignment -- already gated true/false upstream

    zone_mid = (details["zone_low"] + details["zone_high"]) / 2.0
    zone_span = details["zone_high"] - details["zone_low"]
    precision = 1.0 - min(1.0, abs(details["close"] - zone_mid) / zone_span) if zone_span > 0 else 0.5
    score += 20.0 * precision

    vol_ratio = details["volume_ratio"]
    vol_quality = min(1.0, max(0.0, (vol_ratio - config_fib.MIN_VOLUME_MULTIPLIER) / (2.0 - config_fib.MIN_VOLUME_MULTIPLIER)))
    score += 15.0 * vol_quality

    rsi = details["rsi"]
    ideal_lo, ideal_hi = (55.0, 62.0) if direction == "LONG" else (38.0, 45.0)
    if ideal_lo <= rsi <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi - ideal_lo), abs(rsi - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 15.0 * rsi_quality

    rr_quality = min(1.0, max(0.0, (rr - config_fib.MIN_RR) / (2.0 - config_fib.MIN_RR))) if config_fib.MIN_RR < 2.0 else (1.0 if rr >= config_fib.MIN_RR else 0.0)
    score += 20.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: PASS (23 passed)

- [ ] **Step 5: Commit**

```bash
git add fib_strategy.py tests/test_fib_strategy.py
git commit -m "feat: add TP/SL, geometry, RR, and scoring for Fibonacci MTF strategy"
```

---

## Task 5: BTC 4H safety filter

**Files:**
- Modify: `fib_strategy.py`
- Modify: `tests/test_fib_strategy.py`

**Interfaces:**
- Consumes: `BtcContext4h` (Task 2 dataclass), `config_fib` (Task 1), `calculate_ema` (imported from `strategy`).
- Produces:
  - `get_market_klines` — module-level import (`from market_data import get_market_klines`), monkeypatchable by tests/backtest exactly like `strategy.get_market_klines`.
  - `build_btc_context_4h() -> BtcContext4h | None`
  - `_btc_filter_ok_4h(direction: str, btc: BtcContext4h) -> tuple[bool, str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fib_strategy.py` (note: `import fib_strategy` is already at the top
of the file from Task 2 — only the `from fib_strategy import ...` line below is new):

```python
from fib_strategy import BtcContext4h, _btc_filter_ok_4h, build_btc_context_4h


def _bullish_btc_4h() -> BtcContext4h:
    return BtcContext4h(close=50100.0, ema_200=49500.0, one_candle_move_pct=0.1, three_candle_move_pct=0.3)


def _bearish_btc_4h() -> BtcContext4h:
    return BtcContext4h(close=49500.0, ema_200=50100.0, one_candle_move_pct=-0.1, three_candle_move_pct=-0.3)


def _extreme_btc_4h() -> BtcContext4h:
    return BtcContext4h(close=50100.0, ema_200=49500.0, one_candle_move_pct=0.9, three_candle_move_pct=0.3)


def test_btc_filter_long_allowed_when_btc_bullish():
    ok, _ = _btc_filter_ok_4h("LONG", _bullish_btc_4h())
    assert ok is True


def test_btc_filter_long_blocked_when_btc_bearish():
    ok, reason = _btc_filter_ok_4h("LONG", _bearish_btc_4h())
    assert ok is False
    assert "bearish" in reason


def test_btc_filter_short_allowed_when_btc_bearish():
    ok, _ = _btc_filter_ok_4h("SHORT", _bearish_btc_4h())
    assert ok is True


def test_btc_filter_short_blocked_when_btc_bullish():
    ok, reason = _btc_filter_ok_4h("SHORT", _bullish_btc_4h())
    assert ok is False
    assert "bullish" in reason


def test_btc_filter_blocked_during_extreme_move():
    ok, reason = _btc_filter_ok_4h("LONG", _extreme_btc_4h())
    assert ok is False
    assert "volatility" in reason


def test_build_btc_context_4h_from_klines(monkeypatch):
    df_btc = make_4h_trend_df("LONG", bars=220)

    def _fake(symbol, interval, count=100):
        assert symbol == fib_strategy.config_fib.BTC_FILTER_SYMBOL
        assert interval == fib_strategy.config_fib.BTC_FILTER_TF
        return df_btc

    monkeypatch.setattr(fib_strategy, "get_market_klines", _fake)

    ctx = build_btc_context_4h()
    assert ctx is not None
    assert ctx.close > ctx.ema_200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: FAIL with `ImportError: cannot import name 'BtcContext4h'` (it exists as a
dataclass already from Task 2 -- this specific failure is for
`build_btc_context_4h`/`_btc_filter_ok_4h`, which don't exist yet)

- [ ] **Step 3: Add the BTC 4H filter to `fib_strategy.py`**

Add near the top of `fib_strategy.py`, after the `import config_fib` line added in
Task 3:

```python
from market_data import get_market_klines
```

Append to the end of `fib_strategy.py`:

```python
def build_btc_context_4h() -> BtcContext4h | None:
    df = get_market_klines(config_fib.BTC_FILTER_SYMBOL, config_fib.BTC_FILTER_TF, count=config_fib.TREND_KLINE_COUNT)
    if df is None or df.empty:
        return None
    closed = df.iloc[:-1].copy()
    if len(closed) < config_fib.TREND_EMA_PERIOD + 5:
        return None

    ema200 = calculate_ema(closed["close"], config_fib.TREND_EMA_PERIOD)

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
    if abs(btc.one_candle_move_pct) > config_fib.BTC_MAX_SINGLE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"
    if abs(btc.three_candle_move_pct) > config_fib.BTC_MAX_THREE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"

    if direction == "LONG":
        if not (
            btc.close > btc.ema_200
            and btc.three_candle_move_pct >= -config_fib.BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bearish trend"
    else:
        if not (
            btc.close < btc.ema_200
            and btc.three_candle_move_pct <= config_fib.BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bullish trend"

    return True, ""
```

Also add `from tests.fib_fixtures import make_4h_trend_df` to the top of
`tests/test_fib_strategy.py` if not already imported there from Task 3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: PASS (29 passed)

- [ ] **Step 5: Commit**

```bash
git add fib_strategy.py tests/test_fib_strategy.py
git commit -m "feat: add 4H BTC market-safety filter for Fibonacci MTF strategy"
```

---

## Task 6: `evaluate_symbol_fib` pipeline

**Files:**
- Modify: `fib_strategy.py`
- Modify: `tests/test_fib_strategy.py`

**Interfaces:**
- Consumes: everything from Tasks 2-5.
- Produces: `evaluate_symbol_fib(symbol: str, btc_context: BtcContext4h | None = None, reject_sink: dict | None = None) -> FibSignal | None` — the module's public entry point, used by the backtest harness in Task 7.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fib_strategy.py`:

```python
from fib_strategy import evaluate_symbol_fib
from tests.fib_fixtures import patch_fib_klines


def test_evaluate_symbol_fib_long_valid(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_fib_df("LONG")
    patch_fib_klines(monkeypatch, fib_strategy, df_4h, df_1h)

    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bullish_btc_4h())

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.sl_price < sig.entry_price < sig.tp_price
    assert sig.rr >= fib_strategy.config_fib.MIN_RR


def test_evaluate_symbol_fib_short_valid(monkeypatch):
    df_4h = make_4h_trend_df("SHORT")
    df_1h = make_1h_fib_df("SHORT", leg_start=110.0, leg_end=90.0)
    patch_fib_klines(monkeypatch, fib_strategy, df_4h, df_1h)

    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bearish_btc_4h())

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price
    assert sig.rr >= fib_strategy.config_fib.MIN_RR


def test_evaluate_symbol_fib_rejected_without_4h_trend(monkeypatch):
    idx = pd.date_range("2026-01-01", periods=220, freq="4h")
    flat_4h = pd.DataFrame(
        {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1000.0},
        index=idx,
    )
    df_1h = make_1h_fib_df("LONG")
    patch_fib_klines(monkeypatch, fib_strategy, flat_4h, df_1h)

    reject_sink: dict = {}
    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bullish_btc_4h(), reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("no_trend") == 1


def test_evaluate_symbol_fib_rejected_when_btc_opposes(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_fib_df("LONG")
    patch_fib_klines(monkeypatch, fib_strategy, df_4h, df_1h)

    reject_sink: dict = {}
    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bearish_btc_4h(), reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("btc_filter") == 1


def test_evaluate_symbol_fib_1h_active_last_candle_is_ignored(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_fib_df("LONG")
    # Corrupt only the forming (duplicated last) 1H row -- evaluate_symbol_fib
    # must still fire off the completed candles underneath it.
    df_1h.iloc[-1, df_1h.columns.get_loc("close")] = 1.0
    patch_fib_klines(monkeypatch, fib_strategy, df_4h, df_1h)

    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bullish_btc_4h())
    assert sig is not None


def test_evaluate_symbol_fib_4h_active_last_candle_is_ignored(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_fib_df("LONG")
    # Corrupt only the forming (duplicated last) 4H row -- evaluate_symbol_fib
    # must still read trend off the completed 4H candles underneath it.
    df_4h.iloc[-1, df_4h.columns.get_loc("close")] = 1.0
    patch_fib_klines(monkeypatch, fib_strategy, df_4h, df_1h)

    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bullish_btc_4h())
    assert sig is not None


def test_evaluate_symbol_fib_rejected_when_rr_too_low(monkeypatch):
    df_4h = make_4h_trend_df("LONG")
    df_1h = make_1h_fib_df("LONG")
    patch_fib_klines(monkeypatch, fib_strategy, df_4h, df_1h)
    monkeypatch.setattr(fib_strategy.config_fib, "MIN_RR", 100.0)

    reject_sink: dict = {}
    sig = evaluate_symbol_fib("TEST_USDT", btc_context=_bullish_btc_4h(), reject_sink=reject_sink)

    assert sig is None
    assert reject_sink.get("rr_below_min") == 1


def test_evaluate_symbol_fib_handles_missing_data(monkeypatch):
    def _fake(symbol, interval, count=100):
        return pd.DataFrame()

    monkeypatch.setattr(fib_strategy, "get_market_klines", _fake)

    reject_sink: dict = {}
    sig = evaluate_symbol_fib("TEST_USDT", reject_sink=reject_sink)
    assert sig is None
    assert reject_sink.get("missing_data") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: FAIL with `ImportError: cannot import name 'evaluate_symbol_fib'`

- [ ] **Step 3: Add `evaluate_symbol_fib` to `fib_strategy.py`**

Append to `fib_strategy.py`:

```python
def _reason_bucket(reason: str) -> str:
    if reason == "no valid impulse leg found":
        return "no_swing_leg"
    if reason == "no zone touch":
        return "no_zone_touch"
    if "did not close back" in reason:
        return "no_confirmation"
    if reason.startswith("RSI"):
        return "rsi_out_of_band"
    if reason.startswith("volume ratio"):
        return "low_volume"
    return "fib_other"


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def evaluate_symbol_fib(
    symbol: str,
    btc_context: "BtcContext4h | None" = None,
    reject_sink: dict | None = None,
) -> FibSignal | None:
    try:
        raw_4h = get_market_klines(symbol, config_fib.TREND_TF, count=config_fib.TREND_KLINE_COUNT)
        raw_1h = get_market_klines(symbol, config_fib.ENTRY_TF, count=config_fib.ENTRY_KLINE_COUNT)

        if raw_4h is None or raw_4h.empty or raw_1h is None or raw_1h.empty:
            logger.debug("[FIB-REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed_4h = raw_4h.iloc[:-1].copy()
        closed_1h = raw_1h.iloc[:-1].copy()

        if len(closed_4h) < config_fib.TREND_EMA_PERIOD + 5:
            _bump(reject_sink, "insufficient_history")
            return None
        min_1h_needed = config_fib.SWING_LOOKBACK_BARS + config_fib.SWING_FRACTAL_BARS + config_fib.VOLUME_MA_PERIOD + 10
        if len(closed_1h) < min_1h_needed:
            _bump(reject_sink, "insufficient_history")
            return None

        direction = _detect_trend_4h(closed_4h)
        if direction is None:
            logger.debug("[FIB-REJECT] %s no 4H trend", symbol)
            _bump(reject_sink, "no_trend")
            return None

        ok, reason, details = _detect_fib_entry(closed_1h, direction)
        if not ok:
            logger.debug("[FIB-REJECT] %s %s", symbol, reason)
            _bump(reject_sink, _reason_bucket(reason))
            return None

        if config_fib.ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context_4h()
            if ctx is None:
                _bump(reject_sink, "btc_context_unavailable")
                return None
            btc_ok, btc_reason = _btc_filter_ok_4h(direction, ctx)
            if not btc_ok:
                logger.debug("[FIB-REJECT] %s %s %s", symbol, direction, btc_reason)
                _bump(reject_sink, "btc_filter")
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl_fib(direction, entry, details)
        if tp_sl is None:
            _bump(reject_sink, "stop_too_wide")
            return None
        tp, sl = tp_sl

        if not _valid_trade_geometry(direction, entry, tp, sl):
            _bump(reject_sink, "invalid_geometry")
            return None

        rr = _calc_rr(entry, tp, sl)
        if rr < config_fib.MIN_RR:
            _bump(reject_sink, "rr_below_min")
            return None

        score = _score_candidate_fib(direction, details, rr)

        logger.info(
            "[FIB-CANDIDATE] %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
            symbol, direction, score, entry, tp, sl, rr,
        )

        return FibSignal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            leverage=config_fib.LEVERAGE,
            rr=round(rr, 2),
            score=score,
            leg_start=details["leg_start"],
            leg_end=details["leg_end"],
            zone_lower=details["zone_low"],
            zone_upper=details["zone_high"],
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        logger.error("[FIB-EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fib_strategy.py -v`
Expected: PASS (37 passed)

- [ ] **Step 5: Run the full test suite to confirm no regressions**

Run: `python -m pytest -v`
Expected: All tests pass, including the pre-existing `tests/test_strategy_supertrend_pullback.py`,
`tests/test_btc_filter.py`, etc. — untouched by this work.

- [ ] **Step 6: Verify no live-bot files were touched**

Run: `git diff main -- main.py strategy.py config.py bot.py webui.py database.py`
Expected: empty output.

- [ ] **Step 7: Commit**

```bash
git add fib_strategy.py tests/test_fib_strategy.py
git commit -m "feat: wire full evaluate_symbol_fib pipeline for Fibonacci MTF strategy"
```

---

## Task 7: Backtest harness

**Files:**
- Create: `scripts/backtest_fib_strategy.py`
- Test: manual smoke test (2-3 symbols, short window) — no new automated test file; this
  script's correctness is validated by running it, same as `scripts/backtest_simple_strategy.py`
  had no dedicated pytest file.

**Interfaces:**
- Consumes: `get_klines_extended` imported from `scripts.backtest_simple_strategy` (no duplication);
  `fib_strategy.evaluate_symbol_fib`, `fib_strategy.get_market_klines` (monkeypatched per-call, same
  pattern as `backtest_simple_strategy.py`'s `strategy.get_market_klines` monkeypatch);
  `config_fib`.
- Produces: a runnable CLI script `python scripts/backtest_fib_strategy.py --symbols ... --days N --workers N`.

- [ ] **Step 1: Create `scripts/backtest_fib_strategy.py`**

```python
"""
Backtest utility for the Fibonacci Multi-Timeframe strategy.

Walks 1H candles forward in time, at each completed bar building an
"as-of" view (all 4H/1H/BTC candles up to and including that bar, plus a
duplicated last row standing in for the not-yet-formed candle) and calling
fib_strategy.evaluate_symbol_fib against it -- the exact same function a
live runtime would use, so backtest and any future live wiring share one
source of truth.

Reuses get_klines_extended's start/end pagination directly from
scripts/backtest_simple_strategy.py rather than duplicating it.
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

import fib_strategy
import config_fib
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
    max_bars = int(config_fib.SIGNAL_EXPIRE_HOURS)  # 1 bar == 1 hour on the 1H entry TF
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

    gross_roi = price_move_pct * config_fib.LEVERAGE
    cost_pct = (
        config_fib.ESTIMATED_ENTRY_FEE_PCT + config_fib.ESTIMATED_EXIT_FEE_PCT + config_fib.ESTIMATED_SLIPPAGE_PCT
    ) * config_fib.LEVERAGE
    net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi
    return round(gross_roi, 3), round(net_roi, 3)


def backtest_symbol(symbol: str, days: int, df_btc_full: pd.DataFrame) -> list[Trade]:
    """Runs in its own worker process -- returns this symbol's trades rather
    than mutating shared state, since process pool workers don't share
    memory."""
    trades: list[Trade] = []

    df_4h_full = get_klines_extended(symbol, config_fib.TREND_TF, days)
    df_1h_full = get_klines_extended(symbol, config_fib.ENTRY_TF, days)

    if df_4h_full.empty or df_1h_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_4h_full)} x {config_fib.TREND_TF} bars, "
        f"{len(df_1h_full)} x {config_fib.ENTRY_TF} bars",
        flush=True,
    )

    min_start = max(
        config_fib.TREND_EMA_PERIOD + 5,
        config_fib.SWING_LOOKBACK_BARS + config_fib.SWING_FRACTAL_BARS + config_fib.VOLUME_MA_PERIOD + 15,
    )
    in_trade_until_idx = -1

    original_get_market_klines = fib_strategy.get_market_klines
    candle_minutes = _TF_MINUTES[config_fib.ENTRY_TF]
    trend_tf_minutes = _TF_MINUTES[config_fib.TREND_TF]
    btc_tf_minutes = _TF_MINUTES[config_fib.BTC_FILTER_TF]

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
            if trend_idx is None or trend_idx < config_fib.TREND_EMA_PERIOD + 5:
                continue

            as_of_1h = _with_forming_row(df_1h_full, i, config_fib.ENTRY_KLINE_COUNT)
            as_of_4h = _with_forming_row(df_4h_full, trend_idx, config_fib.TREND_KLINE_COUNT)
            as_of_btc = _with_forming_row(df_btc_full, btc_idx, config_fib.TREND_KLINE_COUNT) if btc_idx is not None else None

            def _fake(sym: str, interval: str, count: int = 100, _1h=as_of_1h, _4h=as_of_4h, _btc=as_of_btc):
                if sym == config_fib.BTC_FILTER_SYMBOL and interval == config_fib.BTC_FILTER_TF:
                    return _btc if _btc is not None else pd.DataFrame()
                if interval == config_fib.ENTRY_TF:
                    return _1h
                if interval == config_fib.TREND_TF:
                    return _4h
                return pd.DataFrame()

            fib_strategy.get_market_klines = _fake

            sig = fib_strategy.evaluate_symbol_fib(symbol)

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
        fib_strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Fibonacci MTF Strategy")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=60, help="requested lookback in days (best-effort, paginated via start/end)")
    parser.add_argument("--workers", type=int, default=6, help="parallel worker processes, one symbol each")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- paginated via MEXC start/end)")

    print(f"[{config_fib.BTC_FILTER_SYMBOL}] fetching shared BTC context ({config_fib.BTC_FILTER_TF})...")
    df_btc_full = get_klines_extended(config_fib.BTC_FILTER_SYMBOL, config_fib.BTC_FILTER_TF, args.days)
    print(f"[{config_fib.BTC_FILTER_SYMBOL}] achieved history: {len(df_btc_full)} x {config_fib.BTC_FILTER_TF} bars")

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

- [ ] **Step 2: Verify it compiles**

Run: `python -m py_compile scripts/backtest_fib_strategy.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Smoke-test on 2-3 symbols over a short window**

```bash
python scripts/backtest_fib_strategy.py --symbols XRP_USDT DOGE_USDT ADA_USDT --days 14 --workers 3
```

Expected: script completes without crashing, prints achieved history per
symbol, and prints a report (possibly "No trades generated" on such a short
window and small symbol set -- that alone is not a failure, since 14 days
on 3 symbols may simply not produce a qualifying setup). If it crashes with
a traceback, debug via `systematic-debugging` before proceeding to the full
run in Task 8.

- [ ] **Step 4: Commit**

```bash
git add scripts/backtest_fib_strategy.py
git commit -m "feat: add backtest harness for Fibonacci MTF strategy"
```

---

## Task 8: Full 60-day backtest run

**Files:**
- Create: `scripts/fetch_backtest_symbol_pool.py` (small one-off helper, not a test target)
- No other files modified.

**Interfaces:**
- Consumes: `coin_scanner.refresh_coin_list() -> list[str]` (existing function, read-only reuse) to
  build the symbol universe the same way the live bot would.
- Produces: a text report from the full backtest run, reviewed with the user (not committed as a
  design artifact -- this is the research output itself).

- [ ] **Step 1: Generate the symbol universe**

Create `scripts/fetch_backtest_symbol_pool.py`:

```python
"""One-off helper: print the current live-bot-style coin pool as a
space-separated symbol list, for feeding into backtest scripts' --symbols
argument. Reuses coin_scanner.refresh_coin_list() read-only -- does not
write to any cache file the live bot depends on."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coin_scanner import refresh_coin_list

if __name__ == "__main__":
    symbols = refresh_coin_list()
    print(" ".join(symbols))
```

- [ ] **Step 2: Run it and capture the symbol list**

```bash
python scripts/fetch_backtest_symbol_pool.py > /tmp/fib_backtest_symbols.txt
cat /tmp/fib_backtest_symbols.txt
```

Expected: a space-separated line of ~40-80 `*_USDT` symbols (matches
`TOP_N_COINS`/`COIN_POOL_MIN_SELECTED` from `config.py`, since this reuses
the exact same live coin-pool builder).

- [ ] **Step 3: Run the full 60-day backtest**

```bash
python scripts/backtest_fib_strategy.py --symbols $(cat /tmp/fib_backtest_symbols.txt) --days 60 --workers 6 2>&1 | tee fib_backtest_60d.log
```

Expected: runs to completion (this may take several minutes -- the
`ProcessPoolExecutor` pattern from Task 7 keeps this from the O(n²) blowup
documented in `scripts/backtest_simple_strategy.py`'s module docstring).
Ends with a full `BacktestStats.print_report()` output plus the
average-trades/day line.

- [ ] **Step 4: Review results with the user**

This step has no code -- present `fib_backtest_60d.log`'s report (win rate,
net ROI, average ROI/trade, max drawdown, LONG/SHORT breakdown,
trades/day) to the user directly. Do not draw conclusions or propose
follow-up tuning unprompted; this is the deliverable the whole plan built
toward, per the spec's "Explicitly out of scope" section (no automatic
parameter optimization pass).

- [ ] **Step 5: Commit the helper script only (not the log)**

```bash
git add scripts/fetch_backtest_symbol_pool.py
git commit -m "feat: add coin-pool helper for Fibonacci MTF backtest symbol selection"
```

`fib_backtest_60d.log` is a research output, not a committed artifact --
leave it untracked (matches how `backtestfull.log` from the earlier
Supertrend Pullback backtest was also left untracked).

---

## Final verification

- [ ] Run the full test suite: `python -m pytest -v` — all pass.
- [ ] Compile check: `python -m py_compile fib_strategy.py config_fib.py scripts/backtest_fib_strategy.py scripts/fetch_backtest_symbol_pool.py` — no output.
- [ ] Confirm isolation: `git diff main -- main.py strategy.py config.py bot.py webui.py database.py` — empty.
- [ ] Confirm the branch is `feature/fib-mtf-strategy` and all commits from this plan are on it, not `main`.
