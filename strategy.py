"""
Simple Supertrend Pullback v1.

15m trend (EMA200 + Supertrend) gates direction; 5m EMA20 pullback +
reclaim + RSI + volume + candle-quality confirms entry. Only completed
candles are ever used. See docs/superpowers/specs/2026-07-15-supertrend-pullback-v1-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


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


@dataclass
class BtcContext:
    close: float
    ema_200: float
    supertrend_direction: int
    one_candle_move_pct: float
    three_candle_move_pct: float


# ── indicators ──────────────────────────────────────────────────────

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, 1e-12)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def calculate_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=1, adjust=False).mean()


def calculate_supertrend(df: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame:
    high, low, close = df["high"], df["low"], df["close"]
    atr = calculate_atr(df, atr_period)
    hl2 = (high + low) / 2.0
    basic_upper = (hl2 + multiplier * atr).to_numpy()
    basic_lower = (hl2 - multiplier * atr).to_numpy()
    close_v = close.to_numpy()

    n = len(df)
    final_upper = np.zeros(n)
    final_lower = np.zeros(n)
    supertrend = np.zeros(n)
    direction = np.ones(n, dtype=int)

    for i in range(n):
        if i == 0:
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction[i] = 1 if close_v[i] >= hl2[i] else -1
            supertrend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]
            continue

        final_upper[i] = (
            basic_upper[i]
            if basic_upper[i] < final_upper[i - 1] or close_v[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower[i]
            if basic_lower[i] > final_lower[i - 1] or close_v[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )

        if close_v[i] > final_upper[i - 1]:
            direction[i] = 1
        elif close_v[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

        supertrend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return pd.DataFrame(
        {"supertrend_line": supertrend, "supertrend_direction": direction},
        index=df.index,
    )


# ── evaluate_symbol pipeline: added in Task 4 ──
# ── BTC market safety filter: added in Task 6 ──
