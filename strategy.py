"""
Precision Pullback Scalper v1.

Dual-timeframe pipeline: TREND_TF (15m) EMA200 trend + slope gates
direction; ENTRY_TF (5m) EMA20/EMA50 alignment, a pullback into the
EMA20/EMA50 zone (bounded by NO_CHASE_MAX_DISTANCE_PCT), an RSI14
reset-then-turn, a confirming candle (body/close/volume checks), and an
ATR% volatility band all gate a candidate; a 100-point score (rewarding
trend/pullback/candle/volume quality) must clear MIN_SIGNAL_SCORE.

A passing candidate creates a PENDING setup (persisted via
database.armed_setups): entry is a breakout-buffer beyond the
confirmation candle's high/low (ENTRY_BUFFER_PCT), SL/TP are FIXED
ROI-%-at-LEVERAGE distances (TP_ROI_PCT / MAX_SL_ROI_PCT) -- not
structural or ATR-derived -- so raw RR is a constant 0.70:1 by
construction; quality control is entirely the score gate. The setup
expires after PENDING_SIGNAL_EXPIRY_CANDLES candles if price never
breaks the entry level. Once confirmed, outcome_check.check_tp_sl_with_breakeven
walks the trade to a single TP/SL, moving the stop to breakeven once
price reaches BREAKEVEN_TRIGGER_ROI_PCT.

LONG signals can be disabled via ENABLE_LONG_SIGNALS (true by default).
The last closed candle on both timeframes must be at least
MIN_CANDLE_SETTLE_SECONDS old before it's used -- MEXC's kline REST data
for a just-closed candle can still get revised shortly after close. Only
completed candles are ever used anywhere in this pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    tp2_price: float | None = None
    tp3_price: float | None = None
    position_size: float | None = None


# ── indicators ──────────────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=1, adjust=False).mean()


def calculate_zlema(series: pd.Series, length: int) -> pd.Series:
    """Zero-lag EMA per architecture.txt: lag = floor((length-1)/2);
    adjusted_price = 2*close - close.shift(lag); ZLEMA = EMA(adjusted, length)."""
    lag = (length - 1) // 2
    adjusted = 2.0 * series - series.shift(lag)
    return adjusted.ewm(span=length, adjust=False).mean()


def calculate_zlema_band(
    df: pd.DataFrame, zlema: pd.Series, atr_period: int, atr_lookback: int, multiplier: float,
) -> tuple[pd.Series, pd.Series]:
    """upper/lower = zlema +/- volatility, where volatility is the highest
    ATR(atr_period) over the last atr_lookback candles, times multiplier
    (architecture.txt's AlgoAlpha-derived band calculation)."""
    atr = calculate_atr(df, atr_period)
    volatility = atr.rolling(window=atr_lookback, min_periods=1).max() * multiplier
    return zlema + volatility, zlema - volatility


def calculate_zlema_trend_state(
    df: pd.DataFrame, zlema: pd.Series, upper: pd.Series, lower: pd.Series,
) -> pd.Series:
    """Stateful trend per architecture.txt: NOT close-vs-zlema. Flips to +1
    only when close is beyond the upper band, to -1 only when close is
    beyond the lower band, and otherwise HOLDS the previous state (starts
    neutral/0 until the first cross). Setting state=+1 every bar close
    stays above upper is equivalent to 'cross above' detection (it
    re-asserts the same value), and holding via the else branch is exactly
    the persistence architecture.txt describes -- a stateful walk (each
    bar's state depends on the previous bar's, not vectorizable as a
    comparison)."""
    close = df["close"].to_numpy()
    upper_v = upper.to_numpy()
    lower_v = lower.to_numpy()
    n = len(df)
    state = np.zeros(n, dtype=int)

    for i in range(n):
        if close[i] > upper_v[i]:
            state[i] = 1
        elif close[i] < lower_v[i]:
            state[i] = -1
        elif i > 0:
            state[i] = state[i - 1]
        # else: i == 0 and price is inside the band -> stays 0 (neutral)

    return pd.Series(state, index=df.index)


def _pullback_stage_score(direction: str, zlema_trend: pd.Series, distance_pct: float) -> float:
    """0-70: 30 flat (4H/1H agreement, already gated -- no pending setup
    exists to score without it) + up to 20 (1H ZLEMA slope strength) + up
    to 20 (15m pullback quality: full marks at half the max pullback
    distance, linear decay to 0 at the full distance)."""
    score = 30.0

    last = float(zlema_trend.iloc[-1])
    prev = (
        float(zlema_trend.iloc[-1 - ZERO_LAG_SLOPE_LOOKBACK])
        if len(zlema_trend) > ZERO_LAG_SLOPE_LOOKBACK else last
    )
    slope_move_pct = abs(last - prev) / last if last else 0.0
    score += 20.0 * min(1.0, slope_move_pct / 0.01)

    half = PULLBACK_DISTANCE_PCT / 2.0
    if distance_pct <= half:
        pullback_score = 1.0
    else:
        span = max(PULLBACK_DISTANCE_PCT - half, 1e-9)
        pullback_score = max(0.0, 1.0 - (distance_pct - half) / span)
    score += 20.0 * pullback_score

    return round(score, 1)


def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None:
    try:
        raw_macro = get_market_klines(symbol, MACRO_TF, count=MACRO_KLINE_COUNT)
        if raw_macro is None or raw_macro.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_macro = raw_macro.iloc[:-1].copy()

        raw_trend = get_market_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        if raw_trend is None or raw_trend.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_trend = raw_trend.iloc[:-1].copy()

        raw_pullback = get_market_klines(symbol, PULLBACK_TF, count=PULLBACK_KLINE_COUNT)
        if raw_pullback is None or raw_pullback.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_pullback = raw_pullback.iloc[:-1].copy()

        min_mtf_history = ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + 10
        min_pullback_history = ZERO_LAG_LENGTH + 10
        if (
            len(closed_macro) < min_mtf_history
            or len(closed_trend) < min_mtf_history
            or len(closed_pullback) < min_pullback_history
        ):
            _bump(reject_sink, "insufficient_history")
            return None

        pullback_tf_minutes = _TF_MINUTES.get(PULLBACK_TF, 15)
        candle_close_time = closed_pullback.index[-1].to_pydatetime() + timedelta(minutes=pullback_tf_minutes)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        zlema_macro = calculate_zlema(closed_macro["close"], ZERO_LAG_LENGTH)
        upper_macro, lower_macro = calculate_zlema_band(
            closed_macro, zlema_macro, ATR_PERIOD, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER,
        )
        macro_state = calculate_zlema_trend_state(closed_macro, zlema_macro, upper_macro, lower_macro)
        macro_trend = int(macro_state.iloc[-1])
        if macro_trend == 0:
            _bump(reject_sink, "no_macro_trend")
            return None

        zlema_trend = calculate_zlema(closed_trend["close"], ZERO_LAG_LENGTH)
        upper_trend, lower_trend = calculate_zlema_band(
            closed_trend, zlema_trend, ATR_PERIOD, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER,
        )
        trend_state_series = calculate_zlema_trend_state(closed_trend, zlema_trend, upper_trend, lower_trend)
        trend_state = int(trend_state_series.iloc[-1])
        if trend_state != macro_trend:
            _bump(reject_sink, "no_trend_agreement")
            return None

        direction = "LONG" if macro_trend == 1 else "SHORT"
        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            _bump(reject_sink, "long_disabled")
            return None

        zlema_pullback = calculate_zlema(closed_pullback["close"], ZERO_LAG_LENGTH)
        pullback_close = float(closed_pullback["close"].iloc[-1])
        zlema_15m_last = float(zlema_pullback.iloc[-1])

        if direction == "LONG":
            in_pullback = pullback_close <= zlema_15m_last * (1 + PULLBACK_DISTANCE_PCT)
            raw_distance_pct = (pullback_close - zlema_15m_last) / zlema_15m_last
        else:
            in_pullback = pullback_close >= zlema_15m_last * (1 - PULLBACK_DISTANCE_PCT)
            raw_distance_pct = (zlema_15m_last - pullback_close) / zlema_15m_last

        if not in_pullback:
            _bump(reject_sink, "no_pullback")
            return None
        distance_pct = abs(raw_distance_pct)

        partial_score = _pullback_stage_score(direction, zlema_trend, distance_pct)
        if partial_score + 30.0 < MIN_SIGNAL_SCORE:
            # Even a perfect breakout stage (max 30 more points) couldn't
            # clear the bar -- cheap early exit, avoids arming a setup that
            # would only get discarded later at the pending_breakout gate.
            _bump(reject_sink, "score_below_min")
            return None

        now = datetime.now(timezone.utc)
        return {
            "symbol": symbol,
            "direction": direction,
            "macro_tf": MACRO_TF,
            "trend_tf": TREND_TF,
            "pullback_tf": PULLBACK_TF,
            "entry_tf": ENTRY_TF,
            "macro_trend": macro_trend,
            "trend_state": trend_state,
            "zlema_1h": float(zlema_trend.iloc[-1]),
            "zlema_15m": zlema_15m_last,
            "pullback_price": pullback_close,
            "pullback_time": closed_pullback.index[-1].isoformat(),
            "score": partial_score,
            "setup_time": now.isoformat(),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=PENDING_EXPIRY_CANDLES * CANDLE_MINUTES)).isoformat(),
        }
    except Exception as e:
        logger.error("[ZERO-LAG-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF,
    MACRO_KLINE_COUNT, TREND_KLINE_COUNT, PULLBACK_KLINE_COUNT, ENTRY_KLINE_COUNT,
    CANDLE_MINUTES, _TF_MINUTES,
    ZERO_LAG_LENGTH, ZERO_LAG_BAND_LOOKBACK, ZERO_LAG_MULTIPLIER, ZERO_LAG_SLOPE_LOOKBACK,
    ATR_PERIOD, PULLBACK_DISTANCE_PCT, MIN_SIGNAL_SCORE,
    MIN_CANDLE_SETTLE_SECONDS, LEVERAGE, SL_PRICE_PCT, SL_ROI_PCT, TP_PRICE_PCT, TP_ROI_PCT,
    ENABLE_LONG_SIGNALS, ENTRY_BUFFER_PCT, PENDING_EXPIRY_CANDLES,
)


def valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


def direction_slot_available(direction: str, active_long: int, active_short: int) -> bool:
    """Pure correlation-limit check -- at most one pending signal per direction."""
    from config import MAX_ACTIVE_LONG_SIGNALS, MAX_ACTIVE_SHORT_SIGNALS
    if direction == "LONG":
        return active_long < MAX_ACTIVE_LONG_SIGNALS
    return active_short < MAX_ACTIVE_SHORT_SIGNALS


def _calc_rr(direction: str, entry: float, tp: float, sl: float) -> float:
    reward = abs(tp - entry)
    risk = abs(entry - sl)
    return reward / risk if risk > 0 else 0.0


def _roi_pct(direction: str, entry: float, tp: float, sl: float) -> tuple[float, float]:
    if direction == "LONG":
        tp_roi = (tp - entry) / entry * 100.0 * LEVERAGE
        sl_roi = (entry - sl) / entry * 100.0 * LEVERAGE
    else:
        tp_roi = (entry - tp) / entry * 100.0 * LEVERAGE
        sl_roi = (sl - entry) / entry * 100.0 * LEVERAGE
    return round(tp_roi, 2), round(sl_roi, 2)


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1
