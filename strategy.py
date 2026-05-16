"""
EMA + VWAP Pullback Scalping Strategy with Dynamic ATR SL/TP.

This strategy replaces Nadaraya-Watson and Supertrend.

Structure:
    15m = trend confirmation
    5m  = entry trigger

Indicators:
    15m EMA 50
    5m EMA 9
    5m EMA 21
    5m VWAP
    5m Volume SMA 20
    5m ATR 14

LONG:
    15m close > 15m EMA 50
    5m close > VWAP
    5m EMA 9 > EMA 21
    Price pulled back near EMA 9 or EMA 21
    Last completed 5m candle closes bullish
    Volume >= MIN_VOLUME_RATIO × Volume SMA 20
    Candle body is not too large

SHORT:
    15m close < 15m EMA 50
    5m close < VWAP
    5m EMA 9 < EMA 21
    Price pulled back near EMA 9 or EMA 21
    Last completed 5m candle closes bearish
    Volume >= MIN_VOLUME_RATIO × Volume SMA 20
    Candle body is not too large

Risk:
    SL = ATR-based dynamic stop
    TP = SL distance × dynamic RR
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from mexc_client import get_klines
from config import (
    TREND_TF,
    ENTRY_TF,
    TREND_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    TREND_EMA_PERIOD,
    EMA_FAST_PERIOD,
    EMA_SLOW_PERIOD,
    VOLUME_SMA_PERIOD,
    MIN_VOLUME_RATIO,
    MAX_ENTRY_DISTANCE_FROM_EMA_PCT,
    MAX_SIGNAL_CANDLE_BODY_PCT,
    DYNAMIC_RISK_ENABLED,
    ATR_PERIOD,
    SL_ATR_MULTIPLIER,
    MIN_SL_PCT,
    MAX_SL_PCT,
    RR_WEAK,
    RR_GOOD,
    RR_STRONG,
    SCORE_GOOD_MIN,
    SCORE_STRONG_MIN,
    LEVERAGE,
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


# ── Indicator helpers ─────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rma(series: pd.Series, length: int) -> pd.Series:
    """
    Wilder's RMA, commonly used for ATR.
    """
    return series.ewm(alpha=1 / length, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    high_low = high - low
    high_close = (high - close.shift(1)).abs()
    low_close = (low - close.shift(1)).abs()

    true_range = pd.concat(
        [high_low, high_close, low_close],
        axis=1,
    ).max(axis=1)

    return _rma(true_range, period)


def _vwap(df: pd.DataFrame) -> pd.Series:
    """
    Session VWAP calculated per UTC day.

    Formula:
        cumulative(typical_price * volume) / cumulative(volume)
    """
    typical_price = (
        df["high"].astype(float)
        + df["low"].astype(float)
        + df["close"].astype(float)
    ) / 3.0

    volume = df["volume"].astype(float).replace(0, np.nan)

    date_key = df.index.date

    pv = typical_price * volume
    cumulative_pv = pv.groupby(date_key).cumsum()
    cumulative_volume = volume.groupby(date_key).cumsum()

    vwap = cumulative_pv / cumulative_volume
    return vwap.ffill()


def _distance_pct(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 999.0
    return abs(price - level) / price * 100.0


# ── Trend confirmation ────────────────────────────────────────────

def _get_trend_direction(trend_df: pd.DataFrame) -> tuple[str | None, dict]:
    """
    15m trend filter:
        LONG  if close > EMA50
        SHORT if close < EMA50
    """
    completed = trend_df.iloc[:-1].copy()

    if len(completed) < TREND_EMA_PERIOD + 5:
        return None, {}

    close = completed["close"].astype(float)
    ema_trend = _ema(close, TREND_EMA_PERIOD)

    last_close = float(close.iloc[-1])
    last_ema = float(ema_trend.iloc[-1])

    if pd.isna(last_ema):
        return None, {}

    if last_close > last_ema:
        direction = "LONG"
    elif last_close < last_ema:
        direction = "SHORT"
    else:
        direction = None

    details = {
        "trend_close": last_close,
        "trend_ema": last_ema,
    }

    return direction, details


# ── Entry analysis ────────────────────────────────────────────────

def _analyze_entry(entry_df: pd.DataFrame, trend_direction: str) -> tuple[str | None, dict]:
    """
    5m EMA + VWAP pullback entry trigger.
    """
    completed = entry_df.iloc[:-1].copy()

    min_bars = max(
        EMA_SLOW_PERIOD,
        VOLUME_SMA_PERIOD,
        ATR_PERIOD,
    ) + 10

    if len(completed) < min_bars:
        return None, {}

    open_ = completed["open"].astype(float)
    high = completed["high"].astype(float)
    low = completed["low"].astype(float)
    close = completed["close"].astype(float)
    volume = completed["volume"].astype(float)

    ema_fast = _ema(close, EMA_FAST_PERIOD)
    ema_slow = _ema(close, EMA_SLOW_PERIOD)
    vwap = _vwap(completed)
    vol_sma = volume.rolling(VOLUME_SMA_PERIOD).mean()
    atr = _atr(completed, ATR_PERIOD)

    last_open = float(open_.iloc[-1])
    last_high = float(high.iloc[-1])
    last_low = float(low.iloc[-1])
    last_close = float(close.iloc[-1])
    last_volume = float(volume.iloc[-1])

    last_ema_fast = float(ema_fast.iloc[-1])
    last_ema_slow = float(ema_slow.iloc[-1])
    last_vwap = float(vwap.iloc[-1])
    last_vol_sma = float(vol_sma.iloc[-1])
    last_atr = float(atr.iloc[-1])

    if any(pd.isna(x) for x in [
        last_ema_fast,
        last_ema_slow,
        last_vwap,
        last_vol_sma,
        last_atr,
    ]):
        return None, {}

    if last_close <= 0:
        return None, {}

    candle_body_pct = abs(last_close - last_open) / last_close * 100.0
    volume_ratio = last_volume / last_vol_sma if last_vol_sma > 0 else 0.0

    distance_to_fast = _distance_pct(last_close, last_ema_fast)
    distance_to_slow = _distance_pct(last_close, last_ema_slow)
    min_ema_distance = min(distance_to_fast, distance_to_slow)

    pulled_back_to_ema_long = (
        last_low <= last_ema_fast
        or last_low <= last_ema_slow
        or min_ema_distance <= MAX_ENTRY_DISTANCE_FROM_EMA_PCT
    )

    pulled_back_to_ema_short = (
        last_high >= last_ema_fast
        or last_high >= last_ema_slow
        or min_ema_distance <= MAX_ENTRY_DISTANCE_FROM_EMA_PCT
    )

    bullish_candle = last_close > last_open
    bearish_candle = last_close < last_open

    volume_ok = volume_ratio >= MIN_VOLUME_RATIO
    body_ok = candle_body_pct <= MAX_SIGNAL_CANDLE_BODY_PCT
    distance_ok = min_ema_distance <= MAX_ENTRY_DISTANCE_FROM_EMA_PCT

    direction = None

    if (
        trend_direction == "LONG"
        and last_close > last_vwap
        and last_ema_fast > last_ema_slow
        and pulled_back_to_ema_long
        and bullish_candle
        and volume_ok
        and body_ok
        and distance_ok
    ):
        direction = "LONG"

    elif (
        trend_direction == "SHORT"
        and last_close < last_vwap
        and last_ema_fast < last_ema_slow
        and pulled_back_to_ema_short
        and bearish_candle
        and volume_ok
        and body_ok
        and distance_ok
    ):
        direction = "SHORT"

    details = {
        "entry": last_close,
        "open": last_open,
        "high": last_high,
        "low": last_low,
        "ema_fast": last_ema_fast,
        "ema_slow": last_ema_slow,
        "vwap": last_vwap,
        "volume": last_volume,
        "volume_sma": last_vol_sma,
        "volume_ratio": volume_ratio,
        "atr": last_atr,
        "candle_body_pct": candle_body_pct,
        "distance_to_fast_pct": distance_to_fast,
        "distance_to_slow_pct": distance_to_slow,
        "min_ema_distance_pct": min_ema_distance,
        "volume_ok": volume_ok,
        "body_ok": body_ok,
        "distance_ok": distance_ok,
        "bullish_candle": bullish_candle,
        "bearish_candle": bearish_candle,
    }

    return direction, details


# ── Scoring and risk ──────────────────────────────────────────────

def _calculate_score(direction: str, trend_details: dict, entry_details: dict) -> float:
    score = 0.0

    # Trend alignment
    score += 25.0

    # VWAP confirmation
    entry = entry_details["entry"]
    vwap = entry_details["vwap"]

    if direction == "LONG" and entry > vwap:
        score += 15.0
    elif direction == "SHORT" and entry < vwap:
        score += 15.0

    # EMA structure
    ema_fast = entry_details["ema_fast"]
    ema_slow = entry_details["ema_slow"]

    ema_spread_pct = abs(ema_fast - ema_slow) / entry * 100.0

    if ema_spread_pct >= 0.10:
        score += 15.0
    elif ema_spread_pct >= 0.05:
        score += 10.0
    else:
        score += 5.0

    # Entry distance quality
    dist = entry_details["min_ema_distance_pct"]

    if dist <= 0.08:
        score += 20.0
    elif dist <= 0.15:
        score += 15.0
    elif dist <= MAX_ENTRY_DISTANCE_FROM_EMA_PCT:
        score += 10.0

    # Volume strength
    vol_ratio = entry_details["volume_ratio"]

    if vol_ratio >= 1.50:
        score += 15.0
    elif vol_ratio >= 1.25:
        score += 10.0
    elif vol_ratio >= MIN_VOLUME_RATIO:
        score += 5.0

    # Candle body quality
    body_pct = entry_details["candle_body_pct"]

    if body_pct <= 0.20:
        score += 10.0
    elif body_pct <= MAX_SIGNAL_CANDLE_BODY_PCT:
        score += 5.0

    return round(min(score, 100.0), 1)


def _select_rr(score: float) -> float:
    if score >= SCORE_STRONG_MIN:
        return RR_STRONG

    if score >= SCORE_GOOD_MIN:
        return RR_GOOD

    return RR_WEAK


def _calculate_dynamic_prices(
    direction: str,
    entry: float,
    atr_value: float,
    score: float,
) -> tuple[float, float, float, float]:
    """
    Returns:
        tp_price, sl_price, tp_roi_pct, sl_roi_pct
    """
    if entry <= 0:
        return entry, entry, 0.0, 0.0

    rr = _select_rr(score)

    raw_risk_distance = atr_value * SL_ATR_MULTIPLIER

    min_risk_distance = entry * MIN_SL_PCT / 100.0
    max_risk_distance = entry * MAX_SL_PCT / 100.0

    risk_distance = max(raw_risk_distance, min_risk_distance)
    risk_distance = min(risk_distance, max_risk_distance)

    reward_distance = risk_distance * rr

    if direction == "LONG":
        sl_price = entry - risk_distance
        tp_price = entry + reward_distance
    else:
        sl_price = entry + risk_distance
        tp_price = entry - reward_distance

    if direction == "LONG":
        tp_price_move_pct = (tp_price - entry) / entry * 100.0
        sl_price_move_pct = (entry - sl_price) / entry * 100.0
    else:
        tp_price_move_pct = (entry - tp_price) / entry * 100.0
        sl_price_move_pct = (sl_price - entry) / entry * 100.0

    tp_roi_pct = tp_price_move_pct * LEVERAGE
    sl_roi_pct = sl_price_move_pct * LEVERAGE

    return (
        round(tp_price, 8),
        round(sl_price, 8),
        round(tp_roi_pct, 1),
        round(sl_roi_pct, 1),
    )


# ── Main analysis ─────────────────────────────────────────────────

def analyze_coin(symbol: str) -> "Signal | None":
    try:
        trend_df = get_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        entry_df = get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if trend_df is None or trend_df.empty:
            return None

        if entry_df is None or entry_df.empty:
            return None

        trend_direction, trend_details = _get_trend_direction(trend_df)

        if trend_direction is None:
            return None

        direction, entry_details = _analyze_entry(entry_df, trend_direction)

        if direction is None:
            return None

        entry = float(entry_details["entry"])
        atr_value = float(entry_details["atr"])

        score = _calculate_score(direction, trend_details, entry_details)

        tp_price, sl_price, tp_roi_pct, sl_roi_pct = _calculate_dynamic_prices(
            direction=direction,
            entry=entry,
            atr_value=atr_value,
            score=score,
        )

        rr = _select_rr(score)

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{tp_roi_pct:.1f}% ROI) "
            f"SL={sl_price:.6g} (-{sl_roi_pct:.1f}% ROI) | "
            f"RR={rr:.2f} | "
            f"trend={TREND_TF} close={trend_details.get('trend_close', 0):.6g} "
            f"ema{TREND_EMA_PERIOD}={trend_details.get('trend_ema', 0):.6g} | "
            f"entry={ENTRY_TF} ema{EMA_FAST_PERIOD}={entry_details['ema_fast']:.6g} "
            f"ema{EMA_SLOW_PERIOD}={entry_details['ema_slow']:.6g} "
            f"vwap={entry_details['vwap']:.6g} "
            f"volRatio={entry_details['volume_ratio']:.2f} "
            f"body={entry_details['candle_body_pct']:.3f}% "
            f"dist={entry_details['min_ema_distance_pct']:.3f}% "
            f"atr={atr_value:.6g} | score={score}"
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            timeframe_summary=(
                f"EMA/VWAP Pullback | Trend {TREND_TF} EMA{TREND_EMA_PERIOD} | "
                f"Entry {ENTRY_TF} EMA{EMA_FAST_PERIOD}/{EMA_SLOW_PERIOD} + VWAP | "
                f"ATR{ATR_PERIOD} SLx{SL_ATR_MULTIPLIER:g} RR {rr:g}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None