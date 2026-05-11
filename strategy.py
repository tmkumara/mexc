"""
Nadaraya-Watson Rational Quadratic Kernel (NWE-RQK) Strategy.

Mirrors the "Nadaraya-Watson: Rational Quadratic Kernel (No-Repainting)"
TradingView indicator by Loxx / LuxAlgo with these settings:
  Source:               Close
  Lookback Window (h):  8
  Relative Weighting:   8
  Start Regression at Bar (size): 25
  Smooth Colors:        OFF
  Timeframe:            1H

Signal logic:
  NWE slope flips positive (red → green) → LONG
  NWE slope flips negative (green → red) → SHORT

SL / TP:
  Fixed ROI targets at configured leverage:
    TP = +TP_ROI_PCT ROI  →  entry ± TP_ROI_PCT / (LEVERAGE × 100) × entry
    SL = -SL_ROI_PCT ROI  →  entry ∓ SL_ROI_PCT / (LEVERAGE × 100) × entry
"""

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass
from datetime import datetime, timezone

from mexc_client import get_klines
from config import (
    NWE_H, NWE_ALPHA, NWE_SIZE, NWE_TF, NWE_KLINE_COUNT,
    LEVERAGE, TP_ROI_PCT, SL_ROI_PCT,
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

    Weight for bar i bars ago:
        w(i) = (1 + i² / (2·α·h²))^(−α)

    Endpoint value = Σ(close[i] · w[i]) / Σ(w[i])   for i in [0, size)
    """
    bars = min(size, len(closes))
    if bars < 2:
        return float(closes[-1]) if len(closes) else 0.0

    # index 0 = most-recent bar
    src = closes[-bars:][::-1]
    weights = np.array(
        [(1.0 + i * i / (2.0 * alpha * h * h)) ** (-alpha) for i in range(bars)],
        dtype=np.float64,
    )
    return float(np.dot(src, weights) / weights.sum())


def analyze_coin(symbol: str) -> "Signal | None":
    try:
        df = get_klines(symbol, NWE_TF, count=NWE_KLINE_COUNT)
        if df is None or df.empty or len(df) < NWE_SIZE + 5:
            return None

        # iloc[-1] is the in-progress candle — use only completed bars
        closes = df["close"].values[:-1].astype(np.float64)

        if len(closes) < NWE_SIZE + 3:
            return None

        # NWE at the last three completed bar endpoints
        nwe_t0 = _rqk_endpoint(closes,      h=NWE_H, alpha=NWE_ALPHA, size=NWE_SIZE)
        nwe_t1 = _rqk_endpoint(closes[:-1], h=NWE_H, alpha=NWE_ALPHA, size=NWE_SIZE)
        nwe_t2 = _rqk_endpoint(closes[:-2], h=NWE_H, alpha=NWE_ALPHA, size=NWE_SIZE)

        curr_green = nwe_t0 > nwe_t1   # current slope positive = green
        prev_green = nwe_t1 > nwe_t2   # previous slope

        if curr_green and not prev_green:
            direction = "LONG"
        elif not curr_green and prev_green:
            direction = "SHORT"
        else:
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

        # Score: slope acceleration — how sharply the NWE turned
        # Expressed as a 0-100 value; higher = more decisive flip
        slope_now  = abs(nwe_t0 - nwe_t1)
        slope_prev = abs(nwe_t1 - nwe_t2)
        accel = slope_now / (slope_prev + 1e-12)
        score = round(min(accel * 50.0, 100.0), 1)

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{TP_ROI_PCT}% ROI) "
            f"SL={sl_price:.6g} (-{SL_ROI_PCT}% ROI) | "
            f"NWE: {nwe_t2:.6g}→{nwe_t1:.6g}→{nwe_t0:.6g} | score={score}"
        )

        return Signal(
            symbol            = symbol,
            direction         = direction,
            entry_price       = entry,
            tp_price          = tp_price,
            sl_price          = sl_price,
            leverage          = LEVERAGE,
            tp_roi_pct        = TP_ROI_PCT,
            sl_roi_pct        = SL_ROI_PCT,
            timeframe_summary = f"NWE-RQK 1H | h={NWE_H} α={NWE_ALPHA} size={NWE_SIZE} | slope flip",
            generated_at      = datetime.now(timezone.utc),
            score             = score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
