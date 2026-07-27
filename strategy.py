"""
Binocular Trend Confluence v1.

15m Supply/Demand zones (pivot-based, BOS-tracked) provide structural
confluence; 5m Chandelier Exit direction + Price-Volume-Trend-vs-signal
momentum + dual-RSI(fast/slow) regime, confirmed by a breakout-buffer
close, drive entries. Only completed candles are ever used. See
docs/superpowers/specs/2026-07-27-binocular-trend-confluence-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

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


@dataclass
class BtcContext:
    close: float
    ema_200: float
    supertrend_direction: int
    one_candle_move_pct: float
    three_candle_move_pct: float


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


def calculate_chandelier_exit(df: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame:
    close = df["close"].to_numpy()
    atr = (multiplier * calculate_atr(df, atr_period)).to_numpy()
    highest_close = df["close"].rolling(atr_period, min_periods=1).max().to_numpy()
    lowest_close = df["close"].rolling(atr_period, min_periods=1).min().to_numpy()

    n = len(df)
    long_stop = np.zeros(n)
    short_stop = np.zeros(n)
    direction = np.ones(n, dtype=int)

    for i in range(n):
        raw_long = highest_close[i] - atr[i]
        raw_short = lowest_close[i] + atr[i]
        if i == 0:
            long_stop[i] = raw_long
            short_stop[i] = raw_short
            direction[i] = 1
            continue

        long_stop_prev = long_stop[i - 1]
        short_stop_prev = short_stop[i - 1]
        long_stop[i] = max(raw_long, long_stop_prev) if close[i - 1] > long_stop_prev else raw_long
        short_stop[i] = min(raw_short, short_stop_prev) if close[i - 1] < short_stop_prev else raw_short

        if close[i] > short_stop_prev:
            direction[i] = 1
        elif close[i] < long_stop_prev:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    return pd.DataFrame(
        {
            "chandelier_long_stop": long_stop,
            "chandelier_short_stop": short_stop,
            "chandelier_direction": direction,
        },
        index=df.index,
    )


def calculate_pvt(df: pd.DataFrame) -> pd.Series:
    pct_change = df["close"].pct_change().fillna(0.0)
    return (pct_change * df["volume"]).cumsum().rename("pvt")


def calculate_pvt_signal(pvt: pd.Series, length: int, ma_type: str) -> pd.Series:
    if ma_type.upper() == "EMA":
        return pvt.ewm(span=length, adjust=False).mean()
    return pvt.rolling(length, min_periods=1).mean()


def find_pivot_highs(df: pd.DataFrame, swing_length: int) -> pd.Series:
    high = df["high"]
    n = len(df)
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(swing_length, n - swing_length):
        window = high.iloc[i - swing_length: i + swing_length + 1]
        if high.iloc[i] == window.max():
            result.iloc[i] = high.iloc[i]
    return result


def find_pivot_lows(df: pd.DataFrame, swing_length: int) -> pd.Series:
    low = df["low"]
    n = len(df)
    result = pd.Series(np.nan, index=df.index, dtype=float)
    for i in range(swing_length, n - swing_length):
        window = low.iloc[i - swing_length: i + swing_length + 1]
        if low.iloc[i] == window.min():
            result.iloc[i] = low.iloc[i]
    return result


def build_zones(
    df: pd.DataFrame,
    swing_length: int,
    atr_period: int,
    box_width: float,
    max_age_bars: int,
) -> list[dict]:
    atr = calculate_atr(df, atr_period)
    pivot_highs = find_pivot_highs(df, swing_length)
    pivot_lows = find_pivot_lows(df, swing_length)

    zones: list[dict] = []
    n = len(df)

    for i in range(n):
        atr_i = float(atr.iloc[i])
        atr_buffer = atr_i * (box_width / 10.0)

        if not np.isnan(pivot_highs.iloc[i]):
            top = float(pivot_highs.iloc[i])
            bottom = top - atr_buffer
            poi = (top + bottom) / 2.0
            overlap = any(
                z["type"] == "supply" and not z["bos"]
                and abs(poi - (z["top"] + z["bottom"]) / 2.0) <= 2 * atr_i
                for z in zones
            )
            if not overlap:
                zones.append({"type": "supply", "top": top, "bottom": bottom, "formed_index": i, "bos": False})

        if not np.isnan(pivot_lows.iloc[i]):
            bottom = float(pivot_lows.iloc[i])
            top = bottom + atr_buffer
            poi = (top + bottom) / 2.0
            overlap = any(
                z["type"] == "demand" and not z["bos"]
                and abs(poi - (z["top"] + z["bottom"]) / 2.0) <= 2 * atr_i
                for z in zones
            )
            if not overlap:
                zones.append({"type": "demand", "top": top, "bottom": bottom, "formed_index": i, "bos": False})

        close_i = float(df["close"].iloc[i])
        for z in zones:
            if z["bos"] or z["formed_index"] >= i:
                continue
            if z["type"] == "supply" and close_i >= z["top"]:
                z["bos"] = True
            elif z["type"] == "demand" and close_i <= z["bottom"]:
                z["bos"] = True

    latest_index = n - 1
    for z in zones:
        z["age_bars"] = latest_index - z["formed_index"]

    return [z for z in zones if z["age_bars"] <= max_age_bars]


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    TREND_TF, ENTRY_TF, TREND_KLINE_COUNT, ENTRY_KLINE_COUNT,
    CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER,
    PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE,
    RSI_FAST_PERIOD, RSI_SLOW_PERIOD,
    ENTRY_BUFFER_PCT,
    ZONE_SWING_LENGTH, ZONE_ATR_PERIOD, ZONE_BOX_WIDTH,
    ZONE_PROXIMITY_ATR_MULT, ZONE_MAX_AGE_BARS,
    SL_ATR_BUFFER_MULTIPLIER, LEVERAGE, TP_PRICE_PCT, MAX_SL_PRICE_PCT, MIN_RR,
    TREND_EMA_PERIOD, TREND_SUPERTREND_ATR_PERIOD, TREND_SUPERTREND_MULTIPLIER,
    ENABLE_BTC_FILTER, BTC_FILTER_SYMBOL, BTC_FILTER_TF,
    BTC_MAX_OPPOSING_MOVE_PCT, BTC_MAX_SINGLE_CANDLE_MOVE_PCT, BTC_MAX_THREE_CANDLE_MOVE_PCT,
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


def _detect_trigger(df_5m: pd.DataFrame) -> tuple[str | None, str, dict]:
    close = df_5m["close"]
    chand = calculate_chandelier_exit(df_5m, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df_5m)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(close, RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(close, RSI_SLOW_PERIOD)

    dir_last = int(chand["chandelier_direction"].iloc[-1])
    close_last = float(close.iloc[-1])
    prev_high = float(df_5m["high"].iloc[-2])
    prev_low = float(df_5m["low"].iloc[-2])
    pvt_last = float(pvt.iloc[-1])
    pvt_signal_last = float(pvt_signal.iloc[-1])
    rsi_fast_last = float(rsi_fast.iloc[-1])
    rsi_slow_last = float(rsi_slow.iloc[-1])

    details = {
        "close": close_last,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "pvt": pvt_last,
        "pvt_signal": pvt_signal_last,
        "rsi_fast": rsi_fast_last,
        "rsi_slow": rsi_slow_last,
        "chandelier_direction": dir_last,
    }

    if dir_last == 1:
        if not (pvt_last > pvt_signal_last):
            return None, "no PVT bullish momentum", details
        if not (rsi_fast_last > rsi_slow_last):
            return None, "RSI regime not bullish", details
        if not (close_last > prev_high * (1 + ENTRY_BUFFER_PCT)):
            return None, "no breakout confirmation", details
        return "LONG", "", details

    if not (pvt_last < pvt_signal_last):
        return None, "no PVT bearish momentum", details
    if not (rsi_fast_last < rsi_slow_last):
        return None, "RSI regime not bearish", details
    if not (close_last < prev_low * (1 - ENTRY_BUFFER_PCT)):
        return None, "no breakout confirmation", details
    return "SHORT", "", details


def _find_confluence_zone(
    zones: list[dict], direction: str, price: float, atr: float, proximity_mult: float
) -> dict | None:
    zone_type = "demand" if direction == "LONG" else "supply"
    tolerance = atr * proximity_mult
    candidates = [
        z for z in zones
        if z["type"] == zone_type and not z["bos"]
        and (z["bottom"] - tolerance) <= price <= (z["top"] + tolerance)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda z: z["formed_index"])


def _calculate_tp_sl(direction: str, entry: float, zone: dict, atr_zone: float) -> tuple[float, float] | None:
    if direction == "LONG":
        tp = entry * (1 + TP_PRICE_PCT)
        structural_sl = zone["bottom"] - atr_zone * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl >= entry:
            return None
        if (entry - structural_sl) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl
    else:
        tp = entry * (1 - TP_PRICE_PCT)
        structural_sl = zone["top"] + atr_zone * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl <= entry:
            return None
        if (structural_sl - entry) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl


def _score_candidate(direction: str, details: dict, zone: dict, rr: float) -> float:
    zone_mid = (zone["top"] + zone["bottom"]) / 2.0
    zone_half_width = max((zone["top"] - zone["bottom"]) / 2.0, 1e-9)
    proximity_ratio = min(1.0, abs(details["close"] - zone_mid) / zone_half_width)
    score = 25.0 * (1.0 - proximity_ratio)

    pvt_gap = abs(details["pvt"] - details["pvt_signal"])
    pvt_scale = max(abs(details["pvt_signal"]), 1.0)
    pvt_quality = min(1.0, pvt_gap / (pvt_scale * 0.05))
    score += 25.0 * pvt_quality

    if direction == "LONG":
        clearance = (details["close"] - details["prev_high"]) / details["prev_high"]
    else:
        clearance = (details["prev_low"] - details["close"]) / details["prev_low"]
    breakout_quality = min(1.0, max(0.0, clearance / (max(ENTRY_BUFFER_PCT, 1e-6) * 10)))
    score += 20.0 * breakout_quality

    rsi_fast = details["rsi_fast"]
    ideal_lo, ideal_hi = (55.0, 62.0) if direction == "LONG" else (38.0, 45.0)
    if ideal_lo <= rsi_fast <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi_fast - ideal_lo), abs(rsi_fast - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 10.0 * rsi_quality

    rr_quality = min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0 else (1.0 if rr >= MIN_RR else 0.0)
    score += 10.0 * rr_quality

    freshness = 1.0 - min(1.0, zone["age_bars"] / max(ZONE_MAX_AGE_BARS, 1))
    score += 10.0 * freshness

    return round(min(100.0, max(0.0, score)), 1)


def _reason_bucket(reason: str) -> str:
    """Collapse the free-text trigger reject reason into a stable category
    so scan-level rejects can be aggregated and counted."""
    if "PVT" in reason:
        return "no_pvt_momentum"
    if "RSI regime" in reason:
        return "no_rsi_regime"
    if "breakout confirmation" in reason:
        return "no_breakout_confirmation"
    return "trigger_other"


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def evaluate_symbol(
    symbol: str,
    btc_context: "BtcContext | None" = None,
    reject_sink: dict | None = None,
) -> Signal | None:
    try:
        raw_15m = get_market_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        raw_5m = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if raw_15m is None or raw_15m.empty or raw_5m is None or raw_5m.empty:
            logger.debug("[REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed_15m = raw_15m.iloc[:-1].copy()
        closed_5m = raw_5m.iloc[:-1].copy()

        if len(closed_15m) < ZONE_ATR_PERIOD + ZONE_SWING_LENGTH * 2 + 10:
            logger.debug("[REJECT] %s insufficient 15m candle history", symbol)
            _bump(reject_sink, "insufficient_history")
            return None
        if len(closed_5m) < RSI_SLOW_PERIOD + 20:
            logger.debug("[REJECT] %s insufficient 5m candle history", symbol)
            _bump(reject_sink, "insufficient_history")
            return None

        direction, reason, details = _detect_trigger(closed_5m)
        if direction is None:
            logger.debug("[REJECT] %s %s", symbol, reason)
            _bump(reject_sink, _reason_bucket(reason))
            return None

        zones = build_zones(closed_15m, ZONE_SWING_LENGTH, ZONE_ATR_PERIOD, ZONE_BOX_WIDTH, ZONE_MAX_AGE_BARS)
        atr_zone_last = float(calculate_atr(closed_15m, ZONE_ATR_PERIOD).iloc[-1])
        zone = _find_confluence_zone(zones, direction, details["close"], atr_zone_last, ZONE_PROXIMITY_ATR_MULT)
        if zone is None:
            logger.debug("[REJECT] %s no zone confluence", symbol)
            _bump(reject_sink, "no_zone_confluence")
            return None

        if ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context()
            if ctx is None:
                logger.debug("[REJECT] %s BTC context unavailable", symbol)
                _bump(reject_sink, "btc_context_unavailable")
                return None
            btc_ok, btc_reason = _btc_filter_ok(direction, ctx)
            if not btc_ok:
                logger.debug("[REJECT] %s %s %s", symbol, direction, btc_reason)
                _bump(reject_sink, "btc_filter")
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl(direction, entry, zone, atr_zone_last)
        if tp_sl is None:
            logger.debug("[REJECT] %s structural stop too wide", symbol)
            _bump(reject_sink, "stop_too_wide")
            return None
        tp, sl = tp_sl

        if not valid_trade_geometry(direction, entry, tp, sl):
            logger.debug("[REJECT] %s invalid trade geometry", symbol)
            _bump(reject_sink, "invalid_geometry")
            return None

        rr = _calc_rr(direction, entry, tp, sl)
        if rr < MIN_RR:
            logger.debug("[REJECT] %s RR %.2f below %.2f", symbol, rr, MIN_RR)
            _bump(reject_sink, "rr_below_min")
            return None

        tp_roi, sl_roi = _roi_pct(direction, entry, tp, sl)
        score = _score_candidate(direction, details, zone, rr)

        logger.info(
            "[CANDIDATE] %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
            symbol, direction, score, entry, tp, sl, rr,
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary="15m demand/supply zone + 5m Chandelier/PVT/RSI breakout",
            generated_at=datetime.now(timezone.utc),
            rr=round(rr, 2),
            score=score,
            entry_low=entry,
            entry_high=entry,
        )
    except Exception as e:
        logger.error("[EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None


# ── BTC market safety filter ─────────────────────────────────────────

def build_btc_context() -> BtcContext | None:
    df = get_market_klines(BTC_FILTER_SYMBOL, BTC_FILTER_TF, count=TREND_KLINE_COUNT)
    if df is None or df.empty:
        return None
    closed = df.iloc[:-1].copy()
    if len(closed) < TREND_EMA_PERIOD + 5:
        return None

    ema200 = calculate_ema(closed["close"], TREND_EMA_PERIOD)
    st = calculate_supertrend(closed, TREND_SUPERTREND_ATR_PERIOD, TREND_SUPERTREND_MULTIPLIER)

    latest_close = float(closed["close"].iloc[-1])
    previous_close = float(closed["close"].iloc[-2])
    close_three_bars_ago = float(closed["close"].iloc[-4])

    one_candle_move_pct = (latest_close - previous_close) / previous_close * 100.0
    three_candle_move_pct = (latest_close - close_three_bars_ago) / close_three_bars_ago * 100.0

    return BtcContext(
        close=latest_close,
        ema_200=float(ema200.iloc[-1]),
        supertrend_direction=int(st["supertrend_direction"].iloc[-1]),
        one_candle_move_pct=one_candle_move_pct,
        three_candle_move_pct=three_candle_move_pct,
    )


def _btc_filter_ok(direction: str, btc: BtcContext) -> tuple[bool, str]:
    if abs(btc.one_candle_move_pct) > BTC_MAX_SINGLE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"
    if abs(btc.three_candle_move_pct) > BTC_MAX_THREE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"

    if direction == "LONG":
        if not (
            btc.close > btc.ema_200
            and btc.supertrend_direction == 1
            and btc.three_candle_move_pct >= -BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bearish trend"
    else:
        if not (
            btc.close < btc.ema_200
            and btc.supertrend_direction == -1
            and btc.three_candle_move_pct <= BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bullish trend"

    return True, ""
