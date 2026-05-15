"""
Nadaraya-Watson Rational Quadratic Kernel Strategy.

This strategy mirrors the non-repainting TradingView indicator:

    Nadaraya-Watson: Rational Quadratic Kernel (Non-Repainting)

Settings:
    Source:                  Close
    Lookback Window:          NWE_H = 17
    Relative Weighting:       NWE_ALPHA = 8
    Start Regression at Bar:  NWE_SIZE = 30
    Smooth Colors:            NWE_SMOOTH = False
    Lag:                      NWE_LAG = 2
    Timeframe:                NWE_TF

Signal logic:
    Smooth Colors OFF:
        Bearish → Bullish color change = LONG
        Bullish → Bearish color change = SHORT

    Smooth Colors ON:
        yhat2 crossover yhat1 = bullish/bearish cross logic

Important:
    Uses completed candles only.
    The latest in-progress candle is ignored to avoid repainting.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from mexc_client import get_klines
from config import (
    NWE_H,
    NWE_ALPHA,
    NWE_SIZE,
    NWE_LAG,
    NWE_SMOOTH,
    NWE_TF,
    NWE_KLINE_COUNT,
    LEVERAGE,
    TP_ROI_PCT,
    SL_ROI_PCT,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol:            str
    direction:         str
    entry_price:       float
    tp_price:          float
    sl_price:          float
    leverage:          int
    tp_roi_pct:        float
    sl_roi_pct:        float
    timeframe_summary: str
    generated_at:      datetime
    score:             float = 0.0


def _rqk_endpoint(closes: np.ndarray, h: float, alpha: float, size: int) -> float:
    """
    Rational Quadratic Kernel Nadaraya-Watson endpoint estimator.

    Weight:
        w(i) = (1 + i² / (2 * alpha * h²)) ^ (-alpha)

    Index:
        i = 0 means most recent completed candle.
    """
    bars = min(size, len(closes))

    if bars < 2:
        return float(closes[-1]) if len(closes) else 0.0

    # Reverse because index 0 should be the most recent completed candle.
    src = closes[-bars:][::-1]

    weights = np.array(
        [
            (1.0 + (i * i) / (2.0 * alpha * h * h)) ** (-alpha)
            for i in range(bars)
        ],
        dtype=np.float64,
    )

    return float(np.dot(src, weights) / weights.sum())


def _nwe_value(closes: np.ndarray, h: float) -> float:
    return _rqk_endpoint(
        closes=closes,
        h=h,
        alpha=NWE_ALPHA,
        size=NWE_SIZE,
    )


def _crossover(prev_a: float, curr_a: float, prev_b: float, curr_b: float) -> bool:
    """
    Equivalent to TradingView ta.crossover(a, b):
        previous a <= previous b and current a > current b
    """
    return prev_a <= prev_b and curr_a > curr_b


def _crossunder(prev_a: float, curr_a: float, prev_b: float, curr_b: float) -> bool:
    """
    Equivalent to TradingView ta.crossunder(a, b):
        previous a >= previous b and current a < current b
    """
    return prev_a >= prev_b and curr_a < curr_b


def _detect_signal_from_nwe(closes: np.ndarray) -> tuple[str | None, dict]:
    """
    Detect NWE color-change signal using completed candles only.

    Pine Script equivalent:

        yhat1 = kernel_regression(src, size, h)
        yhat2 = kernel_regression(src, size, h-lag)

        wasBearish = yhat1[2] > yhat1[1]
        wasBullish = yhat1[2] < yhat1[1]
        isBearish = yhat1[1] > yhat1
        isBullish = yhat1[1] < yhat1

        isBearishChange = isBearish and wasBullish
        isBullishChange = isBullish and wasBearish

    Direction mapping:
        Bullish color change = LONG
        Bearish color change = SHORT
    """

    if len(closes) < NWE_SIZE + NWE_LAG + 5:
        return None, {}

    # yhat1 at current, previous, and previous-2 completed candles
    yhat1_t0 = _nwe_value(closes, h=NWE_H)
    yhat1_t1 = _nwe_value(closes[:-1], h=NWE_H)
    yhat1_t2 = _nwe_value(closes[:-2], h=NWE_H)

    # Rate-of-change color logic
    was_bearish = yhat1_t2 > yhat1_t1
    was_bullish = yhat1_t2 < yhat1_t1

    is_bearish = yhat1_t1 > yhat1_t0
    is_bullish = yhat1_t1 < yhat1_t0

    is_bearish_change = is_bearish and was_bullish
    is_bullish_change = is_bullish and was_bearish

    direction = None

    if not NWE_SMOOTH:
        if is_bullish_change:
            direction = "LONG"
        elif is_bearish_change:
            direction = "SHORT"

    else:
        # Smooth color crossover logic using h - lag
        h2 = max(NWE_H - NWE_LAG, 1.0)

        yhat2_t0 = _nwe_value(closes, h=h2)
        yhat2_t1 = _nwe_value(closes[:-1], h=h2)

        bullish_cross = _crossover(
            prev_a=yhat2_t1,
            curr_a=yhat2_t0,
            prev_b=yhat1_t1,
            curr_b=yhat1_t0,
        )

        bearish_cross = _crossunder(
            prev_a=yhat2_t1,
            curr_a=yhat2_t0,
            prev_b=yhat1_t1,
            curr_b=yhat1_t0,
        )

        if bullish_cross:
            direction = "LONG"
        elif bearish_cross:
            direction = "SHORT"

    details = {
        "yhat1_t2": yhat1_t2,
        "yhat1_t1": yhat1_t1,
        "yhat1_t0": yhat1_t0,
        "was_bearish": was_bearish,
        "was_bullish": was_bullish,
        "is_bearish": is_bearish,
        "is_bullish": is_bullish,
        "is_bearish_change": is_bearish_change,
        "is_bullish_change": is_bullish_change,
    }

    return direction, details


def analyze_coin(symbol: str) -> "Signal | None":
    try:
        df = get_klines(symbol, NWE_TF, count=NWE_KLINE_COUNT)

        if df is None or df.empty:
            return None

        if len(df) < NWE_SIZE + NWE_LAG + 10:
            return None

        # Latest candle is normally still forming.
        # Use only completed candles to keep the strategy non-repainting.
        closes = df["close"].values[:-1].astype(np.float64)

        if len(closes) < NWE_SIZE + NWE_LAG + 5:
            return None

        direction, details = _detect_signal_from_nwe(closes)

        if direction is None:
            return None

        entry = float(closes[-1])

        # Fixed ROI targets
        tp_offset = entry * TP_ROI_PCT / (LEVERAGE * 100)
        sl_offset = entry * SL_ROI_PCT / (LEVERAGE * 100)

        if direction == "LONG":
            tp_price = round(entry + tp_offset, 8)
            sl_price = round(entry - sl_offset, 8)
        else:
            tp_price = round(entry - tp_offset, 8)
            sl_price = round(entry + sl_offset, 8)

        yhat1_t0 = details["yhat1_t0"]
        yhat1_t1 = details["yhat1_t1"]
        yhat1_t2 = details["yhat1_t2"]

        slope_now = abs(yhat1_t0 - yhat1_t1)
        slope_prev = abs(yhat1_t1 - yhat1_t2)

        accel = slope_now / (slope_prev + 1e-12)
        score = round(min(accel * 50.0, 100.0), 1)

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{TP_ROI_PCT}% ROI) "
            f"SL={sl_price:.6g} (-{SL_ROI_PCT}% ROI) | "
            f"NWE: {yhat1_t2:.6g} → {yhat1_t1:.6g} → {yhat1_t0:.6g} | "
            f"h={NWE_H} r={NWE_ALPHA} x0={NWE_SIZE} lag={NWE_LAG} "
            f"smooth={NWE_SMOOTH} | score={score}"
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=TP_ROI_PCT,
            sl_roi_pct=SL_ROI_PCT,
            timeframe_summary=(
                f"NWE-RQK {NWE_TF.upper()} | "
                f"h={NWE_H} r={NWE_ALPHA} x0={NWE_SIZE} "
                f"lag={NWE_LAG} smooth={NWE_SMOOTH}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None