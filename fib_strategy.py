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
