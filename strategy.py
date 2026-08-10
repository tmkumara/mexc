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


def calculate_volume_ma(df: pd.DataFrame, period: int) -> pd.Series:
    return df["volume"].rolling(window=period, min_periods=1).mean()


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
    the persistence architecture.txt describes -- same walk shape as this
    file's calculate_supertrend, for the same reason (each bar's state
    depends on the previous bar's, not vectorizable as a comparison)."""
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


def _ema_trend_slope_up(ema_trend: pd.Series, lookback: int) -> bool:
    if len(ema_trend) <= lookback:
        return False
    return float(ema_trend.iloc[-1]) > float(ema_trend.iloc[-1 - lookback])


def _rsi_reset_ok(direction: str, rsi: pd.Series, lookback: int) -> bool:
    if len(rsi) < 2:
        return False
    if direction == "LONG":
        zone_lo, zone_hi = RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX
    else:
        zone_lo, zone_hi = RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX

    window = rsi.iloc[-(lookback + 1):]
    was_in_zone = bool(((window >= zone_lo) & (window <= zone_hi)).any())
    if not was_in_zone:
        return False

    turning = rsi.iloc[-1] > rsi.iloc[-2] if direction == "LONG" else rsi.iloc[-1] < rsi.iloc[-2]
    return bool(turning)


def _confirmation_candle_ok(direction: str, df: pd.DataFrame, ema20: pd.Series, vol_ma: pd.Series) -> bool:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close, open_ = float(last["close"]), float(last["open"])
    high, low, volume = float(last["high"]), float(last["low"]), float(last["volume"])
    ema20_last = float(ema20.iloc[-1])
    vol_ma_last = float(vol_ma.iloc[-1])

    if volume <= vol_ma_last * VOLUME_CONFIRM_MULT:
        return False

    if direction == "LONG":
        return close > open_ and close > ema20_last and close > float(prev["high"])
    return close < open_ and close < ema20_last and close < float(prev["low"])


def _abnormal_candle(df: pd.DataFrame) -> bool:
    last = df.iloc[-1]
    open_, close = float(last["open"]), float(last["close"])
    body_pct = abs(close - open_) / open_
    return body_pct > MAX_CANDLE_BODY_PCT


def _atr_pct_ok(atr_last: float, close: float) -> bool:
    atr_pct = atr_last / close
    return ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT


def _score_pending_setup(
    direction: str,
    df: pd.DataFrame,
    ema_trend: pd.Series,
    slope_lookback: int,
    pullback_distance_pct: float,
    vol_ma: pd.Series,
) -> float:
    """0-100 rubric: 15m EMA200 trend(20) + 5m EMA20/50 alignment(15) --
    both flat since they're already gated pass/fail upstream -- plus
    EMA200 slope strength(10), pullback quality(15), RSI reset(10, flat,
    already gated), confirmation-candle clearance(15), volume(10), and
    ATR environment(5, flat, already gated)."""
    score = 20.0 + 15.0

    ema_last = float(ema_trend.iloc[-1])
    ema_prev = float(ema_trend.iloc[-1 - slope_lookback]) if len(ema_trend) > slope_lookback else ema_last
    slope_move_pct = abs(ema_last - ema_prev) / ema_last if ema_last else 0.0
    score += 10.0 * min(1.0, slope_move_pct / 0.01)

    if pullback_distance_pct <= PULLBACK_PREFERRED_DISTANCE_PCT:
        pullback_score = 1.0
    else:
        span = max(NO_CHASE_MAX_DISTANCE_PCT - PULLBACK_PREFERRED_DISTANCE_PCT, 1e-9)
        pullback_score = max(0.0, 1.0 - (pullback_distance_pct - PULLBACK_PREFERRED_DISTANCE_PCT) / span)
    score += 15.0 * min(1.0, pullback_score)

    score += 10.0

    last = df.iloc[-1]
    prev = df.iloc[-2]
    candle_range = max(float(last["high"]) - float(last["low"]), 1e-9)
    if direction == "LONG":
        clearance = (float(last["close"]) - max(float(last["open"]), float(prev["high"]))) / candle_range
    else:
        clearance = (min(float(last["open"]), float(prev["low"])) - float(last["close"])) / candle_range
    score += 15.0 * min(1.0, max(0.0, clearance))

    vol_ratio = float(last["volume"]) / max(float(vol_ma.iloc[-1]), 1e-9)
    vol_score = min(1.0, max(0.0, (vol_ratio - VOLUME_CONFIRM_MULT) / VOLUME_CONFIRM_MULT))
    score += 10.0 * vol_score

    score += 5.0

    return round(min(100.0, max(0.0, score)), 1)


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
            direction[i] = 1
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


def _build_pending_setup(symbol: str, direction: str, df: pd.DataFrame) -> dict | None:
    last = df.iloc[-1]
    high, low = float(last["high"]), float(last["low"])

    if direction == "LONG":
        entry = high * (1 + ENTRY_BUFFER_PCT)
        sl = entry * (1 - MAX_SL_PRICE_PCT)
        tp = entry * (1 + TP_PRICE_PCT)
    else:
        entry = low * (1 - ENTRY_BUFFER_PCT)
        sl = entry * (1 + MAX_SL_PRICE_PCT)
        tp = entry * (1 - TP_PRICE_PCT)

    if not valid_trade_geometry(direction, entry, tp, sl):
        return None

    rr = round(TP_ROI_PCT / MAX_SL_ROI_PCT, 2)

    return {
        "symbol": symbol,
        "direction": direction,
        "trigger_price": entry,
        "entry_low": entry,
        "entry_high": entry,
        "sl_price": round(sl, 8),
        "tp_price": round(tp, 8),
        "rr": rr,
    }


def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None:
    try:
        raw_entry = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
        if raw_entry is None or raw_entry.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_entry = raw_entry.iloc[:-1].copy()

        raw_trend = get_market_klines(symbol, TREND_TF, count=ENTRY_KLINE_COUNT)
        if raw_trend is None or raw_trend.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_trend = raw_trend.iloc[:-1].copy()

        min_history = max(EMA_TREND_LEN + EMA_TREND_SLOPE_LOOKBACK, EMA_SLOW_LEN, RSI_PERIOD, VOLUME_MA_PERIOD, ATR_PERIOD) + 10
        if len(closed_entry) < min_history or len(closed_trend) < EMA_TREND_LEN + EMA_TREND_SLOPE_LOOKBACK + 10:
            _bump(reject_sink, "insufficient_history")
            return None

        candle_close_time = closed_entry.index[-1].to_pydatetime() + timedelta(minutes=CANDLE_MINUTES)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        ema_trend_15m = calculate_ema(closed_trend["close"], EMA_TREND_LEN)
        trend_close = float(closed_trend["close"].iloc[-1])
        trend_last = float(ema_trend_15m.iloc[-1])
        slope_up = _ema_trend_slope_up(ema_trend_15m, EMA_TREND_SLOPE_LOOKBACK)

        if trend_close > trend_last and slope_up:
            direction = "LONG"
        elif trend_close < trend_last and not slope_up:
            direction = "SHORT"
        else:
            _bump(reject_sink, "no_trend_alignment")
            return None

        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            _bump(reject_sink, "long_disabled")
            return None

        ema20 = calculate_ema(closed_entry["close"], EMA_FAST_LEN)
        ema50 = calculate_ema(closed_entry["close"], EMA_SLOW_LEN)
        close = float(closed_entry["close"].iloc[-1])
        ema20_last = float(ema20.iloc[-1])
        ema50_last = float(ema50.iloc[-1])

        if direction == "LONG":
            aligned = ema20_last > ema50_last and (ema20_last - ema50_last) / close >= EMA_SEPARATION_MIN_PCT
        else:
            aligned = ema20_last < ema50_last and (ema50_last - ema20_last) / close >= EMA_SEPARATION_MIN_PCT
        if not aligned:
            _bump(reject_sink, "no_ema_alignment")
            return None

        ema_trend_entry_tf = calculate_ema(closed_entry["close"], EMA_TREND_LEN)
        ema_trend_entry_last = float(ema_trend_entry_tf.iloc[-1])
        agree = (close > ema_trend_entry_last) if direction == "LONG" else (close < ema_trend_entry_last)
        if not agree:
            _bump(reject_sink, "no_ema200_agreement")
            return None

        distance_pct = abs(close - ema20_last) / close
        if distance_pct > NO_CHASE_MAX_DISTANCE_PCT:
            _bump(reject_sink, "chasing_price")
            return None

        rsi = calculate_rsi(closed_entry["close"], RSI_PERIOD)
        if not _rsi_reset_ok(direction, rsi, PULLBACK_LOOKBACK_BARS):
            _bump(reject_sink, "no_rsi_reset")
            return None

        if _abnormal_candle(closed_entry):
            _bump(reject_sink, "abnormal_candle")
            return None

        vol_ma = calculate_volume_ma(closed_entry, VOLUME_MA_PERIOD)
        if not _confirmation_candle_ok(direction, closed_entry, ema20, vol_ma):
            _bump(reject_sink, "no_confirmation_candle")
            return None

        atr = calculate_atr(closed_entry, ATR_PERIOD)
        atr_last = float(atr.iloc[-1])
        if not _atr_pct_ok(atr_last, close):
            _bump(reject_sink, "atr_out_of_band")
            return None

        setup = _build_pending_setup(symbol, direction, closed_entry)
        if setup is None:
            _bump(reject_sink, "invalid_geometry")
            return None

        score = _score_pending_setup(direction, closed_entry, ema_trend_15m, EMA_TREND_SLOPE_LOOKBACK, distance_pct, vol_ma)
        if score < MIN_SIGNAL_SCORE:
            _bump(reject_sink, "score_below_min")
            return None

        setup["score"] = score
        setup["setup_reason"] = "Precision Pullback confirmation"
        setup["trend_summary"] = f"{TREND_TF} EMA200 + {ENTRY_TF} EMA20/50 pullback"
        setup["created_at"] = datetime.now(timezone.utc).isoformat()
        setup["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES)
        ).isoformat()
        return setup
    except Exception as e:
        logger.error("[PRECISION-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None


def check_setup_confirmation(setup: dict) -> tuple[str, float | None]:
    symbol = setup["symbol"]
    direction = setup["direction"]
    entry = setup["trigger_price"]
    sl = setup["sl_price"]

    raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
    if raw is None or raw.empty:
        return "waiting", None

    closed = raw.iloc[:-1].copy()
    if closed.empty:
        return "waiting", None

    latest = closed.iloc[-1]
    high, low = float(latest["high"]), float(latest["low"])

    if direction == "LONG":
        sl_hit = low <= sl
        entry_hit = high > entry
    else:
        sl_hit = high >= sl
        entry_hit = low < entry

    if sl_hit:
        return "invalidated", None
    if entry_hit:
        return "confirmed", entry

    created_at = datetime.fromisoformat(setup["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_minutes = (datetime.now(timezone.utc) - created_at).total_seconds() / 60.0
    if age_minutes > PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES:
        return "expired", None

    return "waiting", None


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    ENTRY_TF, TREND_TF, ENTRY_KLINE_COUNT, CANDLE_MINUTES,
    EMA_FAST_LEN, EMA_SLOW_LEN, EMA_TREND_LEN, EMA_TREND_SLOPE_LOOKBACK, EMA_SEPARATION_MIN_PCT,
    RSI_PERIOD, RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX, RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX,
    PULLBACK_LOOKBACK_BARS, PULLBACK_PREFERRED_DISTANCE_PCT, NO_CHASE_MAX_DISTANCE_PCT,
    VOLUME_MA_PERIOD, VOLUME_CONFIRM_MULT, MAX_CANDLE_BODY_PCT,
    ATR_PERIOD, ATR_MIN_PCT, ATR_MAX_PCT, MIN_SIGNAL_SCORE,
    MIN_CANDLE_SETTLE_SECONDS, LEVERAGE, MAX_SL_PRICE_PCT, MAX_SL_ROI_PCT, TP_PRICE_PCT, TP_ROI_PCT,
    ENABLE_LONG_SIGNALS, ENTRY_BUFFER_PCT, PENDING_SIGNAL_EXPIRY_CANDLES,
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
