"""
Trend Speed Analyzer strategy (Zeiierman).

Pine Script source: "Trend Speed Analyzer (Zeiierman)" @version=6

Signal: Dynamic EMA crossover on the signal timeframe.
  LONG : prev_close <= prev_dema  AND  curr_close > curr_dema  AND  trendspeed > 0
  SHORT: prev_close >= prev_dema  AND  curr_close < curr_dema  AND  trendspeed < 0

Indicators:
  Dynamic EMA  — adaptive EMA; alpha accelerates on fast price moves
  Trend Speed  — cumulative RMA(close-open) momentum per wave, HMA(5)-smoothed

SL  = entry ∓ ATR(14) × SL_ATR_MULT
TP  = entry ± risk × REWARD_RATIO
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from mexc_client import get_klines
from config import (
    SIGNAL_TF,
    SIGNAL_KLINE_COUNT,
    DYN_EMA_MAX_LENGTH,
    DYN_EMA_ACCEL_MULT,
    ATR_PERIOD,
    SL_ATR_MULT,
    REWARD_RATIO,
    MIN_SL_ROI_PCT,
    MAX_SL_ROI_PCT,
    LEVERAGE,
)

logger = logging.getLogger(__name__)


# ── Signal dataclass ──────────────────────────────────────────────

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
    rr: float = 0.0
    score: float = 0.0
    entry_low: float = 0.0
    entry_high: float = 0.0


# ── Low-level indicator helpers ───────────────────────────────────

def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's Smoothed Moving Average (RMA / SMMA)."""
    values = series.to_numpy(dtype=float)
    result = np.empty(len(values))
    result[:] = np.nan
    alpha = 1.0 / period
    for i in range(len(values)):
        v = values[i]
        if np.isnan(v):
            result[i] = result[i - 1] if i > 0 else np.nan
        elif i == 0 or np.isnan(result[i - 1]):
            result[i] = v
        else:
            result[i] = alpha * v + (1.0 - alpha) * result[i - 1]
    return pd.Series(result, index=series.index)


def _wma(series: pd.Series, period: int) -> pd.Series:
    """Linearly-Weighted Moving Average."""
    weights = np.arange(1, period + 1, dtype=float)
    total = weights.sum()
    return series.rolling(period).apply(
        lambda x: float(np.dot(x, weights[-len(x):]) / weights[-len(x):].sum()),
        raw=True,
    )


def _hma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average: WMA(2·WMA(n/2) − WMA(n), √n)."""
    half   = max(1, period // 2)
    sqrt_p = max(1, round(period ** 0.5))
    return _wma(2.0 * _wma(series, half) - _wma(series, period), sqrt_p)


def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    high       = df["high"].astype(float)
    low        = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ── Core indicators (Pine Script translation) ─────────────────────

def _compute_dyn_ema(
    close: pd.Series,
    max_length: int = 50,
    accel_mult: float = 5.0,
) -> pd.Series:
    """
    Dynamic EMA with accelerating alpha.

    Pine logic:
      counts_diff         = close
      max_abs_counts_diff = highest(abs(close), 200)
      counts_diff_norm    = (close + max_abs) / (2 * max_abs)
      dyn_length          = 5 + counts_diff_norm * (max_length - 5)
      accel_factor        = abs(close - prev_close) / highest(that, 200)
      alpha               = min(1, 2/(dyn_length+1) * (1 + accel_factor * accel_mult))
      dyn_ema             = alpha * close + (1-alpha) * dyn_ema[1]
    """
    c = close.to_numpy(dtype=float)
    n = len(c)

    abs_c = np.abs(c)
    max_abs = np.array([
        np.nanmax(abs_c[max(0, i - 199): i + 1]) for i in range(n)
    ], dtype=float)
    max_abs[max_abs == 0] = 1e-10

    counts_diff_norm = (c + max_abs) / (2.0 * max_abs)
    dyn_length = 5.0 + counts_diff_norm * (max_length - 5)

    delta = np.abs(np.diff(c, prepend=c[0]))
    max_delta = np.array([
        np.nanmax(delta[max(0, i - 199): i + 1]) for i in range(n)
    ], dtype=float)
    max_delta[max_delta == 0] = 1.0
    accel_factor = delta / max_delta

    alpha_base = 2.0 / (dyn_length + 1.0)
    alpha = np.minimum(1.0, alpha_base * (1.0 + accel_factor * accel_mult))

    dyn_ema = np.empty(n, dtype=float)
    dyn_ema[0] = c[0]
    for i in range(1, n):
        dyn_ema[i] = alpha[i] * c[i] + (1.0 - alpha[i]) * dyn_ema[i - 1]

    return pd.Series(dyn_ema, index=close.index)


def _compute_trend_speed(
    close: pd.Series,
    open_: pd.Series,
    dyn_ema: pd.Series,
) -> pd.Series:
    """
    Cumulative momentum per trend wave, HMA(5)-smoothed.

    Pine logic:
      c = rma(close, 10);  o = rma(open, 10)
      On bullish cross: speed = c - o  (reset)
      On bearish cross: speed = c - o  (reset)
      Otherwise:        speed += c - o
      trendspeed = hma(speed, 5)
    """
    c_arr     = _rma(close,  10).to_numpy(dtype=float)
    o_arr     = _rma(open_,  10).to_numpy(dtype=float)
    close_arr = close.to_numpy(dtype=float)
    dema_arr  = dyn_ema.to_numpy(dtype=float)

    n = len(close_arr)
    speed = np.zeros(n, dtype=float)

    for i in range(1, n):
        diff_i    = c_arr[i] - o_arr[i]
        bull_cross = close_arr[i] > dema_arr[i] and close_arr[i - 1] <= dema_arr[i - 1]
        bear_cross = close_arr[i] < dema_arr[i] and close_arr[i - 1] >= dema_arr[i - 1]

        if bull_cross or bear_cross:
            speed[i] = diff_i
        else:
            speed[i] = speed[i - 1] + diff_i

    return _hma(pd.Series(speed, index=close.index), 5)


# ── Geometry guard ────────────────────────────────────────────────

def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


# ── Public: scan one symbol ───────────────────────────────────────

def scan_symbol(symbol: str) -> Signal | None:
    """
    Fetch klines, compute indicators, return Signal on DynEMA crossover
    confirmed by trendspeed direction, or None if no signal.
    """
    try:
        df = get_klines(symbol, SIGNAL_TF, count=SIGNAL_KLINE_COUNT)
        if df is None or df.empty or len(df) < 210:
            return None

        # Use only completed candles (drop in-progress last bar)
        completed = df.iloc[:-1].copy()
        if len(completed) < 205:
            return None

        close = completed["close"].astype(float)
        open_ = completed["open"].astype(float)

        dyn_ema    = _compute_dyn_ema(close, DYN_EMA_MAX_LENGTH, DYN_EMA_ACCEL_MULT)
        trendspeed = _compute_trend_speed(close, open_, dyn_ema)

        curr_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        curr_dema  = float(dyn_ema.iloc[-1])
        prev_dema  = float(dyn_ema.iloc[-2])
        curr_speed = float(trendspeed.iloc[-1])

        long_cross  = prev_close <= prev_dema and curr_close > curr_dema
        short_cross = prev_close >= prev_dema and curr_close < curr_dema

        if not long_cross and not short_cross:
            return None

        direction = "LONG" if long_cross else "SHORT"

        # Trend speed must confirm direction
        if direction == "LONG" and curr_speed <= 0:
            logger.info("[REJECT] %s LONG crossover but speed=%.4f not positive", symbol, curr_speed)
            return None
        if direction == "SHORT" and curr_speed >= 0:
            logger.info("[REJECT] %s SHORT crossover but speed=%.4f not negative", symbol, curr_speed)
            return None

        # ATR-based stop loss
        atr_val = float(_atr_series(completed, ATR_PERIOD).iloc[-1])
        if atr_val <= 0:
            return None

        entry = curr_close
        if direction == "LONG":
            sl_price = entry - atr_val * SL_ATR_MULT
            risk     = entry - sl_price
            tp_price = entry + risk * REWARD_RATIO
        else:
            sl_price = entry + atr_val * SL_ATR_MULT
            risk     = sl_price - entry
            tp_price = entry - risk * REWARD_RATIO

        if not _valid_trade_geometry(direction, entry, tp_price, sl_price):
            return None

        # Leveraged SL ROI bounds
        sl_roi_pct = risk / entry * 100.0 * LEVERAGE
        if sl_roi_pct < MIN_SL_ROI_PCT or sl_roi_pct > MAX_SL_ROI_PCT:
            logger.info(
                "[REJECT] %s %s SL ROI %.1f%% outside [%.0f%%, %.0f%%]",
                symbol, direction, sl_roi_pct, MIN_SL_ROI_PCT, MAX_SL_ROI_PCT,
            )
            return None

        if direction == "LONG":
            tp_roi_pct = (tp_price - entry) / entry * 100.0 * LEVERAGE
        else:
            tp_roi_pct = (entry - tp_price) / entry * 100.0 * LEVERAGE

        rr = REWARD_RATIO

        logger.info(
            "[SIGNAL] %s %s entry=%.6g TP=%.6g SL=%.6g RR=%.1f speed=%.4f ATR=%.6g",
            symbol, direction, entry, tp_price, sl_price, rr, curr_speed, atr_val,
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp_price, 8),
            sl_price=round(sl_price, 8),
            leverage=LEVERAGE,
            tp_roi_pct=round(tp_roi_pct, 1),
            sl_roi_pct=round(sl_roi_pct, 1),
            timeframe_summary=f"{SIGNAL_TF.upper()} DynEMA cross | speed {curr_speed:+.4f}",
            generated_at=datetime.now(timezone.utc),
            rr=round(rr, 2),
            score=0.0,
        )

    except Exception as e:
        logger.error("Error scanning %s: %s", symbol, e, exc_info=True)
        return None
