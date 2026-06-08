"""
Hybrid SMC Pro — 1h Trend + 15m Structure + 5m Sweep/OB Retest.

This file intentionally disables the old EMA/CCI direct signal path.
Signals now fire only through this stateful flow:

    detect_setup(symbol)
        1. Detect 15m market-structure bias.
        2. Confirm 1h trend filter.
        3. Confirm BTC market regime is not against the setup.
        4. Detect 5m liquidity sweep + displacement candle.
        5. Find the order block and calculate SL/TP.
        6. Return a pending setup dict for database.save_pending_setup().

    evaluate_pending_setup(setup)
        1. Wait for price to retest the OB zone.
        2. Require confirmation candle after retest.
        3. Recalculate real entry, SL, TP and ROI.
        4. Return Signal only when entry confirms.

No external TA package is used. Indicators are calculated with pandas only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd

from mexc_client import get_klines
from config import (
    TREND_TF,
    ENTRY_TF,
    HTF_TREND_TF,
    TREND_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    MONITOR_KLINE_COUNT,
    HTF_KLINE_COUNT,
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
    ENABLE_HTF_FILTER,
    HTF_EMA_FAST,
    HTF_EMA_SLOW,
    HTF_EMA_SLOPE_LOOKBACK,
    ENABLE_ENTRY_EMA_FILTER,
    EMA_FAST_FILTER,
    EMA_SLOW_FILTER,
    ENABLE_ATR_FILTER,
    ATR_PERIOD,
    MIN_ATR_PCT,
    MAX_ATR_PCT,
    ATR_SL_MULTIPLIER,
    ENABLE_VOLUME_FILTER,
    VOLUME_LOOKBACK,
    MIN_VOLUME_MULTIPLIER,
    ENABLE_BTC_FILTER,
    BTC_SYMBOL,
    BTC_TF,
    BTC_EMA_PERIOD,
    BTC_KLINE_COUNT,
    MIN_STRUCTURE_RR,
    MAX_STRUCTURE_RR,
    REWARD_RATIO,
    SL_BUFFER_PCT,
    TP_BUFFER_PCT,
    MIN_SL_PCT,
    MAX_SL_PCT,
    MIN_SETUP_SCORE,
    LEVERAGE,
    CANDLE_MINUTES,
    SETUP_MONITOR_LOG_DETAILS,
    EXPIRE_IF_PRICE_AWAY_ATR,
    EXPIRE_IF_PRICE_AWAY_PCT,
    REVALIDATE_BEFORE_FIRE,
    OB_ENTRY_QUALITY_CHECK,
    REQUIRE_MSS_BREAK_ENTRY,
    MSS_BREAK_LOOKBACK_CANDLES,
    MSS_BREAK_BUFFER_PCT,
    ENABLE_ATR_STOP_FLOOR,
    ATR_STOP_FLOOR_MULTIPLIER,
    REQUIRE_TREND_CANDLE_CONFIRMATION,
    TREND_CONFIRM_TF,
)

logger = logging.getLogger(__name__)

_BTC_REGIME_CACHE: dict[str, object] = {
    "expires_at": datetime.min.replace(tzinfo=timezone.utc),
    "strongly_bullish": False,
    "strongly_bearish": False,
}


def _debug_wait(symbol: str, message: str) -> None:
    if SETUP_MONITOR_LOG_DETAILS:
        logger.info("[SETUP-WAIT] %s | %s", symbol, message)


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
    score: float = 0.0


# ── indicator helpers ─────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _ema_slope(series: pd.Series, lookback: int = 3) -> float:
    if len(series) < lookback + 1:
        return 0.0
    return float(series.iloc[-1]) - float(series.iloc[-1 - lookback])


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean()


def _atr_pct(df: pd.DataFrame) -> tuple[bool, float, float]:
    """Returns (ok, atr_pct, raw_atr)."""
    if not ENABLE_ATR_FILTER:
        return True, 0.0, 0.0

    if len(df) < ATR_PERIOD + 5:
        return True, 0.0, 0.0

    atr_value = float(_atr(df, ATR_PERIOD).iloc[-1])
    close = float(df["close"].astype(float).iloc[-1])

    if close <= 0:
        return True, 0.0, atr_value

    pct = atr_value / close * 100.0
    return MIN_ATR_PCT <= pct <= MAX_ATR_PCT, round(pct, 3), atr_value


def _atr_status(df: pd.DataFrame) -> tuple[bool, float, float]:
    """Compatibility wrapper used by monitor logic."""
    return _atr_pct(df)


def _touches_zone(row: pd.Series, zone_low: float, zone_high: float) -> bool:
    high = float(row["high"])
    low = float(row["low"])
    return low <= zone_high and high >= zone_low


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
    buffer_pct = MSS_BREAK_BUFFER_PCT / 100.0

    if direction == "LONG":
        trigger = float(retest_row["high"]) * (1.0 + buffer_pct)
        return (
            float(candidate_row["high"]) > trigger
            and float(candidate_row["close"]) > trigger
            and _is_bullish(candidate_row)
        )

    trigger = float(retest_row["low"]) * (1.0 - buffer_pct)
    return (
        float(candidate_row["low"]) < trigger
        and float(candidate_row["close"]) < trigger
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
        return _is_bullish(row) if direction == "LONG" else _is_bearish(row)

    except Exception as e:
        logger.warning("[TREND-CONFIRM] %s fetch error: %s — filter skipped", symbol, e)
        return True


# ── candle helpers ────────────────────────────────────────────────

def _body_size(row: pd.Series) -> float:
    return abs(float(row["close"]) - float(row["open"]))


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])
    return 0.0 if close <= 0 else _body_size(row) / close * 100.0


def _is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


def _close_position(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    rng = high - low
    return 0.5 if rng <= 0 else (close - low) / rng


def _avg_body(df: pd.DataFrame, pos: int, period: int) -> float:
    start = max(0, pos - period)
    subset = df.iloc[start:pos]
    if subset.empty:
        return 0.0
    return float((subset["close"].astype(float) - subset["open"].astype(float)).abs().mean())


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(ts) -> str:
    if hasattr(ts, "to_pydatetime"):
        dt = ts.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(ts)


# ── swing / structure helpers ─────────────────────────────────────

def _find_swings(df: pd.DataFrame, left: int, right: int) -> list[dict]:
    swings: list[dict] = []
    if len(df) < left + right + 5:
        return swings

    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    for i in range(left, len(df) - right):
        high = float(highs.iloc[i])
        low = float(lows.iloc[i])

        if high > float(highs.iloc[i - left:i].max()) and high > float(highs.iloc[i + 1:i + right + 1].max()):
            swings.append({"type": "HIGH", "pos": i, "time": df.index[i], "price": high})

        if low < float(lows.iloc[i - left:i].min()) and low < float(lows.iloc[i + 1:i + right + 1].min()):
            swings.append({"type": "LOW", "pos": i, "time": df.index[i], "price": low})

    swings.sort(key=lambda x: x["pos"])
    return swings


def _last_swing_before(swings: list[dict], swing_type: str, pos: int) -> dict | None:
    candidates = [s for s in swings if s["type"] == swing_type and s["pos"] < pos]
    return candidates[-1] if candidates else None


def _find_target_swing(swings: list[dict], direction: str, pos: int, reference_price: float) -> dict | None:
    if direction == "LONG":
        candidates = [s for s in swings if s["type"] == "HIGH" and s["pos"] < pos and s["price"] > reference_price]
    else:
        candidates = [s for s in swings if s["type"] == "LOW" and s["pos"] < pos and s["price"] < reference_price]
    return candidates[-1] if candidates else None


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


# ── filters ───────────────────────────────────────────────────────

def _htf_trend_ok(symbol: str, direction: str) -> tuple[bool, bool]:
    """Returns (allowed, strong_agreement)."""
    if not ENABLE_HTF_FILTER:
        return True, False

    try:
        df = get_klines(symbol, HTF_TREND_TF, count=HTF_KLINE_COUNT)
        if df is None or df.empty or len(df) < HTF_EMA_SLOW + 5:
            logger.warning("[HTF] %s insufficient data — filter skipped", symbol)
            return True, False

        close = df["close"].astype(float)
        ema_fast = _ema(close, HTF_EMA_FAST)
        ema_slow = _ema(close, HTF_EMA_SLOW)

        last_close = float(close.iloc[-1])
        last_fast = float(ema_fast.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])
        slope = _ema_slope(ema_fast, HTF_EMA_SLOPE_LOOKBACK)

        if direction == "LONG":
            strong = last_fast > last_slow
            allowed = strong or (last_close > last_slow and slope > 0)
        else:
            strong = last_fast < last_slow
            allowed = strong or (last_close < last_slow and slope < 0)

        return allowed, strong

    except Exception as e:
        logger.warning("[HTF] %s fetch error: %s — filter skipped", symbol, e)
        return True, False


def _entry_ema_ok(df: pd.DataFrame, direction: str) -> bool:
    if not ENABLE_ENTRY_EMA_FILTER:
        return True

    if len(df) < EMA_SLOW_FILTER + 5:
        return True

    close = df["close"].astype(float)
    ema_fast = _ema(close, EMA_FAST_FILTER)
    ema_slow = _ema(close, EMA_SLOW_FILTER)

    last_close = float(close.iloc[-1])
    last_fast = float(ema_fast.iloc[-1])
    last_slow = float(ema_slow.iloc[-1])

    if direction == "LONG":
        return last_close > last_fast and last_fast >= last_slow
    return last_close < last_fast and last_fast <= last_slow


def _volume_ok(df: pd.DataFrame, pos: int) -> bool:
    if not ENABLE_VOLUME_FILTER:
        return True

    try:
        volume = df["volume"].astype(float)
        if pos <= 0 or float(volume.iloc[pos]) <= 0:
            return True

        start = max(0, pos - VOLUME_LOOKBACK)
        avg = float(volume.iloc[start:pos].mean())
        if avg <= 0:
            return True

        return float(volume.iloc[pos]) >= avg * MIN_VOLUME_MULTIPLIER
    except Exception:
        return True


def _btc_regime_ok(direction: str) -> bool:
    """
    BTC market-regime guard with short in-process cache.

    Without this cache, one full scan may fetch BTC candles once per coin,
    which can slow the scheduler and cause missed outcome-check runs.
    """
    if not ENABLE_BTC_FILTER:
        return True

    now = datetime.now(timezone.utc)

    try:
        if now >= _BTC_REGIME_CACHE["expires_at"]:
            df = get_klines(BTC_SYMBOL, BTC_TF, count=BTC_KLINE_COUNT)

            if df is None or df.empty or len(df) < BTC_EMA_PERIOD + 5:
                logger.warning("[BTC-REGIME] insufficient data — filter skipped")
                _BTC_REGIME_CACHE.update({
                    "expires_at": now + timedelta(seconds=60),
                    "strongly_bullish": False,
                    "strongly_bearish": False,
                })
            else:
                close = df["close"].astype(float)
                ema = _ema(close, BTC_EMA_PERIOD)
                last_close = float(close.iloc[-1])
                last_ema = float(ema.iloc[-1])
                slope = _ema_slope(ema)

                strongly_bullish = last_close > last_ema and slope > 0
                strongly_bearish = last_close < last_ema and slope < 0

                _BTC_REGIME_CACHE.update({
                    "expires_at": now + timedelta(seconds=60),
                    "strongly_bullish": strongly_bullish,
                    "strongly_bearish": strongly_bearish,
                })

        if direction == "LONG" and bool(_BTC_REGIME_CACHE["strongly_bearish"]):
            return False
        if direction == "SHORT" and bool(_BTC_REGIME_CACHE["strongly_bullish"]):
            return False

        return True

    except Exception as e:
        logger.warning("[BTC-REGIME] fetch error: %s — filter skipped", e)
        return True

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
    avg = _avg_body(df, pos, AVG_BODY_PERIOD)
    return avg > 0 and _is_bullish(row) and _body_size(row) >= avg * DISPLACEMENT_BODY_MULTIPLIER and _close_position(row) >= DISPLACEMENT_CLOSE_POSITION


def _is_bearish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg = _avg_body(df, pos, AVG_BODY_PERIOD)
    return avg > 0 and _is_bearish(row) and _body_size(row) >= avg * DISPLACEMENT_BODY_MULTIPLIER and _close_position(row) <= (1.0 - DISPLACEMENT_CLOSE_POSITION)


def _find_bullish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)
    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]
        if _is_bearish(row):
            return {
                "type": "BULLISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": float(row["low"]),
                "zone_high": max(float(row["open"]), float(row["close"])),
            }
    return None


def _find_bearish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)
    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]
        if _is_bullish(row):
            return {
                "type": "BEARISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": min(float(row["open"]), float(row["close"])),
                "zone_high": float(row["high"]),
            }
    return None


def _calculate_setup_prices(direction: str, ob: dict, sweep: dict, target_swing: dict | None, atr_value: float) -> tuple[float, float, float, float] | None:
    ob_mid = (ob["zone_low"] + ob["zone_high"]) / 2.0
    if ob_mid <= 0:
        return None

    atr_buffer = atr_value * ATR_SL_MULTIPLIER if ENABLE_ATR_FILTER else 0.0

    if direction == "LONG":
        sl_price = min(sweep["extreme"], ob["zone_low"]) * (1.0 - SL_BUFFER_PCT / 100.0) - atr_buffer
        if target_swing and target_swing["price"] > ob_mid:
            target_price = target_swing["price"] * (1.0 - TP_BUFFER_PCT / 100.0)
        else:
            target_price = ob_mid + (ob_mid - sl_price) * REWARD_RATIO
        risk = ob_mid - sl_price
        reward = target_price - ob_mid
    else:
        sl_price = max(sweep["extreme"], ob["zone_high"]) * (1.0 + SL_BUFFER_PCT / 100.0) + atr_buffer
        if target_swing and target_swing["price"] < ob_mid:
            target_price = target_swing["price"] * (1.0 + TP_BUFFER_PCT / 100.0)
        else:
            target_price = ob_mid - (sl_price - ob_mid) * REWARD_RATIO
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

    if rr > MAX_STRUCTURE_RR:
        rr = MAX_STRUCTURE_RR
        if direction == "LONG":
            target_price = ob_mid + risk * rr
        else:
            target_price = ob_mid - risk * rr

    return round(sl_price, 8), round(target_price, 8), round(rr, 2), round(sl_pct, 3)


def _calculate_final_prices(direction: str, entry: float, setup: dict, atr_value: float = 0.0) -> tuple[float, float, float, float, float] | None:
    sl_price = float(setup["sl_price"])
    target_price = float(setup["target_price"])

    if entry <= 0:
        return None

    # Optional ATR stop floor prevents very tight structure SL from normal noise.
    if ENABLE_ATR_STOP_FLOOR and atr_value > 0 and ATR_STOP_FLOOR_MULTIPLIER > 0:
        min_risk = atr_value * ATR_STOP_FLOOR_MULTIPLIER
        if direction == "LONG" and entry - sl_price < min_risk:
            sl_price = entry - min_risk
        elif direction == "SHORT" and sl_price - entry < min_risk:
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
        rr = MAX_STRUCTURE_RR
        if direction == "LONG":
            target_price = entry + risk * rr
        else:
            target_price = entry - risk * rr

    if direction == "LONG":
        tp_move_pct = (target_price - entry) / entry * 100.0
    else:
        tp_move_pct = (entry - target_price) / entry * 100.0

    return (
        round(target_price, 8),
        round(sl_price, 8),
        round(tp_move_pct * LEVERAGE, 1),
        round(sl_pct * LEVERAGE, 1),
        round(rr, 2),
    )


def _score_setup(rr: float, ob_age: int, sweep_age: int, htf_strong: bool, atr_ok: bool, volume_ok: bool, ema_ok: bool) -> float:
    score = 45.0

    if rr >= 3.0:
        score += 20.0
    elif rr >= 2.0:
        score += 15.0
    elif rr >= MIN_STRUCTURE_RR:
        score += 10.0

    if ob_age <= 8:
        score += 10.0
    elif ob_age <= 16:
        score += 6.0
    else:
        score += 3.0

    if sweep_age <= 12:
        score += 8.0
    elif sweep_age <= 24:
        score += 4.0

    if htf_strong:
        score += 10.0
    if atr_ok and ENABLE_ATR_FILTER:
        score += 7.0
    if volume_ok and ENABLE_VOLUME_FILTER:
        score += 7.0
    if ema_ok and ENABLE_ENTRY_EMA_FILTER:
        score += 5.0

    return round(min(score, 100.0), 1)


# ── public setup detection ────────────────────────────────────────

def detect_setup(symbol: str) -> dict | None:
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

        htf_ok, htf_strong = _htf_trend_ok(symbol, bias)
        if not htf_ok:
            logger.info("[SETUP-REJECT] %s | HTF trend mismatch", symbol)
            return None

        if not _btc_regime_ok(bias):
            logger.info("[SETUP-REJECT] %s | BTC filter conflict", symbol)
            return None

        completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)
        if len(completed) < 80:
            return None

        atr_ok, atr_value_pct, atr_value = _atr_pct(completed)
        if not atr_ok:
            logger.info("[SETUP-REJECT] %s | ATR %.2f%% outside %.2f-%.2f", symbol, atr_value_pct, MIN_ATR_PCT, MAX_ATR_PCT)
            return None

        ema_ok = _entry_ema_ok(completed, bias)
        if not ema_ok:
            logger.info("[SETUP-REJECT] %s | entry EMA misaligned", symbol)
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

            vol_ok = _volume_ok(completed, displacement_pos)
            if not vol_ok:
                logger.info("[SETUP-REJECT] %s | volume weak", symbol)
                continue

            sweep = None
            sweep_start = max(0, displacement_pos - SWEEP_LOOKBACK)
            for sweep_pos in range(displacement_pos - 1, sweep_start - 1, -1):
                sweep = _detect_sell_side_sweep(completed, swings, sweep_pos) if bias == "LONG" else _detect_buy_side_sweep(completed, swings, sweep_pos)
                if sweep:
                    break

            if not sweep:
                continue

            ob = _find_bullish_ob(completed, displacement_pos) if bias == "LONG" else _find_bearish_ob(completed, displacement_pos)
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
                atr_value=atr_value,
            )
            if not prices:
                continue

            sl_price, target_price, rr_estimate, sl_pct = prices
            ob_age = len(completed) - ob["pos"]
            sweep_age = len(completed) - sweep["pos"]
            score = _score_setup(rr_estimate, ob_age, sweep_age, htf_strong, atr_ok, vol_ok, ema_ok)

            if score < MIN_SETUP_SCORE:
                logger.info("[SETUP-REJECT] %s | score %.1f < min %d", symbol, score, MIN_SETUP_SCORE)
                continue

            if score <= best_score:
                continue

            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=PENDING_SETUP_EXPIRE_CANDLES * CANDLE_MINUTES)

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
                "sweep_time": _iso(sweep["time"]),
                "ob_type": ob["type"],
                "ob_low": ob["zone_low"],
                "ob_high": ob["zone_high"],
                "ob_time": _iso(ob["time"]),
                "target_price": target_price,
                "sl_price": sl_price,
                "rr_estimate": rr_estimate,
                "score": score,
                "setup_time": _iso(completed.index[displacement_pos]),
                "expires_at": expires_at.isoformat(),
            }

            logger.info(
                "[SETUP] %s %s | OB=%.6g-%.6g SL=%.6g TP=%.6g SL%%=%.2f ATR%%=%.2f RR=%.2f score=%.1f",
                bias,
                symbol,
                ob["zone_low"],
                ob["zone_high"],
                sl_price,
                target_price,
                sl_pct,
                atr_value_pct,
                rr_estimate,
                score,
            )

        return best_setup

    except Exception as e:
        logger.error("Error detecting setup for %s: %s", symbol, e, exc_info=True)
        return None


# ── public pending setup monitoring ───────────────────────────────

def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    """
    Called by main.py for each waiting setup.

    Returns:
        ("WAIT", None)         — setup is still valid but no confirmed entry yet
        ("EXPIRED", None)      — setup is stale or price moved too far away
        ("INVALIDATED", None)  — SL level was touched before entry
        ("FIRE", Signal)       — confirmed OB retest + MSS break entry
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

        # Use enough candles to support retest candle + following MSS break candles.
        lookback = max(6, MSS_BREAK_LOOKBACK_CANDLES + 4)
        recent = completed.tail(lookback).copy()
        recent_rows = list(recent.iterrows())

        # Invalidation before entry.
        for _, row in recent_rows:
            if direction == "LONG" and float(row["low"]) <= sl_price:
                logger.info(
                    "[SETUP-INVALID] %s LONG | low %.6g <= SL %.6g before entry",
                    symbol,
                    float(row["low"]),
                    sl_price,
                )
                return "INVALIDATED", None

            if direction == "SHORT" and float(row["high"]) >= sl_price:
                logger.info(
                    "[SETUP-INVALID] %s SHORT | high %.6g >= SL %.6g before entry",
                    symbol,
                    float(row["high"]),
                    sl_price,
                )
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
                _debug_wait(
                    symbol,
                    f"touched OB but candle body {body_pct:.2f}% > max {MAX_SIGNAL_CANDLE_BODY_PCT:.2f}%",
                )
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
                _debug_wait(
                    symbol,
                    f"touched OB but rejection candle failed close={close:.6g} midpoint={midpoint:.6g}",
                )
                continue

            found_retest_without_break = True

            # Confirmation-break / MSS style trigger:
            # After the rejection candle, wait for a following completed candle to break
            # the rejection candle high/low. This prevents immediate fake entries.
            break_row = row
            break_ts = ts
            if REQUIRE_MSS_BREAK_ENTRY:
                break_row = None
                max_j = min(len(recent_rows), idx + 1 + MSS_BREAK_LOOKBACK_CANDLES)
                for j in range(idx + 1, max_j):
                    candidate_ts, candidate_row = recent_rows[j]
                    if _mss_break_ok(row, candidate_row, direction):
                        break_ts = candidate_ts
                        break_row = candidate_row
                        break

                if break_row is None:
                    _debug_wait(
                        symbol,
                        f"valid OB rejection but waiting for MSS break within {MSS_BREAK_LOOKBACK_CANDLES} candle(s)",
                    )
                    continue

            if REVALIDATE_BEFORE_FIRE:
                htf_ok, _ = _htf_trend_ok(symbol, direction)
                if not htf_ok:
                    _debug_wait(symbol, "revalidation failed: HTF trend mismatch")
                    return "WAIT", None

                if not _btc_regime_ok(direction):
                    _debug_wait(symbol, "revalidation failed: BTC filter conflict")
                    return "WAIT", None

                atr_ok_now, atr_pct_now, _ = _atr_status(completed)
                if not atr_ok_now:
                    _debug_wait(symbol, f"revalidation failed: ATR {atr_pct_now:.2f}% outside range")
                    return "WAIT", None

                if not _entry_ema_ok(completed, direction):
                    _debug_wait(symbol, "revalidation failed: entry EMA misaligned")
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
            score = min(float(setup["score"]) + 5.0, 100.0)

            logger.info(
                "[ENTRY] %s %s @ %.6g | OB=%.6g-%.6g TP=%.6g SL=%.6g RR=%.2f score=%.1f mss=%s",
                direction,
                symbol,
                entry,
                zone_low,
                zone_high,
                tp_price,
                final_sl_price,
                rr,
                score,
                "on" if REQUIRE_MSS_BREAK_ENTRY else "off",
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
                    f"Hybrid SMC Pro | {TREND_TF} bias + {ENTRY_TF} OB retest + {trigger_note} | "
                    f"{setup['sweep_type']} + {setup['ob_type']} | RR {rr:g}"
                ),
                generated_at=datetime.now(timezone.utc),
                score=score,
            )

        if not touched_zone:
            _debug_wait(
                symbol,
                f"price not in OB yet close={last_close:.6g} OB={zone_low:.6g}-{zone_high:.6g} distance={distance_pct:.2f}%/{distance_atr:.2f}ATR",
            )
        elif found_retest_without_break:
            _debug_wait(symbol, "OB rejection found but no MSS break entry yet")
        else:
            _debug_wait(symbol, "touched OB but no valid retest/rejection condition matched")

        return "WAIT", None

    except Exception as e:
        logger.error("Error evaluating pending setup %s: %s", setup.get("id"), e, exc_info=True)
        return "WAIT", None


# ── legacy direct signal path intentionally disabled ──────────────

def analyze_coin(symbol: str) -> Signal | None:
    """
    Disabled on purpose.

    The previous EMA/CCI direct strategy caused immediate entries that often hit SL.
    main.py should call detect_setup() + evaluate_pending_setup() instead.
    Keeping this wrapper prevents old imports from crashing, but it must not fire signals.
    """
    return None
