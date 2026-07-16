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
