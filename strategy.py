"""
Nadaraya-Watson Rational Quadratic Kernel Strategy + DMI/ADX filter.

TradingView NWE settings:
    Source:                  Close
    Lookback Window:          17
    Relative Weighting:       8
    Start Regression at Bar:  30
    Smooth Colors:            False
    Lag:                      2
    Timeframe:                15m

TradingView DMI/ADX/KEYLEVEL settings:
    ADX Smoothing:            14
    DI Length:                14
    Key Level for ADX:        23

Signal logic:
    LONG:
        NWE turns bullish
        AND ADX >= 23
        AND +DI > -DI

    SHORT:
        NWE turns bearish
        AND ADX >= 23
        AND -DI > +DI

Important:
    Uses completed candles only.
    Latest in-progress candle is ignored to avoid repainting.
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
    DMI_DI_LENGTH,
    DMI_ADX_SMOOTHING,
    DMI_ADX_MIN,
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


# ── NWE helpers ───────────────────────────────────────────────────

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
    return prev_a <= prev_b and curr_a > curr_b


def _crossunder(prev_a: float, curr_a: float, prev_b: float, curr_b: float) -> bool:
    return prev_a >= prev_b and curr_a < curr_b


def _detect_signal_from_nwe(closes: np.ndarray) -> tuple[str | None, dict]:
    """
    Detect NWE color-change signal using completed candles only.

    Smooth Colors OFF:
        Bearish → Bullish = LONG
        Bullish → Bearish = SHORT
    """
    if len(closes) < NWE_SIZE + NWE_LAG + 5:
        return None, {}

    yhat1_t0 = _nwe_value(closes, h=NWE_H)
    yhat1_t1 = _nwe_value(closes[:-1], h=NWE_H)
    yhat1_t2 = _nwe_value(closes[:-2], h=NWE_H)

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


# ── DMI / ADX helpers ─────────────────────────────────────────────

def _rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's RMA, close to TradingView ta.rma behavior.
    Used for DMI/ADX calculation.
    """
    return series.ewm(alpha=1 / length, adjust=False).mean()


def _get_dmi_filter(df: pd.DataFrame) -> tuple[float, float, float]:
    """
    Returns:
        adx, plus_di, minus_di

    Uses completed candles only.
    """
    completed = df.iloc[:-1].copy()

    if len(completed) < DMI_DI_LENGTH + DMI_ADX_SMOOTHING + 5:
        return 0.0, 0.0, 0.0

    high = completed["high"].astype(float)
    low = completed["low"].astype(float)
    close = completed["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=completed.index,
    )

    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=completed.index,
    )

    high_low = high - low
    high_close = (high - close.shift()).abs()
    low_close = (low - close.shift()).abs()

    true_range = pd.concat(
        [high_low, high_close, low_close],
        axis=1,
    ).max(axis=1)

    atr = _rma(true_range, DMI_DI_LENGTH)

    plus_di = 100 * _rma(plus_dm, DMI_DI_LENGTH) / atr.replace(0, np.nan)
    minus_di = 100 * _rma(minus_dm, DMI_DI_LENGTH) / atr.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _rma(dx, DMI_ADX_SMOOTHING)

    last_adx = adx.iloc[-1]
    last_plus_di = plus_di.iloc[-1]
    last_minus_di = minus_di.iloc[-1]

    if pd.isna(last_adx) or pd.isna(last_plus_di) or pd.isna(last_minus_di):
        return 0.0, 0.0, 0.0

    return float(last_adx), float(last_plus_di), float(last_minus_di)


def _passes_dmi_filter(symbol: str, direction: str, df: pd.DataFrame) -> tuple[bool, float, float, float]:
    adx, plus_di, minus_di = _get_dmi_filter(df)

    if adx < DMI_ADX_MIN:
        logger.info(
            f"[FILTER] {symbol} {direction} skipped: "
            f"ADX {adx:.2f} < {DMI_ADX_MIN}"
        )
        return False, adx, plus_di, minus_di

    if direction == "LONG" and plus_di <= minus_di:
        logger.info(
            f"[FILTER] {symbol} LONG skipped: "
            f"+DI {plus_di:.2f} <= -DI {minus_di:.2f}"
        )
        return False, adx, plus_di, minus_di

    if direction == "SHORT" and minus_di <= plus_di:
        logger.info(
            f"[FILTER] {symbol} SHORT skipped: "
            f"-DI {minus_di:.2f} <= +DI {plus_di:.2f}"
        )
        return False, adx, plus_di, minus_di

    return True, adx, plus_di, minus_di


# ── Main analysis ─────────────────────────────────────────────────

def analyze_coin(symbol: str) -> "Signal | None":
    try:
        df = get_klines(symbol, NWE_TF, count=NWE_KLINE_COUNT)

        if df is None or df.empty:
            return None

        if len(df) < NWE_SIZE + NWE_LAG + DMI_DI_LENGTH + DMI_ADX_SMOOTHING + 10:
            return None

        # Latest candle is normally still forming.
        # Use only completed candles to keep signal non-repainting.
        closes = df["close"].values[:-1].astype(np.float64)

        if len(closes) < NWE_SIZE + NWE_LAG + 5:
            return None

        direction, details = _detect_signal_from_nwe(closes)

        if direction is None:
            return None

        passed_filter, adx, plus_di, minus_di = _passes_dmi_filter(symbol, direction, df)

        if not passed_filter:
            return None

        entry = float(closes[-1])

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

        # Give stronger score when ADX is strong.
        if adx >= 30:
            score = min(score + 10, 100)
        elif adx >= 25:
            score = min(score + 5, 100)

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{TP_ROI_PCT}% ROI) "
            f"SL={sl_price:.6g} (-{SL_ROI_PCT}% ROI) | "
            f"NWE: {yhat1_t2:.6g} → {yhat1_t1:.6g} → {yhat1_t0:.6g} | "
            f"DMI: ADX={adx:.2f}, +DI={plus_di:.2f}, -DI={minus_di:.2f} | "
            f"score={score}"
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
                f"NWE-RQK {NWE_TF.upper()} + DMI | "
                f"h={NWE_H:g} r={NWE_ALPHA:g} x0={NWE_SIZE} "
                f"ADX={adx:.1f} +DI={plus_di:.1f} -DI={minus_di:.1f}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None