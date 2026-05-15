"""
Nadaraya-Watson Rational Quadratic Kernel Strategy + Supertrend filter.

NWE TradingView settings:
    Source:                  Close
    Lookback Window:          32
    Relative Weighting:       25
    Start Regression at Bar:  233
    Smooth Colors:            True
    Lag:                      7
    Timeframe:                15m

Supertrend settings:
    ATR Length:               10
    Factor:                   3
    Timeframe:                same as NWE_TF

Signal logic:
    LONG:
        NWE yhat2 crosses above yhat1
        AND Supertrend is bullish

    SHORT:
        NWE yhat2 crosses below yhat1
        AND Supertrend is bearish

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
    SUPERTREND_ENABLED,
    SUPERTREND_ATR_LENGTH,
    SUPERTREND_FACTOR,
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

    TradingView equivalent weight:
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
    Detect NWE signal using completed candles only.

    Smooth Colors ON:
        yhat2 crossover yhat1 = LONG
        yhat2 crossunder yhat1 = SHORT

    Smooth Colors OFF:
        slope color change = LONG / SHORT
    """
    if len(closes) < NWE_SIZE + NWE_LAG + 5:
        return None, {}

    yhat1_t0 = _nwe_value(closes, h=NWE_H)
    yhat1_t1 = _nwe_value(closes[:-1], h=NWE_H)
    yhat1_t2 = _nwe_value(closes[:-2], h=NWE_H)

    direction = None

    if NWE_SMOOTH:
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
            "mode": "smooth-cross",
            "yhat1_t2": yhat1_t2,
            "yhat1_t1": yhat1_t1,
            "yhat1_t0": yhat1_t0,
            "yhat2_t1": yhat2_t1,
            "yhat2_t0": yhat2_t0,
        }

        return direction, details

    # Smooth Colors OFF fallback
    was_bearish = yhat1_t2 > yhat1_t1
    was_bullish = yhat1_t2 < yhat1_t1

    is_bearish = yhat1_t1 > yhat1_t0
    is_bullish = yhat1_t1 < yhat1_t0

    is_bearish_change = is_bearish and was_bullish
    is_bullish_change = is_bullish and was_bearish

    if is_bullish_change:
        direction = "LONG"
    elif is_bearish_change:
        direction = "SHORT"

    details = {
        "mode": "slope-change",
        "yhat1_t2": yhat1_t2,
        "yhat1_t1": yhat1_t1,
        "yhat1_t0": yhat1_t0,
    }

    return direction, details


# ── Supertrend helpers ────────────────────────────────────────────

def _rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's RMA, close to TradingView ta.rma behavior.
    """
    return series.ewm(alpha=1 / length, adjust=False).mean()


def _calculate_supertrend(df: pd.DataFrame) -> tuple[str | None, float | None]:
    """
    Calculate Supertrend on completed candles only.

    Returns:
        ("LONG", supertrend_value)  when bullish
        ("SHORT", supertrend_value) when bearish
        (None, None)                when not enough data
    """
    completed = df.iloc[:-1].copy()

    min_bars = SUPERTREND_ATR_LENGTH + 10
    if completed.empty or len(completed) < min_bars:
        return None, None

    high = completed["high"].astype(float)
    low = completed["low"].astype(float)
    close = completed["close"].astype(float)

    hl2 = (high + low) / 2.0

    high_low = high - low
    high_close = (high - close.shift(1)).abs()
    low_close = (low - close.shift(1)).abs()

    true_range = pd.concat(
        [high_low, high_close, low_close],
        axis=1,
    ).max(axis=1)

    atr = _rma(true_range, SUPERTREND_ATR_LENGTH)

    basic_upper = hl2 + (SUPERTREND_FACTOR * atr)
    basic_lower = hl2 - (SUPERTREND_FACTOR * atr)

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    trend = pd.Series(index=completed.index, dtype="int64")
    supertrend = pd.Series(index=completed.index, dtype="float64")

    for i in range(len(completed)):
        if i == 0:
            trend.iloc[i] = 1
            final_upper.iloc[i] = basic_upper.iloc[i]
            final_lower.iloc[i] = basic_lower.iloc[i]
            supertrend.iloc[i] = final_lower.iloc[i]
            continue

        prev_i = i - 1

        if (
            basic_upper.iloc[i] < final_upper.iloc[prev_i]
            or close.iloc[prev_i] > final_upper.iloc[prev_i]
        ):
            final_upper.iloc[i] = basic_upper.iloc[i]
        else:
            final_upper.iloc[i] = final_upper.iloc[prev_i]

        if (
            basic_lower.iloc[i] > final_lower.iloc[prev_i]
            or close.iloc[prev_i] < final_lower.iloc[prev_i]
        ):
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_lower.iloc[i] = final_lower.iloc[prev_i]

        if trend.iloc[prev_i] == -1 and close.iloc[i] > final_upper.iloc[i]:
            trend.iloc[i] = 1
        elif trend.iloc[prev_i] == 1 and close.iloc[i] < final_lower.iloc[i]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[prev_i]

        supertrend.iloc[i] = final_lower.iloc[i] if trend.iloc[i] == 1 else final_upper.iloc[i]

    last_trend = int(trend.iloc[-1])
    last_value = float(supertrend.iloc[-1])

    if last_trend == 1:
        return "LONG", last_value

    if last_trend == -1:
        return "SHORT", last_value

    return None, None


def _passes_supertrend_filter(symbol: str, direction: str, df: pd.DataFrame) -> tuple[bool, str | None, float | None]:
    if not SUPERTREND_ENABLED:
        return True, None, None

    st_direction, st_value = _calculate_supertrend(df)

    if st_direction is None:
        logger.info(f"[FILTER] {symbol} {direction} skipped: Supertrend unavailable")
        return False, st_direction, st_value

    if direction != st_direction:
        logger.info(
            f"[FILTER] {symbol} {direction} skipped: "
            f"Supertrend={st_direction} value={st_value:.6g}"
        )
        return False, st_direction, st_value

    return True, st_direction, st_value


# ── Main analysis ─────────────────────────────────────────────────

def analyze_coin(symbol: str) -> "Signal | None":
    try:
        df = get_klines(symbol, NWE_TF, count=NWE_KLINE_COUNT)

        if df is None or df.empty:
            return None

        if len(df) < NWE_SIZE + NWE_LAG + SUPERTREND_ATR_LENGTH + 10:
            return None

        # Latest candle is normally still forming.
        # Use only completed candles to keep the strategy non-repainting.
        closes = df["close"].values[:-1].astype(np.float64)

        if len(closes) < NWE_SIZE + NWE_LAG + 5:
            return None

        direction, details = _detect_signal_from_nwe(closes)

        if direction is None:
            return None

        passed_filter, st_direction, st_value = _passes_supertrend_filter(symbol, direction, df)

        if not passed_filter:
            return None

        entry = float(closes[-1])

        # Fixed ROI targets.
        # 20x leverage + 5% ROI = 0.25% price movement.
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

        if details.get("mode") == "smooth-cross":
            yhat2_t0 = details.get("yhat2_t0", 0.0)
            cross_strength = abs(yhat2_t0 - yhat1_t0) / (entry + 1e-12) * 10000
            score = round(min(score + cross_strength, 100.0), 1)

        if SUPERTREND_ENABLED:
            score = round(min(score + 10.0, 100.0), 1)

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{TP_ROI_PCT}% ROI) "
            f"SL={sl_price:.6g} (-{SL_ROI_PCT}% ROI) | "
            f"mode={details.get('mode')} | "
            f"NWE yhat1: {yhat1_t2:.6g} → {yhat1_t1:.6g} → {yhat1_t0:.6g} | "
            f"Supertrend={st_direction} value={st_value if st_value is not None else 0:.6g} | "
            f"h={NWE_H} r={NWE_ALPHA} x0={NWE_SIZE} lag={NWE_LAG} "
            f"smooth={NWE_SMOOTH} | score={score}"
        )

        st_text = ""
        if SUPERTREND_ENABLED and st_direction is not None and st_value is not None:
            st_text = f" | ST={st_direction} {st_value:.6g}"

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
                f"NWE-RQK {NWE_TF.upper()} + Supertrend | "
                f"NWE h={NWE_H:g} r={NWE_ALPHA:g} x0={NWE_SIZE} lag={NWE_LAG} "
                f"| ST {SUPERTREND_ATR_LENGTH}/{SUPERTREND_FACTOR:g}"
                f"{st_text}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None