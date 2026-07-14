"""
Simple Supertrend Pullback v1.

15m trend (EMA200 + Supertrend) gates direction; 5m EMA20 pullback +
reclaim + RSI + volume + candle-quality confirms entry. Only completed
candles are ever used. See docs/superpowers/specs/2026-07-15-supertrend-pullback-v1-design.md.
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


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    TREND_TF, ENTRY_TF, TREND_KLINE_COUNT, ENTRY_KLINE_COUNT,
    TREND_EMA_PERIOD, ENTRY_EMA_PERIOD,
    RSI_PERIOD, RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX,
    ATR_PERIOD,
    TREND_SUPERTREND_ATR_PERIOD, TREND_SUPERTREND_MULTIPLIER,
    ENTRY_SUPERTREND_ATR_PERIOD, ENTRY_SUPERTREND_MULTIPLIER,
    VOLUME_MA_PERIOD, MIN_VOLUME_MULTIPLIER,
    PULLBACK_LOOKBACK_BARS, MAX_EMA_DISTANCE_PCT, MAX_CONFIRMATION_CANDLE_ATR,
    SL_ATR_BUFFER_MULTIPLIER, LEVERAGE, TP_PRICE_PCT, MAX_SL_PRICE_PCT, MIN_RR,
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


def _ema_slope_ok(ema: pd.Series, direction: str, tolerance: float = 1e-9) -> bool:
    current = ema.iloc[-1]
    three_bars_ago = ema.iloc[-4]
    if direction == "LONG":
        return current >= three_bars_ago - tolerance
    return current <= three_bars_ago + tolerance


def _detect_trend(df_15m: pd.DataFrame) -> str | None:
    ema200 = calculate_ema(df_15m["close"], TREND_EMA_PERIOD)
    st = calculate_supertrend(df_15m, TREND_SUPERTREND_ATR_PERIOD, TREND_SUPERTREND_MULTIPLIER)
    close = float(df_15m["close"].iloc[-1])
    st_dir = int(st["supertrend_direction"].iloc[-1])

    if close > float(ema200.iloc[-1]) and st_dir == 1 and _ema_slope_ok(ema200, "LONG"):
        return "LONG"
    if close < float(ema200.iloc[-1]) and st_dir == -1 and _ema_slope_ok(ema200, "SHORT"):
        return "SHORT"
    return None


def _detect_pullback_and_confirmation(df_5m: pd.DataFrame, direction: str) -> tuple[bool, str, dict]:
    """
    Indexing convention (df_5m already closed-candles-only, so -1 is the
    latest COMPLETED candle):
      -1        confirmation candle
      -4..-2    PULLBACK_LOOKBACK_BARS prior completed candles (pullback window)
      -5        candle immediately before the pullback window, used to
                confirm price was already on the correct side of EMA20
                before the pullback began
    """
    ema20 = calculate_ema(df_5m["close"], ENTRY_EMA_PERIOD)
    rsi = calculate_rsi(df_5m["close"], RSI_PERIOD)
    atr = calculate_atr(df_5m, ATR_PERIOD)
    st = calculate_supertrend(df_5m, ENTRY_SUPERTREND_ATR_PERIOD, ENTRY_SUPERTREND_MULTIPLIER)

    close = float(df_5m["close"].iloc[-1])
    open_ = float(df_5m["open"].iloc[-1])
    high = float(df_5m["high"].iloc[-1])
    low = float(df_5m["low"].iloc[-1])
    ema20_last = float(ema20.iloc[-1])
    rsi_last = float(rsi.iloc[-1])
    atr_last = float(atr.iloc[-1])
    st_dir_last = int(st["supertrend_direction"].iloc[-1])
    vol_last = float(df_5m["volume"].iloc[-1])
    vol_avg = float(df_5m["volume"].iloc[-(VOLUME_MA_PERIOD + 1):-1].mean())

    pullback_lows = df_5m["low"].iloc[-(PULLBACK_LOOKBACK_BARS + 1):-1]
    pullback_highs = df_5m["high"].iloc[-(PULLBACK_LOOKBACK_BARS + 1):-1]
    pullback_ema = ema20.iloc[-(PULLBACK_LOOKBACK_BARS + 1):-1]
    pre_pullback_close = float(df_5m["close"].iloc[-(PULLBACK_LOOKBACK_BARS + 2)])
    pre_pullback_ema = float(ema20.iloc[-(PULLBACK_LOOKBACK_BARS + 2)])

    rsi_min, rsi_max = (RSI_LONG_MIN, RSI_LONG_MAX) if direction == "LONG" else (RSI_SHORT_MIN, RSI_SHORT_MAX)

    if direction == "LONG":
        if pre_pullback_close <= pre_pullback_ema:
            return False, "no prior uptrend above EMA20 before pullback", {}
        if not bool((pullback_lows <= pullback_ema).any()):
            return False, "no EMA20 pullback", {}
        if not (close > ema20_last):
            return False, "confirmation candle did not reclaim EMA20", {}
        if not (close > open_):
            return False, "confirmation candle not bullish", {}
        if st_dir_last != 1:
            return False, "5m supertrend not bullish", {}
    else:
        if pre_pullback_close >= pre_pullback_ema:
            return False, "no prior downtrend below EMA20 before pullback", {}
        if not bool((pullback_highs >= pullback_ema).any()):
            return False, "no EMA20 pullback", {}
        if not (close < ema20_last):
            return False, "confirmation candle did not reclaim EMA20", {}
        if not (close < open_):
            return False, "confirmation candle not bearish", {}
        if st_dir_last != -1:
            return False, "5m supertrend not bearish", {}

    if not (rsi_min <= rsi_last <= rsi_max):
        return False, f"RSI {rsi_last:.1f} outside {direction.lower()} range", {}
    if vol_avg <= 0 or not (vol_last >= MIN_VOLUME_MULTIPLIER * vol_avg):
        ratio = (vol_last / vol_avg) if vol_avg else 0.0
        return False, f"volume ratio {ratio:.2f} below {MIN_VOLUME_MULTIPLIER}", {}

    candle_range = high - low
    if atr_last <= 0 or candle_range > MAX_CONFIRMATION_CANDLE_ATR * atr_last:
        return False, f"confirmation candle {candle_range / atr_last if atr_last else float('inf'):.2f} ATR", {}

    if direction == "LONG":
        distance_from_ema_pct = (close - ema20_last) / close
    else:
        distance_from_ema_pct = (ema20_last - close) / close
    if distance_from_ema_pct > MAX_EMA_DISTANCE_PCT:
        return False, f"price {distance_from_ema_pct * 100:.2f}% from EMA20 (chasing)", {}

    details = {
        "close": close,
        "ema20": ema20_last,
        "rsi": rsi_last,
        "atr": atr_last,
        "volume_ratio": vol_last / vol_avg if vol_avg else 0.0,
        "recent_lows": pullback_lows,
        "recent_highs": pullback_highs,
    }
    return True, "", details


def _calculate_tp_sl(direction: str, entry: float, details: dict) -> tuple[float, float] | None:
    atr_last = details["atr"]
    if direction == "LONG":
        tp = entry * (1 + TP_PRICE_PCT)
        recent_low = float(details["recent_lows"].min())
        structural_sl = recent_low - atr_last * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl >= entry:
            return None
        if (entry - structural_sl) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl
    else:
        tp = entry * (1 - TP_PRICE_PCT)
        recent_high = float(details["recent_highs"].max())
        structural_sl = recent_high + atr_last * SL_ATR_BUFFER_MULTIPLIER
        if structural_sl <= entry:
            return None
        if (structural_sl - entry) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl


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


def _score_candidate(direction: str, details: dict, rr: float) -> float:
    score = 25.0  # 15m trend alignment -- already gated true/false upstream
    score += 20.0  # 5m Supertrend alignment -- already gated

    distance_pct = abs(details["close"] - details["ema20"]) / details["close"]
    reclaim_quality = max(0.0, 1.0 - (distance_pct / MAX_EMA_DISTANCE_PCT))
    score += 20.0 * reclaim_quality

    vol_ratio = details["volume_ratio"]
    vol_quality = min(1.0, max(0.0, (vol_ratio - MIN_VOLUME_MULTIPLIER) / (2.0 - MIN_VOLUME_MULTIPLIER)))
    score += 15.0 * vol_quality

    rsi = details["rsi"]
    ideal_lo, ideal_hi = (55.0, 62.0) if direction == "LONG" else (38.0, 45.0)
    if ideal_lo <= rsi <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi - ideal_lo), abs(rsi - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 10.0 * rsi_quality

    rr_quality = min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0 else (1.0 if rr >= MIN_RR else 0.0)
    score += 10.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)


def evaluate_symbol(symbol: str, btc_context: "BtcContext | None" = None) -> Signal | None:
    try:
        raw_15m = get_market_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        raw_5m = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if raw_15m is None or raw_15m.empty or raw_5m is None or raw_5m.empty:
            logger.debug("[REJECT] %s missing candle data", symbol)
            return None

        closed_15m = raw_15m.iloc[:-1].copy()
        closed_5m = raw_5m.iloc[:-1].copy()

        if len(closed_15m) < TREND_EMA_PERIOD + 5:
            logger.debug("[REJECT] %s insufficient 15m candle history", symbol)
            return None
        if len(closed_5m) < ENTRY_EMA_PERIOD + PULLBACK_LOOKBACK_BARS + 10:
            logger.debug("[REJECT] %s insufficient 5m candle history", symbol)
            return None

        direction = _detect_trend(closed_15m)
        if direction is None:
            logger.debug("[REJECT] %s no 15m trend", symbol)
            return None

        ok, reason, details = _detect_pullback_and_confirmation(closed_5m, direction)
        if not ok:
            logger.debug("[REJECT] %s %s", symbol, reason)
            return None

        if ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context()
            if ctx is None:
                logger.debug("[REJECT] %s BTC context unavailable", symbol)
                return None
            btc_ok, btc_reason = _btc_filter_ok(direction, ctx)
            if not btc_ok:
                logger.debug("[REJECT] %s %s %s", symbol, direction, btc_reason)
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl(direction, entry, details)
        if tp_sl is None:
            logger.debug("[REJECT] %s structural stop too wide", symbol)
            return None
        tp, sl = tp_sl

        if not valid_trade_geometry(direction, entry, tp, sl):
            logger.debug("[REJECT] %s invalid trade geometry", symbol)
            return None

        rr = _calc_rr(direction, entry, tp, sl)
        if rr < MIN_RR:
            logger.debug("[REJECT] %s RR %.2f below %.2f", symbol, rr, MIN_RR)
            return None

        tp_roi, sl_roi = _roi_pct(direction, entry, tp, sl)
        score = _score_candidate(direction, details, rr)

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
            timeframe_summary=f"15m {direction.lower()} trend + 5m EMA20 pullback reclaim",
            generated_at=datetime.now(timezone.utc),
            rr=round(rr, 2),
            score=score,
            entry_low=entry,
            entry_high=entry,
        )
    except Exception as e:
        logger.error("[EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
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
