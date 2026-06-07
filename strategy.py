"""
Stateful SMC / Market Structure Strategy.

No classic indicators are used.

Flow:
    1. detect_setup(symbol)
       - 15m market structure bias
       - 1H/4H higher timeframe direction filter
       - 5m liquidity sweep
       - 5m displacement candle
       - 5m order block
       - stores pending setup in database via main.py

    2. evaluate_pending_setup(setup)
       - checks whether price retested OB zone
       - validates confirmation candle
       - recalculates RR from actual retest entry
       - returns Signal only when entry confirms
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd

from mexc_client import get_klines
import config as _cfg
from config import (
    TREND_TF,
    ENTRY_TF,
    TREND_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    MONITOR_KLINE_COUNT,
    SWING_LEFT,
    SWING_RIGHT,
    STRUCTURE_LOOKBACK,
    ENTRY_LOOKBACK,
    SWEEP_LOOKBACK,
    AVG_BODY_PERIOD,
    DISPLACEMENT_BODY_MULTIPLIER,
    DISPLACEMENT_CLOSE_POSITION,
    ORDER_BLOCK_LOOKBACK,
    PENDING_SETUP_EXPIRE_CANDLES,
    MAX_SIGNAL_CANDLE_BODY_PCT,
    MIN_STRUCTURE_RR,
    MAX_STRUCTURE_RR,
    SL_BUFFER_PCT,
    TP_BUFFER_PCT,
    MIN_SL_PCT,
    MAX_SL_PCT,
    LEVERAGE,
    CANDLE_MINUTES,
)

logger = logging.getLogger(__name__)

# ── optional config values with safe defaults ─────────────────────
# These are read via getattr so strategy.py does not crash if config.py
# is slightly older on the server/local branch.
SETUP_MONITOR_LOG_DETAILS: bool = getattr(_cfg, "SETUP_MONITOR_LOG_DETAILS", True)
ENABLE_ATR_FILTER: bool = getattr(_cfg, "ENABLE_ATR_FILTER", True)
ATR_PERIOD: int = int(getattr(_cfg, "ATR_PERIOD", 14))
MIN_ATR_PCT: float = float(getattr(_cfg, "MIN_ATR_PCT", 0.18))
MAX_ATR_PCT: float = float(getattr(_cfg, "MAX_ATR_PCT", 2.50))
ATR_SL_MULTIPLIER: float = float(getattr(_cfg, "ATR_SL_MULTIPLIER", 0.25))
ATR_STOP_FLOOR_MULTIPLIER: float = float(getattr(_cfg, "ATR_STOP_FLOOR_MULTIPLIER", 0.75))
MAX_OB_DISTANCE_ATR: float = float(getattr(_cfg, "MAX_OB_DISTANCE_ATR", 5.0))
MAX_OB_DISTANCE_PCT: float = float(getattr(_cfg, "MAX_OB_DISTANCE_PCT", 4.0))
EXPIRE_IF_PRICE_AWAY_ATR: float = float(getattr(_cfg, "EXPIRE_IF_PRICE_AWAY_ATR", 5.0))
EXPIRE_IF_PRICE_AWAY_PCT: float = float(getattr(_cfg, "EXPIRE_IF_PRICE_AWAY_PCT", 4.0))
OB_ENTRY_QUALITY_CHECK: bool = getattr(_cfg, "OB_ENTRY_QUALITY_CHECK", True)
REVALIDATE_BEFORE_FIRE: bool = getattr(_cfg, "REVALIDATE_BEFORE_FIRE", True)
REQUIRE_MSS_BREAK_ENTRY: bool = getattr(_cfg, "REQUIRE_MSS_BREAK_ENTRY", True)
MSS_BREAK_LOOKBACK_CANDLES: int = int(getattr(_cfg, "MSS_BREAK_LOOKBACK_CANDLES", 4))
REQUIRE_TREND_CANDLE_CONFIRMATION: bool = getattr(_cfg, "REQUIRE_TREND_CANDLE_CONFIRMATION", True)
TREND_CONFIRM_TF: str = str(getattr(_cfg, "TREND_CONFIRM_TF", TREND_TF))

# Higher timeframe direction filter.
# This is intentionally applied before saving setups, so counter-trend
# 15m setups are not stored and later fired.
ENABLE_HTF_FILTER: bool = bool(getattr(_cfg, "ENABLE_HTF_FILTER", True))
HTF_CONFIRM_TFS: list[str] = list(getattr(_cfg, "HTF_CONFIRM_TFS", ["1h", "4h"]))
HTF_KLINE_COUNT: int = int(getattr(_cfg, "HTF_KLINE_COUNT", 260))
HTF_EMA_FAST: int = int(getattr(_cfg, "HTF_EMA_FAST", 50))
HTF_EMA_SLOW: int = int(getattr(_cfg, "HTF_EMA_SLOW", 200))
REQUIRE_HTF_EMA_STACK: bool = bool(getattr(_cfg, "REQUIRE_HTF_EMA_STACK", True))
REQUIRE_HTF_EMA_SLOPE: bool = bool(getattr(_cfg, "REQUIRE_HTF_EMA_SLOPE", False))
HTF_EMA_SLOPE_LOOKBACK: int = int(getattr(_cfg, "HTF_EMA_SLOPE_LOOKBACK", 3))


def _debug_wait(symbol: str, message: str) -> None:
    if SETUP_MONITOR_LOG_DETAILS:
        logger.info("[SETUP-WAIT] %s | %s", symbol, message)


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


# ── candle helpers ────────────────────────────────────────────────

def _body_size(row: pd.Series) -> float:
    return abs(float(row["close"]) - float(row["open"]))


def _is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])

    if close <= 0:
        return 0.0

    return _body_size(row) / close * 100.0


def _close_position(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    rng = high - low

    if rng <= 0:
        return 0.5

    return (close - low) / rng


def _avg_body(df: pd.DataFrame, pos: int, period: int) -> float:
    start = max(0, pos - period)
    subset = df.iloc[start:pos]

    if subset.empty:
        return 0.0

    bodies = (subset["close"].astype(float) - subset["open"].astype(float)).abs()
    return float(bodies.mean())


def _touches_zone(row: pd.Series, zone_low: float, zone_high: float) -> bool:
    high = float(row["high"])
    low = float(row["low"])

    return low <= zone_high and high >= zone_low


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return true_range.ewm(span=period, adjust=False).mean()


def _atr_status(df: pd.DataFrame) -> tuple[bool, float, float]:
    """Return (ok, atr_pct, raw_atr)."""
    if not ENABLE_ATR_FILTER:
        return True, 0.0, 0.0

    if len(df) < ATR_PERIOD + 5:
        return True, 0.0, 0.0

    atr_value = float(_atr(df, ATR_PERIOD).iloc[-1])
    close = float(df["close"].astype(float).iloc[-1])

    if close <= 0:
        return True, 0.0, atr_value

    atr_pct = atr_value / close * 100.0
    return MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT, round(atr_pct, 3), atr_value


def _distance_to_zone(price: float, zone_low: float, zone_high: float) -> float:
    if zone_low <= price <= zone_high:
        return 0.0
    if price < zone_low:
        return zone_low - price
    return price - zone_high


def _distance_from_ob(price: float, zone_low: float, zone_high: float, atr_value: float) -> tuple[float, float]:
    distance = _distance_to_zone(price, zone_low, zone_high)
    distance_pct = 0.0 if price <= 0 else distance / price * 100.0
    distance_atr = 0.0 if atr_value <= 0 else distance / atr_value
    return round(distance_pct, 3), round(distance_atr, 3)


def _too_far_from_ob(
    price: float,
    zone_low: float,
    zone_high: float,
    atr_value: float,
    max_pct: float,
    max_atr: float,
) -> tuple[bool, float, float]:
    distance_pct, distance_atr = _distance_from_ob(price, zone_low, zone_high, atr_value)
    pct_fail = max_pct > 0 and distance_pct > max_pct
    atr_fail = max_atr > 0 and atr_value > 0 and distance_atr > max_atr
    return pct_fail or atr_fail, distance_pct, distance_atr


def _ob_entry_quality_ok(row: pd.Series, direction: str, zone_low: float, zone_high: float) -> bool:
    if not OB_ENTRY_QUALITY_CHECK:
        return True

    midpoint = (zone_low + zone_high) / 2.0

    if direction == "LONG":
        return float(row["low"]) <= midpoint
    return float(row["high"]) >= midpoint


def _mss_break_ok(retest_row: pd.Series, candidate_row: pd.Series, direction: str) -> bool:
    if direction == "LONG":
        return (
            float(candidate_row["high"]) > float(retest_row["high"])
            and float(candidate_row["close"]) > float(retest_row["high"])
            and _is_bullish(candidate_row)
        )

    return (
        float(candidate_row["low"]) < float(retest_row["low"])
        and float(candidate_row["close"]) < float(retest_row["low"])
        and _is_bearish(candidate_row)
    )


def _trend_candle_confirmation_ok(symbol: str, direction: str) -> bool:
    if not REQUIRE_TREND_CANDLE_CONFIRMATION:
        return True

    try:
        df = get_klines(symbol, TREND_CONFIRM_TF, count=5)

        if df is None or df.empty or len(df) < 2:
            logger.warning("[TREND-CONFIRM] %s insufficient data — filter skipped", symbol)
            return True

        row = df.iloc[-2]

        if direction == "LONG":
            return _is_bullish(row)
        return _is_bearish(row)

    except Exception as e:
        logger.warning("[TREND-CONFIRM] %s fetch error: %s — filter skipped", symbol, e)
        return True


def _higher_tf_direction_ok(symbol: str, direction: str) -> bool:
    """
    Confirm 15m SMC setup with stronger 1H/4H direction.

    LONG:
        close > EMA200
        optional EMA50 > EMA200
        optional EMA200 slope rising

    SHORT:
        close < EMA200
        optional EMA50 < EMA200
        optional EMA200 slope falling
    """
    if not ENABLE_HTF_FILTER:
        return True

    for tf in HTF_CONFIRM_TFS:
        try:
            df = get_klines(symbol, tf, count=HTF_KLINE_COUNT)

            if df is None or df.empty or len(df) < max(HTF_EMA_FAST, HTF_EMA_SLOW) + HTF_EMA_SLOPE_LOOKBACK + 5:
                logger.info("[HTF-FILTER] %s %s skipped: insufficient %s data", symbol, direction, tf)
                return False

            completed = df.iloc[:-1].copy()
            close = float(completed["close"].astype(float).iloc[-1])
            ema_fast = _ema(completed["close"], HTF_EMA_FAST)
            ema_slow = _ema(completed["close"], HTF_EMA_SLOW)

            fast_now = float(ema_fast.iloc[-1])
            slow_now = float(ema_slow.iloc[-1])
            slow_prev = float(ema_slow.iloc[-1 - HTF_EMA_SLOPE_LOOKBACK])

            if direction == "LONG":
                close_ok = close > slow_now
                stack_ok = (fast_now > slow_now) if REQUIRE_HTF_EMA_STACK else True
                slope_ok = (slow_now > slow_prev) if REQUIRE_HTF_EMA_SLOPE else True
            else:
                close_ok = close < slow_now
                stack_ok = (fast_now < slow_now) if REQUIRE_HTF_EMA_STACK else True
                slope_ok = (slow_now < slow_prev) if REQUIRE_HTF_EMA_SLOPE else True

            if not (close_ok and stack_ok and slope_ok):
                logger.info(
                    "[HTF-FILTER] Reject %s %s | %s close=%.6g ema%d=%.6g ema%d=%.6g close_ok=%s stack_ok=%s slope_ok=%s",
                    symbol,
                    direction,
                    tf,
                    close,
                    HTF_EMA_FAST,
                    fast_now,
                    HTF_EMA_SLOW,
                    slow_now,
                    close_ok,
                    stack_ok,
                    slope_ok,
                )
                return False

        except Exception as e:
            logger.warning("[HTF-FILTER] %s %s %s error: %s", symbol, direction, tf, e)
            return False

    return True


# ── swings ────────────────────────────────────────────────────────

def _find_swings(df: pd.DataFrame, left: int, right: int) -> list[dict]:
    swings: list[dict] = []

    if len(df) < left + right + 5:
        return swings

    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    for i in range(left, len(df) - right):
        high = float(highs.iloc[i])
        low = float(lows.iloc[i])

        left_high = float(highs.iloc[i - left:i].max())
        right_high = float(highs.iloc[i + 1:i + right + 1].max())

        left_low = float(lows.iloc[i - left:i].min())
        right_low = float(lows.iloc[i + 1:i + right + 1].min())

        if high > left_high and high > right_high:
            swings.append({
                "type": "HIGH",
                "pos": i,
                "time": df.index[i],
                "price": high,
            })

        if low < left_low and low < right_low:
            swings.append({
                "type": "LOW",
                "pos": i,
                "time": df.index[i],
                "price": low,
            })

    swings.sort(key=lambda x: x["pos"])
    return swings


def _last_swing_before(swings: list[dict], swing_type: str, pos: int) -> dict | None:
    candidates = [
        s for s in swings
        if s["type"] == swing_type and s["pos"] < pos
    ]

    return candidates[-1] if candidates else None


def _find_target_swing(swings: list[dict], direction: str, pos: int, reference_price: float) -> dict | None:
    if direction == "LONG":
        candidates = [
            s for s in swings
            if s["type"] == "HIGH"
            and s["pos"] < pos
            and s["price"] > reference_price
        ]
        return candidates[-1] if candidates else None

    candidates = [
        s for s in swings
        if s["type"] == "LOW"
        and s["pos"] < pos
        and s["price"] < reference_price
    ]

    return candidates[-1] if candidates else None


# ── 15m bias ──────────────────────────────────────────────────────

def _get_market_structure_bias(trend_df: pd.DataFrame) -> tuple[str | None, dict]:
    completed = trend_df.iloc[:-1].copy()

    if len(completed) < STRUCTURE_LOOKBACK // 2:
        return None, {}

    recent = completed.tail(STRUCTURE_LOOKBACK).copy()
    swings = _find_swings(recent, SWING_LEFT, SWING_RIGHT)

    if len(swings) < 4:
        return None, {}

    last_bias = None
    last_break_price = None
    last_break_time = None

    for i in range(SWING_LEFT + SWING_RIGHT + 2, len(recent)):
        close = float(recent["close"].iloc[i])

        previous_high = _last_swing_before(swings, "HIGH", i)
        previous_low = _last_swing_before(swings, "LOW", i)

        if previous_high and close > previous_high["price"]:
            last_bias = "LONG"
            last_break_price = previous_high["price"]
            last_break_time = recent.index[i]

        if previous_low and close < previous_low["price"]:
            last_bias = "SHORT"
            last_break_price = previous_low["price"]
            last_break_time = recent.index[i]

    if not last_bias:
        return None, {}

    return last_bias, {
        "bias": last_bias,
        "break_price": last_break_price,
        "break_time": last_break_time,
        "swing_count": len(swings),
    }


# ── sweep / displacement / OB ─────────────────────────────────────

def _detect_sell_side_sweep(df: pd.DataFrame, swings: list[dict], pos: int) -> dict | None:
    prev_low = _last_swing_before(swings, "LOW", pos)

    if not prev_low:
        return None

    row = df.iloc[pos]
    low = float(row["low"])
    close = float(row["close"])

    if low < prev_low["price"] and close > prev_low["price"]:
        return {
            "type": "SELL_SIDE_SWEEP",
            "swing": prev_low,
            "pos": pos,
            "time": df.index[pos],
            "level": prev_low["price"],
            "extreme": low,
        }

    return None


def _detect_buy_side_sweep(df: pd.DataFrame, swings: list[dict], pos: int) -> dict | None:
    prev_high = _last_swing_before(swings, "HIGH", pos)

    if not prev_high:
        return None

    row = df.iloc[pos]
    high = float(row["high"])
    close = float(row["close"])

    if high > prev_high["price"] and close < prev_high["price"]:
        return {
            "type": "BUY_SIDE_SWEEP",
            "swing": prev_high,
            "pos": pos,
            "time": df.index[pos],
            "level": prev_high["price"],
            "extreme": high,
        }

    return None


def _is_bullish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)

    if avg_body <= 0:
        return False

    return (
        _is_bullish(row)
        and _body_size(row) >= avg_body * DISPLACEMENT_BODY_MULTIPLIER
        and _close_position(row) >= DISPLACEMENT_CLOSE_POSITION
    )


def _is_bearish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)

    if avg_body <= 0:
        return False

    return (
        _is_bearish(row)
        and _body_size(row) >= avg_body * DISPLACEMENT_BODY_MULTIPLIER
        and _close_position(row) <= (1.0 - DISPLACEMENT_CLOSE_POSITION)
    )


def _find_bullish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)

    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]

        if _is_bearish(row):
            zone_low = float(row["low"])
            zone_high = max(float(row["open"]), float(row["close"]))

            return {
                "type": "BULLISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": zone_low,
                "zone_high": zone_high,
            }

    return None


def _find_bearish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)

    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]

        if _is_bullish(row):
            zone_low = min(float(row["open"]), float(row["close"]))
            zone_high = float(row["high"])

            return {
                "type": "BEARISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": zone_low,
                "zone_high": zone_high,
            }

    return None


# ── price calculations ────────────────────────────────────────────

def _calculate_setup_prices(
    direction: str,
    ob: dict,
    sweep: dict,
    target_swing: dict | None,
) -> tuple[float, float, float] | None:
    """
    Returns:
        sl_price, target_price, rr_estimate

    RR estimate uses OB midpoint as assumed entry.
    Actual RR is recalculated on retest entry.
    """
    ob_mid = (ob["zone_low"] + ob["zone_high"]) / 2.0

    if ob_mid <= 0:
        return None

    if direction == "LONG":
        sl_price = min(sweep["extreme"], ob["zone_low"]) * (1.0 - SL_BUFFER_PCT / 100.0)

        if target_swing and target_swing["price"] > ob_mid:
            target_price = target_swing["price"] * (1.0 - TP_BUFFER_PCT / 100.0)
        else:
            target_price = ob_mid + (ob_mid - sl_price) * MIN_STRUCTURE_RR

        risk = ob_mid - sl_price
        reward = target_price - ob_mid

    else:
        sl_price = max(sweep["extreme"], ob["zone_high"]) * (1.0 + SL_BUFFER_PCT / 100.0)

        if target_swing and target_swing["price"] < ob_mid:
            target_price = target_swing["price"] * (1.0 + TP_BUFFER_PCT / 100.0)
        else:
            target_price = ob_mid - (sl_price - ob_mid) * MIN_STRUCTURE_RR

        risk = sl_price - ob_mid
        reward = ob_mid - target_price

    if risk <= 0 or reward <= 0:
        return None

    sl_pct = risk / ob_mid * 100.0

    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None

    rr = reward / risk

    if rr < MIN_STRUCTURE_RR:
        return None

    rr = min(rr, MAX_STRUCTURE_RR)

    return round(sl_price, 8), round(target_price, 8), round(rr, 2)


def _calculate_final_prices(
    direction: str,
    entry: float,
    setup: dict,
    atr_value: float = 0.0,
) -> tuple[float, float, float, float, float] | None:
    sl_price = float(setup["sl_price"])
    target_price = float(setup["target_price"])

    if entry <= 0:
        return None

    # ATR stop floor: avoid very tight stops that get hit by normal 5m noise.
    if atr_value > 0 and ATR_STOP_FLOOR_MULTIPLIER > 0:
        min_risk = atr_value * ATR_STOP_FLOOR_MULTIPLIER
        if direction == "LONG":
            if entry - sl_price < min_risk:
                sl_price = entry - min_risk
        else:
            if sl_price - entry < min_risk:
                sl_price = entry + min_risk

    if direction == "LONG":
        risk = entry - sl_price
        reward = target_price - entry
    else:
        risk = sl_price - entry
        reward = entry - target_price

    if risk <= 0 or reward <= 0:
        return None

    sl_pct = risk / entry * 100.0

    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None

    rr = reward / risk

    if rr < MIN_STRUCTURE_RR:
        return None

    if rr > MAX_STRUCTURE_RR:
        if direction == "LONG":
            target_price = entry + risk * MAX_STRUCTURE_RR
        else:
            target_price = entry - risk * MAX_STRUCTURE_RR

        reward = abs(target_price - entry)
        rr = reward / risk

    if direction == "LONG":
        tp_move_pct = (target_price - entry) / entry * 100.0
        sl_move_pct = (entry - sl_price) / entry * 100.0
    else:
        tp_move_pct = (entry - target_price) / entry * 100.0
        sl_move_pct = (sl_price - entry) / entry * 100.0

    return (
        round(target_price, 8),
        round(sl_price, 8),
        round(tp_move_pct * LEVERAGE, 1),
        round(sl_move_pct * LEVERAGE, 1),
        round(rr, 2),
    )


def _score_setup(rr: float, ob_age: int, sweep_age: int) -> float:
    score = 55.0

    if rr >= 3.0:
        score += 20.0
    elif rr >= 2.0:
        score += 15.0
    elif rr >= MIN_STRUCTURE_RR:
        score += 10.0

    if ob_age <= 8:
        score += 15.0
    elif ob_age <= 16:
        score += 10.0
    else:
        score += 5.0

    if sweep_age <= 12:
        score += 10.0
    elif sweep_age <= 24:
        score += 5.0

    return round(min(score, 100.0), 1)


# ── setup detection ───────────────────────────────────────────────

def detect_setup(symbol: str) -> dict | None:
    """
    Detects SMC setup and returns pending setup dict.

    This does NOT fire a signal.
    """
    try:
        trend_df = get_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        entry_df = get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if trend_df is None or trend_df.empty:
            return None

        if entry_df is None or entry_df.empty:
            return None

        bias, bias_details = _get_market_structure_bias(trend_df)

        if bias is None:
            return None

        if not _higher_tf_direction_ok(symbol, bias):
            return None

        completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)

        if len(completed) < 80:
            return None

        swings = _find_swings(completed, SWING_LEFT, SWING_RIGHT)

        if len(swings) < 4:
            return None

        start = max(AVG_BODY_PERIOD + SWEEP_LOOKBACK + ORDER_BLOCK_LOOKBACK, 30)
        last_possible = len(completed) - 3

        best_setup = None
        best_score = -1.0

        for displacement_pos in range(start, last_possible + 1):
            if bias == "LONG" and not _is_bullish_displacement(completed, displacement_pos):
                continue

            if bias == "SHORT" and not _is_bearish_displacement(completed, displacement_pos):
                continue

            sweep = None
            sweep_start = max(0, displacement_pos - SWEEP_LOOKBACK)

            for sweep_pos in range(displacement_pos - 1, sweep_start - 1, -1):
                if bias == "LONG":
                    sweep = _detect_sell_side_sweep(completed, swings, sweep_pos)
                else:
                    sweep = _detect_buy_side_sweep(completed, swings, sweep_pos)

                if sweep:
                    break

            if not sweep:
                continue

            if bias == "LONG":
                ob = _find_bullish_ob(completed, displacement_pos)
            else:
                ob = _find_bearish_ob(completed, displacement_pos)

            if not ob:
                continue

            target_swing = _find_target_swing(
                swings=swings,
                direction=bias,
                pos=displacement_pos,
                reference_price=(ob["zone_low"] + ob["zone_high"]) / 2.0,
            )

            prices = _calculate_setup_prices(
                direction=bias,
                ob=ob,
                sweep=sweep,
                target_swing=target_swing,
            )

            if not prices:
                continue

            sl_price, target_price, rr_estimate = prices

            ob_age = len(completed) - ob["pos"]
            sweep_age = len(completed) - sweep["pos"]
            score = _score_setup(rr_estimate, ob_age, sweep_age)

            if score <= best_score:
                continue

            setup_time = completed.index[displacement_pos]
            expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=PENDING_SETUP_EXPIRE_CANDLES * CANDLE_MINUTES
            )

            best_score = score
            best_setup = {
                "symbol": symbol,
                "direction": bias,
                "trend_tf": TREND_TF,
                "entry_tf": ENTRY_TF,
                "bias": bias,
                "bias_break": bias_details.get("break_price"),
                "sweep_type": sweep["type"],
                "sweep_level": sweep["level"],
                "sweep_extreme": sweep["extreme"],
                "sweep_time": sweep["time"].to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
                    if hasattr(sweep["time"], "to_pydatetime") else str(sweep["time"]),
                "ob_type": ob["type"],
                "ob_low": ob["zone_low"],
                "ob_high": ob["zone_high"],
                "ob_time": ob["time"].to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
                    if hasattr(ob["time"], "to_pydatetime") else str(ob["time"]),
                "target_price": target_price,
                "sl_price": sl_price,
                "rr_estimate": rr_estimate,
                "score": score,
                "setup_time": setup_time.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
                    if hasattr(setup_time, "to_pydatetime") else str(setup_time),
                "expires_at": expires_at.isoformat(),
            }

        if best_setup:
            logger.info(
                f"[SETUP] {best_setup['direction']} {symbol} | "
                f"OB={best_setup['ob_low']:.6g}-{best_setup['ob_high']:.6g} "
                f"SL={best_setup['sl_price']:.6g} "
                f"TP={best_setup['target_price']:.6g} "
                f"RR~{best_setup['rr_estimate']:.2f} "
                f"score={best_setup['score']}"
            )

        return best_setup

    except Exception as e:
        logger.error(f"Error detecting setup for {symbol}: {e}", exc_info=True)
        return None


# ── pending setup monitoring ──────────────────────────────────────

def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    """
    Returns:
        ("WAIT", None)
        ("EXPIRED", None)
        ("INVALIDATED", None)
        ("FIRE", Signal)

    Entry is intentionally stricter than the old version:
    OB touch + rejection candle + optional MSS/break confirmation.
    """
    try:
        now = datetime.now(timezone.utc)
        expires_at = _parse_utc(setup["expires_at"])

        if now >= expires_at:
            return "EXPIRED", None

        symbol = setup["symbol"]
        direction = setup["direction"]
        zone_low = float(setup["ob_low"])
        zone_high = float(setup["ob_high"])
        sl_price = float(setup["sl_price"])

        df = get_klines(symbol, ENTRY_TF, count=MONITOR_KLINE_COUNT)

        if df is None or df.empty or len(df) < 6:
            _debug_wait(symbol, "not enough monitor candles")
            return "WAIT", None

        completed = df.iloc[:-1].copy()
        last_close = float(completed["close"].astype(float).iloc[-1])
        _, monitor_atr_pct, monitor_atr_value = _atr_status(completed)

        too_far, distance_pct, distance_atr = _too_far_from_ob(
            last_close,
            zone_low,
            zone_high,
            monitor_atr_value,
            EXPIRE_IF_PRICE_AWAY_PCT,
            EXPIRE_IF_PRICE_AWAY_ATR,
        )
        if too_far:
            logger.info(
                "[SETUP-EXPIRE] %s %s | price moved too far from OB close=%.6g distance=%.2f%% %.2fATR limit=%.2f%% %.2fATR",
                symbol,
                direction,
                last_close,
                distance_pct,
                distance_atr,
                EXPIRE_IF_PRICE_AWAY_PCT,
                EXPIRE_IF_PRICE_AWAY_ATR,
            )
            return "EXPIRED", None

        lookback = max(6, MSS_BREAK_LOOKBACK_CANDLES + 4)
        recent = completed.tail(lookback)
        recent_rows = list(recent.iterrows())

        # Invalidation before entry.
        for _, row in recent_rows:
            if direction == "LONG" and float(row["low"]) <= sl_price:
                logger.info("[SETUP-INVALID] %s LONG | low %.6g <= SL %.6g before entry", symbol, float(row["low"]), sl_price)
                return "INVALIDATED", None

            if direction == "SHORT" and float(row["high"]) >= sl_price:
                logger.info("[SETUP-INVALID] %s SHORT | high %.6g >= SL %.6g before entry", symbol, float(row["high"]), sl_price)
                return "INVALIDATED", None

        midpoint = (zone_low + zone_high) / 2.0
        touched_zone = False
        found_retest_without_break = False

        for idx, (ts, row) in enumerate(recent_rows):
            if not _touches_zone(row, zone_low, zone_high):
                continue

            touched_zone = True

            body_pct = _body_pct(row)
            if body_pct > MAX_SIGNAL_CANDLE_BODY_PCT:
                _debug_wait(symbol, f"touched OB but candle body {body_pct:.2f}% > max {MAX_SIGNAL_CANDLE_BODY_PCT:.2f}%")
                continue

            if not _ob_entry_quality_ok(row, direction, zone_low, zone_high):
                _debug_wait(symbol, "touched OB but not in preferred OB half")
                continue

            close = float(row["close"])
            if direction == "LONG":
                valid_retest = _is_bullish(row) and close > midpoint
            else:
                valid_retest = _is_bearish(row) and close < midpoint

            if not valid_retest:
                _debug_wait(symbol, f"touched OB but rejection candle failed close={close:.6g} midpoint={midpoint:.6g}")
                continue

            found_retest_without_break = True
            break_row = row

            if REQUIRE_MSS_BREAK_ENTRY:
                break_row = None
                max_j = min(len(recent_rows), idx + 1 + MSS_BREAK_LOOKBACK_CANDLES)

                for j in range(idx + 1, max_j):
                    _, candidate_row = recent_rows[j]
                    if _mss_break_ok(row, candidate_row, direction):
                        break_row = candidate_row
                        break

                if break_row is None:
                    _debug_wait(symbol, f"valid OB rejection but waiting for MSS break within {MSS_BREAK_LOOKBACK_CANDLES} candle(s)")
                    continue

            if REVALIDATE_BEFORE_FIRE:
                atr_ok_now, atr_pct_now, _ = _atr_status(completed)
                if not atr_ok_now:
                    _debug_wait(symbol, f"revalidation failed: ATR {atr_pct_now:.2f}% outside range")
                    return "WAIT", None

                if not _trend_candle_confirmation_ok(symbol, direction):
                    _debug_wait(symbol, f"revalidation failed: {TREND_CONFIRM_TF} candle not aligned")
                    return "WAIT", None

            entry = float(break_row["close"])

            prices = _calculate_final_prices(
                direction=direction,
                entry=entry,
                setup=setup,
                atr_value=monitor_atr_value,
            )

            if not prices:
                _debug_wait(symbol, "confirmed retest/MSS but final RR/SL validation failed")
                return "WAIT", None

            tp_price, final_sl_price, tp_roi_pct, sl_roi_pct, rr = prices

            base_score = float(setup["score"])
            score = min(base_score + 5.0, 100.0)

            logger.info(
                f"[ENTRY] {direction} {symbol} @ {entry:.6g} | "
                f"OB={zone_low:.6g}-{zone_high:.6g} "
                f"TP={tp_price:.6g} SL={final_sl_price:.6g} "
                f"RR={rr:.2f} score={score} mss={'on' if REQUIRE_MSS_BREAK_ENTRY else 'off'}"
            )

            trigger_note = "MSS break" if REQUIRE_MSS_BREAK_ENTRY else "OB rejection"

            return "FIRE", Signal(
                symbol=symbol,
                direction=direction,
                entry_price=round(entry, 8),
                tp_price=tp_price,
                sl_price=final_sl_price,
                leverage=LEVERAGE,
                tp_roi_pct=tp_roi_pct,
                sl_roi_pct=sl_roi_pct,
                timeframe_summary=(
                    f"Stateful SMC {ENTRY_TF} | {TREND_TF} bias {setup['bias']} + HTF filter | "
                    f"{setup['sweep_type']} + {setup['ob_type']} retest + {trigger_note} | RR {rr:g}"
                ),
                generated_at=datetime.now(timezone.utc),
                score=score,
            )

        if not touched_zone:
            _debug_wait(symbol, f"price not in OB yet close={last_close:.6g} OB={zone_low:.6g}-{zone_high:.6g} distance={distance_pct:.2f}%/{distance_atr:.2f}ATR")
        elif found_retest_without_break:
            _debug_wait(symbol, "OB rejection found but no MSS break entry yet")
        else:
            _debug_wait(symbol, "touched OB but no valid retest/rejection condition matched")

        return "WAIT", None

    except Exception as e:
        logger.error(f"Error evaluating pending setup {setup.get('id')}: {e}", exc_info=True)
        return "WAIT", None


# Compatibility wrapper. Main.py does not need this in the new flow.
def analyze_coin(symbol: str) -> "Signal | None":
    return None