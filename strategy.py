"""
MTF SMC — 1D Macro + 4H Trend + 1H Structure + 15m Entry (Sweep/OB Retest).

Signal flow:

    detect_setup(symbol)
        1. ATR cooldown gate (skip symbols that recently failed ATR).
        2. 1H market-structure bias (swing-based).
        3. 1D macro regime filter (EMA50/200).
        4. 4H trend filter (EMA50/200).
        5. MTF alignment gate — all three must agree (elite: strict).
        6. BTC 1H quick regime cross-check.
        7. 15m entry analysis: ATR, EMA, sweeps, OB, displacement.
        8. OB distance filter — reject if price already too far from OB.
        9. Freshness gates — reject stale sweep/OB/displacement.
       10. Score >= MIN_SETUP_SCORE (95 in elite mode).
       11. Market regime gate — block counter-regime shorts/longs.
       12. Return pending setup dict with setup_key fingerprint.

    evaluate_pending_setup(setup)
        1. Wait for 15m price to enter OB zone.
        2. Rejection candle required.
        3. MSS break within lookback window.
        4. Entry volume confirmation (elite mode).
        5. Revalidate 4H trend + BTC + ATR + entry EMA.
        6. Return FIRE + Signal when entry confirms.

No external TA package. All indicators use pandas only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd

from mexc_client import get_klines
from config import (
    QUALITY_MODE,
    MACRO_TF,
    HTF_TREND_TF,
    STRUCTURE_TF,
    ENTRY_TF,
    TREND_TF,             # backward compat alias = STRUCTURE_TF
    MACRO_KLINE_COUNT,
    HTF_KLINE_COUNT,
    STRUCTURE_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    TREND_KLINE_COUNT,    # backward compat alias
    MONITOR_KLINE_COUNT,
    SWING_LEFT,
    SWING_RIGHT,
    STRUCTURE_LOOKBACK,
    ENTRY_LOOKBACK,
    SWEEP_LOOKBACK,
    AVG_BODY_PERIOD,
    DISPLACEMENT_BODY_MULTIPLIER,
    DISPLACEMENT_CLOSE_POSITION,
    MAX_DISPLACEMENT_AGE_CANDLES,
    MAX_SWEEP_AGE_CANDLES,
    MAX_OB_AGE_CANDLES,
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
    ATR_REJECT_COOLDOWN_MINUTES,
    ATR_REJECT_COOLDOWN_MARGIN_PCT,
    ENABLE_VOLUME_FILTER,
    VOLUME_LOOKBACK,
    MIN_VOLUME_MULTIPLIER,
    ENABLE_ENTRY_VOLUME_CONFIRMATION,
    ENTRY_VOLUME_LOOKBACK,
    ENTRY_MIN_VOLUME_MULTIPLIER,
    ENABLE_BTC_FILTER,
    BTC_SYMBOL,
    BTC_TF,
    BTC_EMA_PERIOD,
    BTC_KLINE_COUNT,
    ENABLE_MARKET_REGIME_GATE,
    MARKET_REGIME_SYMBOL,
    MARKET_REGIME_TF,
    MARKET_REGIME_KLINE_COUNT,
    MARKET_REGIME_EMA_FAST,
    MARKET_REGIME_EMA_SLOW,
    MARKET_REGIME_SLOPE_LOOKBACK,
    BLOCK_SHORTS_IN_BTC_BULL,
    BLOCK_LONGS_IN_BTC_BEAR,
    ALLOW_COUNTER_REGIME_IF_SCORE,
    MIN_STRUCTURE_RR,
    MAX_STRUCTURE_RR,
    REWARD_RATIO,
    SL_BUFFER_PCT,
    TP_BUFFER_PCT,
    MIN_SL_PCT,
    MAX_SL_PCT,
    MIN_SETUP_SCORE,
    MIN_TP_ROI_PCT,
    MAX_SL_ROI_PCT,
    REQUIRE_MTF_ALIGNMENT,
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
    USE_SR_TARGETS,
    SR_LOOKBACK,
    SR_SWING_LEFT,
    SR_SWING_RIGHT,
    SR_MERGE_ATR_MULT,
    MIN_ROOM_TO_TARGET_ATR,
    MAX_TARGET_DISTANCE_ATR,
    SR_MIN_TOUCHES,
    ALLOW_FIXED_RR_FALLBACK,
    SR_TARGET_SCORE_BONUS,
    MAX_OB_DISTANCE_ATR,
    MAX_OB_DISTANCE_PCT,
    INVALIDATE_ON_WICK,
    INVALIDATE_ON_CLOSE,
    PENDING_INVALIDATION_BUFFER_PCT,
    USE_PLANNED_ENTRY_FOR_RR,
    KEEP_WAITING_ON_FINAL_RR_FAIL,
    HIGH_SCORE_MIN_FINAL_RR,
    HIGH_SCORE_RR_SCORE_THRESHOLD,
)

logger = logging.getLogger(__name__)

_is_elite = QUALITY_MODE == "elite"

# ── Module-level caches ───────────────────────────────────────────

_BTC_REGIME_CACHE: dict[str, object] = {
    "expires_at": datetime.min.replace(tzinfo=timezone.utc),
    "strongly_bullish": False,
    "strongly_bearish": False,
}

_MARKET_REGIME_CACHE: dict[str, object] = {
    "expires_at": datetime.min.replace(tzinfo=timezone.utc),
    "regime": "NEUTRAL",
}

# Symbols temporarily blocked from rescanning after a clear ATR rejection.
_SYMBOL_SKIP_UNTIL: dict[str, datetime] = {}


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


# ── Indicator helpers ─────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _ema_slope(series: pd.Series, lookback: int = 3) -> float:
    if len(series) < lookback + 1:
        return 0.0
    return float(series.iloc[-1]) - float(series.iloc[-1 - lookback])


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"].astype(float)
    low  = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.ewm(span=period, adjust=False).mean()


def _atr_scalar(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or df.empty or len(df) < period + 2:
        return 0.0
    val = float(_atr(df, period).iloc[-1])
    return val if val > 0 else 0.0


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
    return _atr_pct(df)


def _touches_zone(row: pd.Series, zone_low: float, zone_high: float) -> bool:
    return float(row["low"]) <= zone_high and float(row["high"]) >= zone_low


def _distance_to_zone(price: float, zone_low: float, zone_high: float) -> float:
    if zone_low <= price <= zone_high:
        return 0.0
    if price < zone_low:
        return zone_low - price
    return price - zone_high


def _distance_from_ob(
    price: float, zone_low: float, zone_high: float, atr_value: float
) -> tuple[float, float]:
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


def _ob_entry_quality_ok(
    row: pd.Series, direction: str, zone_low: float, zone_high: float
) -> bool:
    if not OB_ENTRY_QUALITY_CHECK:
        return True

    midpoint = (zone_low + zone_high) / 2.0
    if direction == "LONG":
        return float(row["low"]) <= midpoint
    return float(row["high"]) >= midpoint


def _mss_break_ok(
    retest_row: pd.Series, candidate_row: pd.Series, direction: str
) -> bool:
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


# ── Candle helpers ────────────────────────────────────────────────

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
    high  = float(row["high"])
    low   = float(row["low"])
    close = float(row["close"])
    rng   = high - low
    return 0.5 if rng <= 0 else (close - low) / rng


def _avg_body(df: pd.DataFrame, pos: int, period: int) -> float:
    start  = max(0, pos - period)
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


# ── Swing / structure helpers ─────────────────────────────────────

def _find_swings(df: pd.DataFrame, left: int, right: int) -> list[dict]:
    swings: list[dict] = []
    if len(df) < left + right + 5:
        return swings

    highs = df["high"].astype(float)
    lows  = df["low"].astype(float)

    for i in range(left, len(df) - right):
        high = float(highs.iloc[i])
        low  = float(lows.iloc[i])

        if high > float(highs.iloc[i - left:i].max()) and high > float(highs.iloc[i + 1:i + right + 1].max()):
            swings.append({"type": "HIGH", "pos": i, "time": df.index[i], "price": high})

        if low < float(lows.iloc[i - left:i].min()) and low < float(lows.iloc[i + 1:i + right + 1].min()):
            swings.append({"type": "LOW",  "pos": i, "time": df.index[i], "price": low})

    swings.sort(key=lambda x: x["pos"])
    return swings


def _last_swing_before(swings: list[dict], swing_type: str, pos: int) -> dict | None:
    candidates = [s for s in swings if s["type"] == swing_type and s["pos"] < pos]
    return candidates[-1] if candidates else None


def _find_target_swing(
    swings: list[dict], direction: str, pos: int, reference_price: float
) -> dict | None:
    if direction == "LONG":
        candidates = [
            s for s in swings
            if s["type"] == "HIGH" and s["pos"] < pos and s["price"] > reference_price
        ]
    else:
        candidates = [
            s for s in swings
            if s["type"] == "LOW" and s["pos"] < pos and s["price"] < reference_price
        ]
    return candidates[-1] if candidates else None


def _build_sr_levels(
    df: pd.DataFrame,
    left: int,
    right: int,
    merge_distance: float,
) -> list[dict]:
    swings = _find_swings(df, left, right)
    n = len(df)

    raw: list[dict] = []
    for s in swings:
        level_type = "RESISTANCE" if s["type"] == "HIGH" else "SUPPORT"
        raw.append({"type": level_type, "price": s["price"], "pos": s["pos"]})

    result: list[dict] = []
    for level_type in ("RESISTANCE", "SUPPORT"):
        levels = sorted(
            [r for r in raw if r["type"] == level_type],
            key=lambda x: x["price"],
        )

        groups: list[list[dict]] = []
        for lvl in levels:
            if groups and abs(lvl["price"] - groups[-1][-1]["price"]) <= merge_distance:
                groups[-1].append(lvl)
            else:
                groups.append([lvl])

        for group in groups:
            prices    = [g["price"] for g in group]
            positions = [g["pos"]   for g in group]
            avg_price = sum(prices) / len(prices)
            last_pos  = max(positions)
            touches   = len(group)
            recency_bonus = max(0.0, 1.0 - ((n - last_pos) / n)) if n > 0 else 0.0
            strength  = touches + recency_bonus
            result.append({
                "type":     level_type,
                "price":    avg_price,
                "touches":  touches,
                "strength": strength,
                "last_pos": last_pos,
            })

    return result


def _select_sr_target(
    direction: str,
    entry_ref: float,
    sl_price: float,
    df: pd.DataFrame,
) -> tuple[float, float, dict] | None:
    atr = _atr_scalar(df)
    if atr <= 0:
        return None

    sr_df     = df.tail(SR_LOOKBACK)
    merge_dist = atr * SR_MERGE_ATR_MULT
    levels    = _build_sr_levels(sr_df, SR_SWING_LEFT, SR_SWING_RIGHT, merge_dist)

    if direction == "LONG":
        risk = entry_ref - sl_price
        if risk <= 0:
            return None
        candidates = sorted(
            [l for l in levels if l["type"] == "RESISTANCE" and l["price"] > entry_ref],
            key=lambda x: x["price"],
        )
    else:
        risk = sl_price - entry_ref
        if risk <= 0:
            return None
        candidates = sorted(
            [l for l in levels if l["type"] == "SUPPORT" and l["price"] < entry_ref],
            key=lambda x: x["price"],
            reverse=True,
        )

    valid: list[tuple[float, float, float, dict]] = []
    for lvl in candidates:
        if lvl["touches"] < SR_MIN_TOUCHES:
            continue

        if direction == "LONG":
            distance = lvl["price"] - entry_ref
        else:
            distance = entry_ref - lvl["price"]

        if distance < atr * MIN_ROOM_TO_TARGET_ATR:
            continue
        if distance > atr * MAX_TARGET_DISTANCE_ATR:
            continue

        if direction == "LONG":
            target = lvl["price"] * (1.0 - TP_BUFFER_PCT / 100.0)
            reward = target - entry_ref
        else:
            target = lvl["price"] * (1.0 + TP_BUFFER_PCT / 100.0)
            reward = entry_ref - target

        if reward <= 0:
            continue

        rr = reward / risk
        if rr < MIN_STRUCTURE_RR:
            continue

        if rr > MAX_STRUCTURE_RR:
            rr = MAX_STRUCTURE_RR
            if direction == "LONG":
                target = entry_ref + risk * rr
            else:
                target = entry_ref - risk * rr

        valid.append((distance, -lvl["touches"], target, rr, lvl))

    if not valid:
        return None

    valid.sort(key=lambda x: (x[0], x[1]))
    _, _, target, rr, lvl = valid[0]
    return round(target, 8), round(rr, 2), lvl


def _get_market_structure_bias(structure_df: pd.DataFrame) -> tuple[str | None, dict]:
    """Determines 1H bias via swing structure break."""
    completed = structure_df.iloc[:-1].copy()
    if len(completed) < STRUCTURE_LOOKBACK // 2:
        return None, {}

    recent = completed.tail(STRUCTURE_LOOKBACK).copy()
    swings = _find_swings(recent, SWING_LEFT, SWING_RIGHT)
    if len(swings) < 4:
        return None, {}

    last_bias        = None
    last_break_price = None
    last_break_time  = None

    for i in range(SWING_LEFT + SWING_RIGHT + 2, len(recent)):
        close        = float(recent["close"].iloc[i])
        previous_high = _last_swing_before(swings, "HIGH", i)
        previous_low  = _last_swing_before(swings, "LOW",  i)

        if previous_high and close > previous_high["price"]:
            last_bias        = "LONG"
            last_break_price = previous_high["price"]
            last_break_time  = recent.index[i]

        if previous_low and close < previous_low["price"]:
            last_bias        = "SHORT"
            last_break_price = previous_low["price"]
            last_break_time  = recent.index[i]

    if not last_bias:
        return None, {}

    return last_bias, {
        "bias":        last_bias,
        "break_price": last_break_price,
        "break_time":  last_break_time,
        "swing_count": len(swings),
    }


# ── MTF regime / trend helpers ────────────────────────────────────

def _get_macro_regime(macro_df: pd.DataFrame) -> str | None:
    """1D macro using EMA50/200. Returns 'LONG', 'SHORT', or None."""
    if len(macro_df) < HTF_EMA_SLOW + 10:
        return None

    close  = macro_df["close"].astype(float)
    ema50  = _ema(close, 50)
    ema200 = _ema(close, 200)

    last_close = float(close.iloc[-1])
    last_50    = float(ema50.iloc[-1])
    last_200   = float(ema200.iloc[-1])

    if last_close > last_50 and last_50 >= last_200:
        return "LONG"
    if last_close < last_50 and last_50 <= last_200:
        return "SHORT"
    return None


def _get_htf_trend(htf_df: pd.DataFrame) -> str | None:
    """4H trend using EMA50/200. Returns 'LONG', 'SHORT', or None."""
    if len(htf_df) < HTF_EMA_SLOW + 10:
        return None

    close   = htf_df["close"].astype(float)
    ema_fast = _ema(close, HTF_EMA_FAST)
    ema_slow = _ema(close, HTF_EMA_SLOW)

    last_close = float(close.iloc[-1])
    last_fast  = float(ema_fast.iloc[-1])
    last_slow  = float(ema_slow.iloc[-1])

    if last_fast > last_slow and last_close > last_fast:
        return "LONG"
    if last_fast < last_slow and last_close < last_fast:
        return "SHORT"
    return None


# ── Filters ───────────────────────────────────────────────────────

def _htf_trend_ok(symbol: str, direction: str) -> tuple[bool, bool]:
    """4H revalidation filter used by evaluate_pending_setup()."""
    if not ENABLE_HTF_FILTER:
        return True, False

    try:
        df = get_klines(symbol, HTF_TREND_TF, count=HTF_KLINE_COUNT)
        if df is None or df.empty or len(df) < HTF_EMA_SLOW + 5:
            logger.warning("[HTF] %s insufficient data — filter skipped", symbol)
            return True, False

        close    = df["close"].astype(float)
        ema_fast = _ema(close, HTF_EMA_FAST)
        ema_slow = _ema(close, HTF_EMA_SLOW)

        last_close = float(close.iloc[-1])
        last_fast  = float(ema_fast.iloc[-1])
        last_slow  = float(ema_slow.iloc[-1])
        slope      = _ema_slope(ema_fast, HTF_EMA_SLOPE_LOOKBACK)

        if direction == "LONG":
            strong  = last_fast > last_slow
            allowed = strong or (last_close > last_slow and slope > 0)
        else:
            strong  = last_fast < last_slow
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

    close    = df["close"].astype(float)
    ema_fast = _ema(close, EMA_FAST_FILTER)
    ema_slow = _ema(close, EMA_SLOW_FILTER)

    last_close = float(close.iloc[-1])
    last_fast  = float(ema_fast.iloc[-1])
    last_slow  = float(ema_slow.iloc[-1])

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
        avg   = float(volume.iloc[start:pos].mean())
        if avg <= 0:
            return True

        return float(volume.iloc[pos]) >= avg * MIN_VOLUME_MULTIPLIER
    except Exception:
        return True


def _btc_regime_ok(direction: str) -> bool:
    """BTC 1H EMA50 quick regime guard with short in-process cache."""
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
                ema   = _ema(close, BTC_EMA_PERIOD)
                last_close = float(close.iloc[-1])
                last_ema   = float(ema.iloc[-1])
                slope      = _ema_slope(ema)

                _BTC_REGIME_CACHE.update({
                    "expires_at":      now + timedelta(seconds=60),
                    "strongly_bullish": last_close > last_ema and slope > 0,
                    "strongly_bearish": last_close < last_ema and slope < 0,
                })

        if direction == "LONG" and bool(_BTC_REGIME_CACHE["strongly_bearish"]):
            return False
        if direction == "SHORT" and bool(_BTC_REGIME_CACHE["strongly_bullish"]):
            return False

        return True

    except Exception as e:
        logger.warning("[BTC-REGIME] fetch error: %s — filter skipped", e)
        return True


def _get_market_regime() -> str:
    """
    Returns 'BULLISH', 'BEARISH', or 'NEUTRAL' based on BTC 4H EMA50/200.
    Cached for 2 minutes.

    BULLISH: close > EMA50 > EMA200 AND EMA50 slope > 0
    BEARISH: close < EMA50 < EMA200 AND EMA50 slope < 0
    NEUTRAL: anything else
    """
    if not ENABLE_MARKET_REGIME_GATE:
        return "NEUTRAL"

    now = datetime.now(timezone.utc)

    try:
        if now >= _MARKET_REGIME_CACHE["expires_at"]:
            df = get_klines(
                MARKET_REGIME_SYMBOL, MARKET_REGIME_TF,
                count=MARKET_REGIME_KLINE_COUNT,
            )

            if df is None or df.empty or len(df) < MARKET_REGIME_EMA_SLOW + 5:
                logger.warning("[REGIME] %s %s insufficient data — NEUTRAL", MARKET_REGIME_SYMBOL, MARKET_REGIME_TF)
                _MARKET_REGIME_CACHE.update({
                    "expires_at": now + timedelta(seconds=120),
                    "regime": "NEUTRAL",
                })
            else:
                close    = df["close"].astype(float)
                ema_fast = _ema(close, MARKET_REGIME_EMA_FAST)
                ema_slow = _ema(close, MARKET_REGIME_EMA_SLOW)
                slope    = _ema_slope(ema_fast, MARKET_REGIME_SLOPE_LOOKBACK)

                last_close = float(close.iloc[-1])
                last_fast  = float(ema_fast.iloc[-1])
                last_slow  = float(ema_slow.iloc[-1])

                if last_close > last_fast and last_fast > last_slow and slope > 0:
                    regime = "BULLISH"
                elif last_close < last_fast and last_fast < last_slow and slope < 0:
                    regime = "BEARISH"
                else:
                    regime = "NEUTRAL"

                _MARKET_REGIME_CACHE.update({
                    "expires_at": now + timedelta(seconds=120),
                    "regime": regime,
                })

        return str(_MARKET_REGIME_CACHE["regime"])

    except Exception as e:
        logger.warning("[REGIME] fetch error: %s — NEUTRAL assumed", e)
        return "NEUTRAL"


def _regime_gate_ok(direction: str, score: float) -> tuple[bool, str]:
    """
    Returns (allowed, log_reason).  Called after score is computed.

    In elite mode, counter-regime setups are only allowed if score >=
    ALLOW_COUNTER_REGIME_IF_SCORE (98 by default).
    """
    if not ENABLE_MARKET_REGIME_GATE:
        return True, ""

    regime = _get_market_regime()

    if regime == "NEUTRAL":
        return True, "BTC regime neutral"

    if direction == "SHORT" and regime == "BULLISH" and BLOCK_SHORTS_IN_BTC_BULL:
        if score >= ALLOW_COUNTER_REGIME_IF_SCORE:
            return True, f"SHORT allowed despite BTC bullish high_score={score:.1f}"
        return False, f"SHORT blocked because BTC {MARKET_REGIME_TF} bullish"

    if direction == "LONG" and regime == "BEARISH" and BLOCK_LONGS_IN_BTC_BEAR:
        if score >= ALLOW_COUNTER_REGIME_IF_SCORE:
            return True, f"LONG allowed despite BTC bearish high_score={score:.1f}"
        return False, f"LONG blocked because BTC {MARKET_REGIME_TF} bearish"

    return True, ""


def _generate_setup_key(
    symbol: str,
    direction: str,
    ob_low: float,
    ob_high: float,
    sweep_level: float,
    ob_time: str,
    sweep_time: str,
) -> str:
    """Fingerprint that uniquely identifies a setup by its structural components."""
    return (
        f"{symbol}:{direction}"
        f":{ob_low:.8g}:{ob_high:.8g}"
        f":{sweep_level:.8g}"
        f":{ob_time}:{sweep_time}"
    )


# ── Sweep / displacement / OB ─────────────────────────────────────

def _detect_sell_side_sweep(
    df: pd.DataFrame, swings: list[dict], pos: int
) -> dict | None:
    prev_low = _last_swing_before(swings, "LOW", pos)
    if not prev_low:
        return None

    row   = df.iloc[pos]
    low   = float(row["low"])
    close = float(row["close"])

    if low < prev_low["price"] and close > prev_low["price"]:
        return {
            "type":    "SELL_SIDE_SWEEP",
            "swing":   prev_low,
            "pos":     pos,
            "time":    df.index[pos],
            "level":   prev_low["price"],
            "extreme": low,
        }
    return None


def _detect_buy_side_sweep(
    df: pd.DataFrame, swings: list[dict], pos: int
) -> dict | None:
    prev_high = _last_swing_before(swings, "HIGH", pos)
    if not prev_high:
        return None

    row   = df.iloc[pos]
    high  = float(row["high"])
    close = float(row["close"])

    if high > prev_high["price"] and close < prev_high["price"]:
        return {
            "type":    "BUY_SIDE_SWEEP",
            "swing":   prev_high,
            "pos":     pos,
            "time":    df.index[pos],
            "level":   prev_high["price"],
            "extreme": high,
        }
    return None


def _is_bullish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg = _avg_body(df, pos, AVG_BODY_PERIOD)
    return (
        avg > 0
        and _is_bullish(row)
        and _body_size(row) >= avg * DISPLACEMENT_BODY_MULTIPLIER
        and _close_position(row) >= DISPLACEMENT_CLOSE_POSITION
    )


def _is_bearish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg = _avg_body(df, pos, AVG_BODY_PERIOD)
    return (
        avg > 0
        and _is_bearish(row)
        and _body_size(row) >= avg * DISPLACEMENT_BODY_MULTIPLIER
        and _close_position(row) <= (1.0 - DISPLACEMENT_CLOSE_POSITION)
    )


def _find_bullish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)
    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]
        if _is_bearish(row):
            return {
                "type":      "BULLISH_OB",
                "pos":       i,
                "time":      df.index[i],
                "zone_low":  float(row["low"]),
                "zone_high": max(float(row["open"]), float(row["close"])),
            }
    return None


def _find_bearish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)
    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]
        if _is_bullish(row):
            return {
                "type":      "BEARISH_OB",
                "pos":       i,
                "time":      df.index[i],
                "zone_low":  min(float(row["open"]), float(row["close"])),
                "zone_high": float(row["high"]),
            }
    return None


def _calculate_setup_prices(
    direction: str,
    ob: dict,
    sweep: dict,
    target_swing: dict | None,
    atr_value: float,
    entry_df: pd.DataFrame | None = None,
) -> tuple[float, float, float, float, str] | None:
    """Returns (sl_price, target_price, rr, sl_pct, target_source) or None."""
    ob_mid = (ob["zone_low"] + ob["zone_high"]) / 2.0
    if ob_mid <= 0:
        return None

    atr_buffer = atr_value * ATR_SL_MULTIPLIER if ENABLE_ATR_FILTER else 0.0

    if direction == "LONG":
        sl_price = min(sweep["extreme"], ob["zone_low"]) * (1.0 - SL_BUFFER_PCT / 100.0) - atr_buffer
    else:
        sl_price = max(sweep["extreme"], ob["zone_high"]) * (1.0 + SL_BUFFER_PCT / 100.0) + atr_buffer

    target_price: float | None = None
    target_source = "FIXED_RR"

    # 1. Try SR-based target
    if USE_SR_TARGETS and entry_df is not None:
        sr_result = _select_sr_target(direction, ob_mid, sl_price, entry_df)
        if sr_result is not None:
            target_price, _, _ = sr_result
            target_source = "SR"

    # 2. Fall back to target swing
    if target_price is None:
        if direction == "LONG" and target_swing and target_swing["price"] > ob_mid:
            target_price  = target_swing["price"] * (1.0 - TP_BUFFER_PCT / 100.0)
            target_source = "SWING"
        elif direction == "SHORT" and target_swing and target_swing["price"] < ob_mid:
            target_price  = target_swing["price"] * (1.0 + TP_BUFFER_PCT / 100.0)
            target_source = "SWING"

    # 3. Fall back to fixed RR
    if target_price is None:
        if not ALLOW_FIXED_RR_FALLBACK:
            return None
        if direction == "LONG":
            target_price = ob_mid + (ob_mid - sl_price) * REWARD_RATIO
        else:
            target_price = ob_mid - (sl_price - ob_mid) * REWARD_RATIO

    if direction == "LONG":
        risk   = ob_mid - sl_price
        reward = target_price - ob_mid
    else:
        risk   = sl_price - ob_mid
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

    return round(sl_price, 8), round(target_price, 8), round(rr, 2), round(sl_pct, 3), target_source


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry and sl < entry
    if direction == "SHORT":
        return tp < entry and sl > entry
    return False


def _trade_quality_ok(
    direction: str,
    entry: float,
    tp: float,
    sl: float,
    leverage: int,
    min_rr: float | None = None,
) -> tuple[bool, float, float, float]:
    if not _valid_trade_geometry(direction, entry, tp, sl):
        return False, 0.0, 0.0, 0.0

    if direction == "LONG":
        risk_pct   = (entry - sl) / entry * 100.0
        reward_pct = (tp - entry) / entry * 100.0
    else:
        risk_pct   = (sl - entry) / entry * 100.0
        reward_pct = (entry - tp) / entry * 100.0

    if risk_pct <= 0 or reward_pct <= 0:
        return False, 0.0, 0.0, 0.0

    rr     = reward_pct / risk_pct
    tp_roi = reward_pct * leverage
    sl_roi = risk_pct  * leverage

    effective_min_rr = min_rr if min_rr is not None else MIN_STRUCTURE_RR
    ok = (
        rr     >= effective_min_rr
        and tp_roi >= MIN_TP_ROI_PCT
        and sl_roi <= MAX_SL_ROI_PCT
    )

    return ok, round(tp_roi, 1), round(sl_roi, 1), round(rr, 2)


def _calculate_final_prices(
    direction: str,
    entry: float,
    setup: dict,
    atr_value: float = 0.0,
    min_rr_override: float | None = None,
) -> tuple[float, float, float, float, float] | None:
    sl_price = float(setup["sl_price"])

    if entry <= 0 or sl_price <= 0:
        return None

    if ENABLE_ATR_STOP_FLOOR and atr_value > 0 and ATR_STOP_FLOOR_MULTIPLIER > 0:
        min_risk = atr_value * ATR_STOP_FLOOR_MULTIPLIER
        if direction == "LONG" and entry - sl_price < min_risk:
            sl_price = entry - min_risk
        elif direction == "SHORT" and sl_price - entry < min_risk:
            sl_price = entry + min_risk

    if direction == "LONG":
        risk = entry - sl_price
        if risk <= 0:
            return None
        target_price = entry + (risk * REWARD_RATIO)
    else:
        risk = sl_price - entry
        if risk <= 0:
            return None
        target_price = entry - (risk * REWARD_RATIO)

    if not _valid_trade_geometry(direction, entry, target_price, sl_price):
        logger.warning(
            "[FINAL-REJECT] invalid geometry %s entry=%.8g tp=%.8g sl=%.8g",
            direction, entry, target_price, sl_price,
        )
        return None

    sl_pct = risk / entry * 100.0
    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None

    if min_rr_override is not None:
        logger.info("[RR-OVERRIDE] high score setup using min_rr=%.2f", min_rr_override)

    ok, tp_roi_pct, sl_roi_pct, rr = _trade_quality_ok(
        direction=direction,
        entry=entry,
        tp=target_price,
        sl=sl_price,
        leverage=LEVERAGE,
        min_rr=min_rr_override,
    )

    if not ok:
        logger.info(
            "[FINAL-REJECT] quality failed %s entry=%.8g tp=%.8g sl=%.8g rr=%.2f tp_roi=%.1f sl_roi=%.1f",
            direction, entry, target_price, sl_price, rr, tp_roi_pct, sl_roi_pct,
        )
        return None

    return (
        round(target_price, 8),
        round(sl_price, 8),
        tp_roi_pct,
        sl_roi_pct,
        rr,
    )


def _score_setup(
    rr: float,
    ob_age: int,
    sweep_age: int,
    displacement_age: int,
    mtf_aligned: bool,
    atr_ok: bool,
    volume_ok: bool,
    ema_ok: bool,
) -> float:
    """
    MTF-weighted scoring.

    Base 50 — requires MTF alignment + decent RR + fresh OB to reach MIN_SETUP_SCORE.
    Elite threshold of 95 further filters to only the cleanest setups.
    """
    score = 50.0

    if mtf_aligned:
        score += 20.0

    if rr >= 3.0:
        score += 15.0
    elif rr >= 2.0:
        score += 10.0

    if ob_age <= 6:
        score += 10.0
    elif ob_age <= 12:
        score += 5.0

    if sweep_age <= 6:
        score += 5.0

    if displacement_age <= 6:
        score += 5.0

    if atr_ok and ENABLE_ATR_FILTER:
        score += 3.0
    if volume_ok and ENABLE_VOLUME_FILTER:
        score += 3.0
    if ema_ok and ENABLE_ENTRY_EMA_FILTER:
        score += 3.0

    return round(min(score, 100.0), 1)


# ── Public setup detection ────────────────────────────────────────

def detect_setup(symbol: str) -> dict | None:
    try:
        # ── ATR cooldown gate ──────────────────────────────────────
        _now = datetime.now(timezone.utc)
        _skip_until = _SYMBOL_SKIP_UNTIL.get(symbol)
        if _skip_until and _now < _skip_until:
            return None

        # ── Fetch all 4 timeframes ─────────────────────────────────
        macro_df     = get_klines(symbol, MACRO_TF,     count=MACRO_KLINE_COUNT)
        htf_df       = get_klines(symbol, HTF_TREND_TF, count=HTF_KLINE_COUNT)
        structure_df = get_klines(symbol, STRUCTURE_TF, count=STRUCTURE_KLINE_COUNT)
        entry_df     = get_klines(symbol, ENTRY_TF,     count=ENTRY_KLINE_COUNT)

        if any(df is None or df.empty for df in [macro_df, htf_df, structure_df, entry_df]):
            return None

        # ── 1H structure bias ──────────────────────────────────────
        bias, bias_details = _get_market_structure_bias(structure_df)
        if bias is None:
            return None

        # ── 1D macro regime ────────────────────────────────────────
        macro_regime = _get_macro_regime(macro_df)

        # ── 4H trend ───────────────────────────────────────────────
        htf_trend = _get_htf_trend(htf_df)

        # ── MTF alignment gate ─────────────────────────────────────
        # Elite mode requires strict 3-way agreement (no neutral allowed).
        if _is_elite:
            if macro_regime is None or htf_trend is None:
                logger.info(
                    "[SETUP-REJECT] %s | elite: incomplete MTF data 1D=%s 4H=%s 1H=%s",
                    symbol, macro_regime, htf_trend, bias,
                )
                return None
            if not (macro_regime == htf_trend == bias):
                logger.info(
                    "[SETUP-REJECT] %s | elite MTF mismatch 1D=%s 4H=%s 1H=%s",
                    symbol, macro_regime, htf_trend, bias,
                )
                return None
        else:
            if macro_regime is None:
                logger.info("[SETUP-REJECT] %s | 1D macro neutral", symbol)
                return None

            if htf_trend is None:
                logger.info("[SETUP-REJECT] %s | 4H trend neutral", symbol)
                return None

            mtf_aligned_check = (macro_regime == htf_trend == bias)
            if REQUIRE_MTF_ALIGNMENT and not mtf_aligned_check:
                logger.info(
                    "[SETUP-REJECT] %s | MTF mismatch 1D=%s 4H=%s 1H=%s",
                    symbol, macro_regime, htf_trend, bias,
                )
                return None

        mtf_aligned = (macro_regime == htf_trend == bias)

        # ── BTC 1H quick regime cross-check ───────────────────────
        if not _btc_regime_ok(bias):
            logger.info("[SETUP-REJECT] %s | BTC 1H filter conflict", symbol)
            return None

        # ── Entry TF filters (15m) ─────────────────────────────────
        completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)
        if len(completed) < 80:
            return None

        atr_ok, atr_value_pct, atr_value = _atr_pct(completed)
        if not atr_ok:
            if ATR_REJECT_COOLDOWN_MINUTES > 0:
                low_miss  = MIN_ATR_PCT - atr_value_pct
                high_miss = atr_value_pct - MAX_ATR_PCT
                miss_pct  = max(low_miss, high_miss)
                if miss_pct > ATR_REJECT_COOLDOWN_MARGIN_PCT:
                    _SYMBOL_SKIP_UNTIL[symbol] = (
                        datetime.now(timezone.utc) + timedelta(minutes=ATR_REJECT_COOLDOWN_MINUTES)
                    )
                    logger.info(
                        "[ATR-COOLDOWN] %s skipped %dm ATR=%.2f%%",
                        symbol, ATR_REJECT_COOLDOWN_MINUTES, atr_value_pct,
                    )
            logger.info(
                "[SETUP-REJECT] %s | ATR %.2f%% outside %.2f-%.2f",
                symbol, atr_value_pct, MIN_ATR_PCT, MAX_ATR_PCT,
            )
            return None

        ema_ok = _entry_ema_ok(completed, bias)
        if not ema_ok:
            logger.info("[SETUP-REJECT] %s | entry EMA misaligned", symbol)
            return None

        # Current close used for OB distance check at detect time.
        current_entry_close = float(completed["close"].astype(float).iloc[-1])

        swings = _find_swings(completed, SWING_LEFT, SWING_RIGHT)
        if len(swings) < 4:
            return None

        start         = max(AVG_BODY_PERIOD + SWEEP_LOOKBACK + ORDER_BLOCK_LOOKBACK, 30)
        last_possible = len(completed) - 3

        best_setup = None
        best_score = -1.0

        for displacement_pos in range(start, last_possible + 1):
            if bias == "LONG" and not _is_bullish_displacement(completed, displacement_pos):
                continue
            if bias == "SHORT" and not _is_bearish_displacement(completed, displacement_pos):
                continue

            displacement_age = len(completed) - displacement_pos
            if displacement_age > MAX_DISPLACEMENT_AGE_CANDLES:
                continue

            vol_ok = _volume_ok(completed, displacement_pos)
            if not vol_ok:
                logger.info("[SETUP-REJECT] %s | volume weak", symbol)
                continue

            sweep = None
            sweep_start = max(0, displacement_pos - SWEEP_LOOKBACK)
            for sweep_pos in range(displacement_pos - 1, sweep_start - 1, -1):
                sweep = (
                    _detect_sell_side_sweep(completed, swings, sweep_pos)
                    if bias == "LONG"
                    else _detect_buy_side_sweep(completed, swings, sweep_pos)
                )
                if sweep:
                    break

            if not sweep:
                continue

            sweep_age = len(completed) - sweep["pos"]
            if sweep_age > MAX_SWEEP_AGE_CANDLES:
                logger.info("[SETUP-REJECT] %s | sweep too old (%d candles)", symbol, sweep_age)
                continue

            ob = (
                _find_bullish_ob(completed, displacement_pos)
                if bias == "LONG"
                else _find_bearish_ob(completed, displacement_pos)
            )
            if not ob:
                continue

            ob_age = len(completed) - ob["pos"]
            if ob_age > MAX_OB_AGE_CANDLES:
                logger.info("[SETUP-REJECT] %s | stale OB age=%d max=%d", symbol, ob_age, MAX_OB_AGE_CANDLES)
                continue

            # Reject if price is already too far from the OB — avoids setups that
            # will never fill and then expire.
            too_far_now, dist_pct_now, dist_atr_now = _too_far_from_ob(
                current_entry_close,
                ob["zone_low"],
                ob["zone_high"],
                atr_value,
                MAX_OB_DISTANCE_PCT,
                MAX_OB_DISTANCE_ATR,
            )
            if too_far_now:
                logger.info(
                    "[SETUP-REJECT] %s | OB too far close=%.6g OB=%.6g-%.6g distance=%.2f%%/%.2fATR",
                    symbol, current_entry_close,
                    ob["zone_low"], ob["zone_high"],
                    dist_pct_now, dist_atr_now,
                )
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
                entry_df=completed,
            )
            if not prices:
                if USE_SR_TARGETS and not ALLOW_FIXED_RR_FALLBACK:
                    logger.info(
                        "[SETUP-REJECT] %s | %s requires valid SR target (no fixed RR fallback)",
                        symbol, "elite" if _is_elite else "config",
                    )
                continue

            sl_price, target_price, rr_estimate, sl_pct, target_source = prices

            score = _score_setup(
                rr_estimate, ob_age, sweep_age, displacement_age,
                mtf_aligned, atr_ok, vol_ok, ema_ok,
            )

            sr_bonus_applied = False
            if target_source == "SR" and SR_TARGET_SCORE_BONUS > 0:
                score = min(score + SR_TARGET_SCORE_BONUS, 100.0)
                sr_bonus_applied = True

            if score < MIN_SETUP_SCORE:
                logger.info(
                    "[SETUP-REJECT] %s | score %.1f < min %g",
                    symbol, score, MIN_SETUP_SCORE,
                )
                continue

            # ── Market regime gate (applied after score is known) ──
            regime_ok, regime_reason = _regime_gate_ok(bias, score)
            if not regime_ok:
                logger.info("[REGIME-GATE] %s %s %s", symbol, bias, regime_reason)
                continue
            if regime_reason:
                logger.info("[REGIME-GATE] %s %s %s", symbol, bias, regime_reason)

            if score <= best_score:
                continue

            now        = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=PENDING_SETUP_EXPIRE_CANDLES * CANDLE_MINUTES)

            best_score = score
            best_setup = {
                "symbol":    symbol,
                "direction": bias,
                "trend_tf":  STRUCTURE_TF,
                "entry_tf":  ENTRY_TF,
                "bias":      bias,
                "bias_break": bias_details.get("break_price"),
                "sweep_type":    sweep["type"],
                "sweep_level":   sweep["level"],
                "sweep_extreme": sweep["extreme"],
                "sweep_time":    _iso(sweep["time"]),
                "ob_type":  ob["type"],
                "ob_low":   ob["zone_low"],
                "ob_high":  ob["zone_high"],
                "ob_time":  _iso(ob["time"]),
                "target_price": target_price,
                "sl_price":     sl_price,
                "rr_estimate":  rr_estimate,
                "score":        score,
                "setup_time":   _iso(completed.index[displacement_pos]),
                "expires_at":   expires_at.isoformat(),
                # MTF context stored for DB and signal summary
                "macro_tf":      MACRO_TF,
                "macro_bias":    macro_regime,
                "htf_tf":        HTF_TREND_TF,
                "htf_bias":      htf_trend,
                "structure_tf":  STRUCTURE_TF,
                "structure_bias": bias,
                "setup_key": _generate_setup_key(
                    symbol=symbol,
                    direction=bias,
                    ob_low=ob["zone_low"],
                    ob_high=ob["zone_high"],
                    sweep_level=sweep["level"],
                    ob_time=_iso(ob["time"]),
                    sweep_time=_iso(sweep["time"]),
                ),
            }

            logger.info(
                "[SETUP] %s %s | OB=%.6g-%.6g SL=%.6g TP=%.6g SL%%=%.2f ATR%%=%.2f RR=%.2f"
                " score=%.1f tp_src=%s sr_bonus=%s regime=%s [1D=%s 4H=%s 1H=%s]",
                bias, symbol,
                ob["zone_low"], ob["zone_high"],
                sl_price, target_price,
                sl_pct, atr_value_pct, rr_estimate, score,
                target_source, "yes" if sr_bonus_applied else "no",
                _get_market_regime(),
                macro_regime, htf_trend, bias,
            )

        return best_setup

    except Exception as e:
        logger.error("Error detecting setup for %s: %s", symbol, e, exc_info=True)
        return None


# ── Public pending setup monitoring ───────────────────────────────

def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    """
    Called by main.py for each waiting setup every minute.

    Returns:
        ("WAIT", None)         — valid but no confirmed entry yet
        ("EXPIRED", None)      — stale or price moved too far
        ("INVALIDATED", None)  — SL touched before entry
        ("FIRE", Signal)       — confirmed OB retest + MSS break entry
    """
    try:
        now        = datetime.now(timezone.utc)
        expires_at = _parse_utc(setup["expires_at"])

        if now >= expires_at:
            return "EXPIRED", None

        symbol     = setup["symbol"]
        direction  = setup["direction"]
        zone_low   = float(setup["ob_low"])
        zone_high  = float(setup["ob_high"])
        sl_price   = float(setup["sl_price"])

        df = get_klines(symbol, ENTRY_TF, count=MONITOR_KLINE_COUNT)
        if df is None or df.empty or len(df) < 6:
            _debug_wait(symbol, "not enough monitor candles")
            return "WAIT", None

        completed  = df.iloc[:-1].copy()
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
                "[SETUP-EXPIRE] %s %s | price too far from OB close=%.6g"
                " distance=%.2f%% %.2fATR limit=%.2f%% %.2fATR",
                symbol, direction, last_close,
                distance_pct, distance_atr,
                EXPIRE_IF_PRICE_AWAY_PCT, EXPIRE_IF_PRICE_AWAY_ATR,
            )
            return "EXPIRED", None

        lookback    = max(6, MSS_BREAK_LOOKBACK_CANDLES + 4)
        recent      = completed.tail(lookback).copy()
        recent_rows = list(recent.iterrows())

        # Invalidation before entry.
        inval_buf = PENDING_INVALIDATION_BUFFER_PCT / 100.0
        for _, row in recent_rows:
            if direction == "LONG":
                inval_level  = sl_price * (1.0 - inval_buf)
                wick_breach  = INVALIDATE_ON_WICK  and float(row["low"])   <= inval_level
                close_breach = INVALIDATE_ON_CLOSE and float(row["close"]) <= inval_level
                if wick_breach or close_breach:
                    logger.info(
                        "[SETUP-INVALID] %s LONG | low=%.6g close=%.6g <= inval=%.6g (SL=%.6g buf=%.2f%%)",
                        symbol, float(row["low"]), float(row["close"]),
                        inval_level, sl_price, PENDING_INVALIDATION_BUFFER_PCT,
                    )
                    return "INVALIDATED", None
            elif direction == "SHORT":
                inval_level  = sl_price * (1.0 + inval_buf)
                wick_breach  = INVALIDATE_ON_WICK  and float(row["high"])  >= inval_level
                close_breach = INVALIDATE_ON_CLOSE and float(row["close"]) >= inval_level
                if wick_breach or close_breach:
                    logger.info(
                        "[SETUP-INVALID] %s SHORT | high=%.6g close=%.6g >= inval=%.6g (SL=%.6g buf=%.2f%%)",
                        symbol, float(row["high"]), float(row["close"]),
                        inval_level, sl_price, PENDING_INVALIDATION_BUFFER_PCT,
                    )
                    return "INVALIDATED", None

        midpoint                 = (zone_low + zone_high) / 2.0
        touched_zone             = False
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

            break_row = row
            break_ts  = ts
            if REQUIRE_MSS_BREAK_ENTRY:
                break_row = None
                max_j = min(len(recent_rows), idx + 1 + MSS_BREAK_LOOKBACK_CANDLES)
                for j in range(idx + 1, max_j):
                    candidate_ts, candidate_row = recent_rows[j]
                    if _mss_break_ok(row, candidate_row, direction):
                        break_ts  = candidate_ts
                        break_row = candidate_row
                        break

                if break_row is None:
                    _debug_wait(
                        symbol,
                        f"valid OB rejection but waiting for MSS break within {MSS_BREAK_LOOKBACK_CANDLES} candle(s)",
                    )
                    continue

                # Entry volume confirmation on the MSS break candle.
                if ENABLE_ENTRY_VOLUME_CONFIRMATION:
                    break_vol = float(break_row.get("volume", 0)) if hasattr(break_row, "get") else 0.0
                    try:
                        break_vol = float(break_row["volume"])
                    except Exception:
                        break_vol = 0.0

                    if break_vol > 0:
                        avg_vol = float(
                            completed["volume"].astype(float).tail(ENTRY_VOLUME_LOOKBACK).mean()
                        )
                        if avg_vol > 0 and break_vol < avg_vol * ENTRY_MIN_VOLUME_MULTIPLIER:
                            _debug_wait(
                                symbol,
                                f"MSS break volume weak {break_vol:.0f} < required {avg_vol * ENTRY_MIN_VOLUME_MULTIPLIER:.0f}",
                            )
                            break_row = None

                if break_row is None:
                    continue

            if REVALIDATE_BEFORE_FIRE:
                htf_ok, _ = _htf_trend_ok(symbol, direction)
                if not htf_ok:
                    _debug_wait(symbol, "revalidation failed: HTF trend mismatch")
                    return "WAIT", None

                if not _btc_regime_ok(direction):
                    _debug_wait(symbol, "revalidation failed: BTC 1H filter conflict")
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

            entry  = float(break_row["close"])
            ob_mid = (zone_low + zone_high) / 2.0

            setup_score    = float(setup.get("score", 0.0))
            min_rr_override = (
                HIGH_SCORE_MIN_FINAL_RR
                if setup_score >= HIGH_SCORE_RR_SCORE_THRESHOLD
                else None
            )

            prices = _calculate_final_prices(
                direction=direction,
                entry=entry,
                setup=setup,
                atr_value=monitor_atr_value,
                min_rr_override=min_rr_override,
            )

            # If actual entry fails RR, retry using OB midpoint as reference entry.
            if prices is None and USE_PLANNED_ENTRY_FOR_RR and abs(entry - ob_mid) > 0:
                prices_planned = _calculate_final_prices(
                    direction=direction,
                    entry=ob_mid,
                    setup=setup,
                    atr_value=monitor_atr_value,
                    min_rr_override=min_rr_override,
                )
                if prices_planned is not None:
                    tp_price_p, sl_price_p, _, _, rr_p = prices_planned
                    if direction == "LONG":
                        tp_roi_adj = round((tp_price_p - entry) / entry * 100.0 * LEVERAGE, 1)
                        sl_roi_adj = round((entry - sl_price_p) / entry * 100.0 * LEVERAGE, 1)
                    else:
                        tp_roi_adj = round((entry - tp_price_p) / entry * 100.0 * LEVERAGE, 1)
                        sl_roi_adj = round((sl_price_p - entry) / entry * 100.0 * LEVERAGE, 1)
                    prices = (tp_price_p, sl_price_p, tp_roi_adj, sl_roi_adj, rr_p)
                    logger.info(
                        "[ENTRY-ADJUST] %s %s | actual entry %.6g vs OB mid %.6g — RR validated from planned",
                        direction, symbol, entry, ob_mid,
                    )

            if prices is None:
                if KEEP_WAITING_ON_FINAL_RR_FAIL:
                    _debug_wait(
                        symbol,
                        f"confirmed retest/MSS but final RR/SL failed (entry={entry:.6g} ob_mid={ob_mid:.6g}) — will retry",
                    )
                    continue
                _debug_wait(symbol, "confirmed retest/MSS but final RR/SL validation failed")
                return "WAIT", None

            tp_price, final_sl_price, tp_roi_pct, sl_roi_pct, rr = prices

            if not _valid_trade_geometry(direction, entry, tp_price, final_sl_price):
                logger.warning(
                    "[ENTRY-REJECT] %s %s invalid geometry entry=%.8g tp=%.8g sl=%.8g",
                    symbol, direction, entry, tp_price, final_sl_price,
                )
                return "WAIT", None

            quality_ok, checked_tp_roi, checked_sl_roi, checked_rr = _trade_quality_ok(
                direction=direction,
                entry=entry,
                tp=tp_price,
                sl=final_sl_price,
                leverage=LEVERAGE,
            )
            if not quality_ok:
                logger.info(
                    "[ENTRY-REJECT] %s %s final quality failed rr=%.2f tp_roi=%.1f sl_roi=%.1f",
                    symbol, direction, checked_rr, checked_tp_roi, checked_sl_roi,
                )
                return "WAIT", None

            score = min(float(setup["score"]) + 5.0, 100.0)

            macro_tf   = setup.get("macro_tf")   or MACRO_TF
            htf_tf     = setup.get("htf_tf")     or HTF_TREND_TF
            macro_bias = setup.get("macro_bias") or direction
            htf_bias   = setup.get("htf_bias")   or direction
            structure_tf = setup.get("structure_tf") or STRUCTURE_TF
            trigger_note = "MSS break" if REQUIRE_MSS_BREAK_ENTRY else "OB rejection"

            logger.info(
                "[ENTRY] %s %s @ %.6g | OB=%.6g-%.6g TP=%.6g SL=%.6g RR=%.2f score=%.1f mss=%s",
                direction, symbol, entry,
                zone_low, zone_high,
                tp_price, final_sl_price,
                rr, score,
                "on" if REQUIRE_MSS_BREAK_ENTRY else "off",
            )

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
                    f"SMC MTF | {macro_tf.upper()} macro {macro_bias} | "
                    f"{htf_tf.upper()} trend {htf_bias} | "
                    f"{structure_tf.upper()} bias {direction} | "
                    f"{ENTRY_TF} OB retest + {trigger_note} | RR {rr:g}"
                ),
                generated_at=datetime.now(timezone.utc),
                score=score,
            )

        if not touched_zone:
            _debug_wait(
                symbol,
                f"price not in OB yet close={last_close:.6g} OB={zone_low:.6g}-{zone_high:.6g}"
                f" distance={distance_pct:.2f}%/{distance_atr:.2f}ATR",
            )
        elif found_retest_without_break:
            _debug_wait(symbol, "OB rejection found but no MSS break entry yet")
        else:
            _debug_wait(symbol, "touched OB but no valid retest/rejection condition matched")

        return "WAIT", None

    except Exception as e:
        logger.error("Error evaluating pending setup %s: %s", setup.get("id"), e, exc_info=True)
        return "WAIT", None


# ── Legacy direct signal path intentionally disabled ──────────────

def analyze_coin(symbol: str) -> Signal | None:
    """Disabled — use detect_setup() + evaluate_pending_setup() instead."""
    return None
