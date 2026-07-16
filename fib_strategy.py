"""
Fibonacci Multi-Timeframe Strategy (backtest research only).

4H trend (EMA200 + slope) gates direction; 1H fractal-swing Fibonacci
retracement into the 0.5-0.618 golden zone + rejection candle + RSI +
volume confirms entry. TP at a Fibonacci extension of the impulse leg, SL
just beyond the leg's origin swing point. Only completed candles are ever
used. Fully standalone from strategy.py -- see
docs/superpowers/specs/2026-07-16-fib-mtf-strategy-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class FibLeg:
    leg_start: float
    leg_end: float
    leg_start_idx: int
    leg_end_idx: int


@dataclass
class FibSignal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    rr: float
    score: float
    leg_start: float
    leg_end: float
    zone_lower: float
    zone_upper: float
    generated_at: datetime


@dataclass
class BtcContext4h:
    close: float
    ema_200: float
    one_candle_move_pct: float
    three_candle_move_pct: float


# ── swing + fibonacci math ────────────────────────────────────────────

def find_swings(df: pd.DataFrame, fractal_bars: int) -> pd.DataFrame:
    """Marks bars whose high/low is the max/min among itself and
    `fractal_bars // 2` bars on each side (ties count as swings -- the
    comparison uses `==` against the window extreme, not strict
    uniqueness). A swing at index i only becomes
    knowable once the `half` bars after it have closed -- naturally
    lookahead-safe as long as this is only ever called on already-completed
    candles."""
    half = fractal_bars // 2
    n = len(df)
    swing_high = [False] * n
    swing_low = [False] * n
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    for i in range(half, n - half):
        window_high = highs[i - half: i + half + 1]
        window_low = lows[i - half: i + half + 1]
        if highs[i] == window_high.max():
            swing_high[i] = True
        if lows[i] == window_low.min():
            swing_low[i] = True

    result = df.copy()
    result["swing_high"] = swing_high
    result["swing_low"] = swing_low
    return result


def find_last_impulse_leg(df_with_swings: pd.DataFrame, direction: str, lookback_bars: int) -> FibLeg | None:
    """Anchors on the most recent swing extreme in the trend direction
    (swing high for LONG, swing low for SHORT), then pairs it with the most
    recent swing of the opposite kind before it. Anchoring on the most
    recent swing HIGH (not "most recent swing low") makes this robust to a
    retracement itself creating a new, more-recent minor swing low after
    the leg's true end -- that new low is simply ignored since we never
    search for swing lows after the anchor point."""
    window = df_with_swings.iloc[-lookback_bars:] if len(df_with_swings) > lookback_bars else df_with_swings
    swing_high_positions = np.flatnonzero(window["swing_high"].to_numpy())
    swing_low_positions = np.flatnonzero(window["swing_low"].to_numpy())

    if len(swing_high_positions) == 0 or len(swing_low_positions) == 0:
        return None

    if direction == "LONG":
        end_pos = swing_high_positions[-1]
        candidates = swing_low_positions[swing_low_positions < end_pos]
        if len(candidates) == 0:
            return None
        start_pos = candidates[-1]
        leg_start = float(window["low"].iloc[start_pos])
        leg_end = float(window["high"].iloc[end_pos])
    else:
        end_pos = swing_low_positions[-1]
        candidates = swing_high_positions[swing_high_positions < end_pos]
        if len(candidates) == 0:
            return None
        start_pos = candidates[-1]
        leg_start = float(window["high"].iloc[start_pos])
        leg_end = float(window["low"].iloc[end_pos])

    start_idx = df_with_swings.index.get_loc(window.index[start_pos])
    end_idx = df_with_swings.index.get_loc(window.index[end_pos])
    return FibLeg(leg_start=leg_start, leg_end=leg_end, leg_start_idx=int(start_idx), leg_end_idx=int(end_idx))


def fib_retracement_price(leg_start: float, leg_end: float, direction: str, level: float) -> float:
    """`level` in [0, 1]: 0 = leg_end (no retracement), 1 = leg_start (full
    retracement back to the leg's origin)."""
    if direction == "LONG":
        return leg_end - (leg_end - leg_start) * level
    return leg_end + (leg_start - leg_end) * level


def fib_extension_price(leg_start: float, leg_end: float, direction: str, ratio: float) -> float:
    """`ratio` > 1 projects the leg_start->leg_end move beyond leg_end in
    the trend-continuation direction, measured from leg_start (standard
    Fibonacci extension convention)."""
    span = leg_end - leg_start
    return leg_start + span * ratio


# ── trend filter + entry confirmation ─────────────────────────────────

from strategy import calculate_ema, calculate_rsi, calculate_atr
import config_fib
from market_data import get_market_klines


def _ema_slope_ok(ema: pd.Series, direction: str, tolerance: float = 1e-9) -> bool:
    current = ema.iloc[-1]
    three_bars_ago = ema.iloc[-4]
    if direction == "LONG":
        return current >= three_bars_ago - tolerance
    return current <= three_bars_ago + tolerance


def _detect_trend_4h(df_4h: pd.DataFrame) -> str | None:
    ema200 = calculate_ema(df_4h["close"], config_fib.TREND_EMA_PERIOD)
    close = float(df_4h["close"].iloc[-1])

    if close > float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "LONG"):
        return "LONG"
    if close < float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "SHORT"):
        return "SHORT"
    return None


def _detect_fib_entry(df_1h: pd.DataFrame, direction: str) -> tuple[bool, str, dict]:
    rsi = calculate_rsi(df_1h["close"], config_fib.RSI_PERIOD)
    atr = calculate_atr(df_1h, config_fib.ATR_PERIOD)

    swings = find_swings(df_1h, config_fib.SWING_FRACTAL_BARS)
    leg = find_last_impulse_leg(swings, direction, config_fib.SWING_LOOKBACK_BARS)
    if leg is None:
        return False, "no valid impulse leg found", {}

    price_a = fib_retracement_price(leg.leg_start, leg.leg_end, direction, config_fib.ZONE_LOWER)
    price_b = fib_retracement_price(leg.leg_start, leg.leg_end, direction, config_fib.ZONE_UPPER)
    zone_low, zone_high = min(price_a, price_b), max(price_a, price_b)

    recent = df_1h.iloc[-(config_fib.ZONE_LOOKBACK_BARS + 1):-1]
    touched = bool(((recent["low"] <= zone_high) & (recent["high"] >= zone_low)).any())
    if not touched:
        return False, "no zone touch", {}

    # Breakout candle: within the last CONFIRMATION_LOOKBACK_BARS candles
    # (including the latest), at least one candle closed back out of the zone
    # in the trend direction AND was itself the right color -- this is the
    # actual reversal/rejection event, and it no longer has to be the exact
    # latest bar (real breakouts often take a candle or two to fully clear
    # the zone before the market "confirms" it).
    breakout_window = df_1h.iloc[-config_fib.CONFIRMATION_LOOKBACK_BARS:]
    if direction == "LONG":
        breakout_occurred = bool(
            ((breakout_window["close"] > zone_high) & (breakout_window["close"] > breakout_window["open"])).any()
        )
    else:
        breakout_occurred = bool(
            ((breakout_window["close"] < zone_low) & (breakout_window["close"] < breakout_window["open"])).any()
        )
    if not breakout_occurred:
        return False, "no confirmation candle closed back out of zone within lookback", {}

    close = float(df_1h["close"].iloc[-1])
    open_ = float(df_1h["open"].iloc[-1])
    rsi_last = float(rsi.iloc[-1])
    atr_last = float(atr.iloc[-1])
    vol_last = float(df_1h["volume"].iloc[-1])
    vol_avg = float(df_1h["volume"].iloc[-(config_fib.VOLUME_MA_PERIOD + 1):-1].mean())

    # The current (latest) candle doesn't have to be a fresh breakout itself
    # -- that already happened within the lookback window above -- but price
    # must still be holding beyond the zone level now, not have fallen back
    # into it.
    if direction == "LONG":
        if not (close > zone_high):
            return False, "price fell back into zone before confirmation held", {}
    else:
        if not (close < zone_low):
            return False, "price fell back into zone before confirmation held", {}

    rsi_min, rsi_max = (
        (config_fib.RSI_LONG_MIN, config_fib.RSI_LONG_MAX) if direction == "LONG"
        else (config_fib.RSI_SHORT_MIN, config_fib.RSI_SHORT_MAX)
    )
    if not (rsi_min <= rsi_last <= rsi_max):
        return False, f"RSI {rsi_last:.1f} outside {direction.lower()} range", {}

    if vol_avg <= 0 or not (vol_last >= config_fib.MIN_VOLUME_MULTIPLIER * vol_avg):
        ratio = (vol_last / vol_avg) if vol_avg else 0.0
        return False, f"volume ratio {ratio:.2f} below {config_fib.MIN_VOLUME_MULTIPLIER}", {}

    details = {
        "close": close,
        "leg_start": leg.leg_start,
        "leg_end": leg.leg_end,
        "zone_low": zone_low,
        "zone_high": zone_high,
        "rsi": rsi_last,
        "atr": atr_last,
        "volume_ratio": vol_last / vol_avg if vol_avg else 0.0,
    }
    return True, "", details


# ── trade geometry, TP/SL, RR, scoring ────────────────────────────────

def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    return tp < entry < sl


def _calc_rr(entry: float, tp: float, sl: float) -> float:
    reward = abs(tp - entry)
    risk = abs(entry - sl)
    return reward / risk if risk > 0 else 0.0


def _calculate_tp_sl_fib(direction: str, entry: float, details: dict) -> tuple[float, float] | None:
    leg_start = details["leg_start"]
    leg_end = details["leg_end"]
    atr_last = details["atr"]

    tp = fib_extension_price(leg_start, leg_end, direction, config_fib.TP_EXTENSION_LEVEL)

    if direction == "LONG":
        sl = leg_start - atr_last * config_fib.SL_ATR_BUFFER_MULTIPLIER
        if sl >= entry:
            return None
        if (entry - sl) / entry > config_fib.MAX_SL_PRICE_PCT:
            return None
    else:
        sl = leg_start + atr_last * config_fib.SL_ATR_BUFFER_MULTIPLIER
        if sl <= entry:
            return None
        if (sl - entry) / entry > config_fib.MAX_SL_PRICE_PCT:
            return None

    return tp, sl


def _score_candidate_fib(direction: str, details: dict, rr: float) -> float:
    score = 30.0  # 4H trend alignment -- already gated true/false upstream

    zone_mid = (details["zone_low"] + details["zone_high"]) / 2.0
    zone_span = details["zone_high"] - details["zone_low"]
    precision = 1.0 - min(1.0, abs(details["close"] - zone_mid) / zone_span) if zone_span > 0 else 0.5
    score += 20.0 * precision

    vol_ratio = details["volume_ratio"]
    vol_quality = min(1.0, max(0.0, (vol_ratio - config_fib.MIN_VOLUME_MULTIPLIER) / (2.0 - config_fib.MIN_VOLUME_MULTIPLIER))) if config_fib.MIN_VOLUME_MULTIPLIER < 2.0 else (1.0 if vol_ratio >= config_fib.MIN_VOLUME_MULTIPLIER else 0.0)
    score += 15.0 * vol_quality

    rsi = details["rsi"]
    ideal_lo, ideal_hi = (55.0, 62.0) if direction == "LONG" else (38.0, 45.0)
    if ideal_lo <= rsi <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi - ideal_lo), abs(rsi - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 15.0 * rsi_quality

    rr_quality = min(1.0, max(0.0, (rr - config_fib.MIN_RR) / (2.0 - config_fib.MIN_RR))) if config_fib.MIN_RR < 2.0 else (1.0 if rr >= config_fib.MIN_RR else 0.0)
    score += 20.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)


# ── BTC 4H market-safety filter ────────────────────────────────────────

def build_btc_context_4h() -> BtcContext4h | None:
    df = get_market_klines(config_fib.BTC_FILTER_SYMBOL, config_fib.BTC_FILTER_TF, count=config_fib.TREND_KLINE_COUNT)
    if df is None or df.empty:
        return None
    closed = df.iloc[:-1].copy()
    if len(closed) < config_fib.TREND_EMA_PERIOD + 5:
        return None

    ema200 = calculate_ema(closed["close"], config_fib.TREND_EMA_PERIOD)

    latest_close = float(closed["close"].iloc[-1])
    previous_close = float(closed["close"].iloc[-2])
    close_three_bars_ago = float(closed["close"].iloc[-4])

    one_candle_move_pct = (latest_close - previous_close) / previous_close * 100.0
    three_candle_move_pct = (latest_close - close_three_bars_ago) / close_three_bars_ago * 100.0

    return BtcContext4h(
        close=latest_close,
        ema_200=float(ema200.iloc[-1]),
        one_candle_move_pct=one_candle_move_pct,
        three_candle_move_pct=three_candle_move_pct,
    )


def _btc_filter_ok_4h(direction: str, btc: BtcContext4h) -> tuple[bool, str]:
    if abs(btc.one_candle_move_pct) > config_fib.BTC_MAX_SINGLE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"
    if abs(btc.three_candle_move_pct) > config_fib.BTC_MAX_THREE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"

    if direction == "LONG":
        if not (
            btc.close > btc.ema_200
            and btc.three_candle_move_pct >= -config_fib.BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bearish trend"
    else:
        if not (
            btc.close < btc.ema_200
            and btc.three_candle_move_pct <= config_fib.BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bullish trend"

    return True, ""


# ── public entry point ─────────────────────────────────────────────────

def _reason_bucket(reason: str) -> str:
    if reason == "no valid impulse leg found":
        return "no_swing_leg"
    if reason == "no zone touch":
        return "no_zone_touch"
    if "no confirmation candle closed back out of zone" in reason:
        return "no_confirmation"
    if "fell back into zone" in reason:
        return "confirmation_not_held"
    if reason.startswith("RSI"):
        return "rsi_out_of_band"
    if reason.startswith("volume ratio"):
        return "low_volume"
    return "fib_other"


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def evaluate_symbol_fib(
    symbol: str,
    btc_context: "BtcContext4h | None" = None,
    reject_sink: dict | None = None,
) -> FibSignal | None:
    try:
        raw_4h = get_market_klines(symbol, config_fib.TREND_TF, count=config_fib.TREND_KLINE_COUNT)
        raw_1h = get_market_klines(symbol, config_fib.ENTRY_TF, count=config_fib.ENTRY_KLINE_COUNT)

        if raw_4h is None or raw_4h.empty or raw_1h is None or raw_1h.empty:
            logger.debug("[FIB-REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed_4h = raw_4h.iloc[:-1].copy()
        closed_1h = raw_1h.iloc[:-1].copy()

        if len(closed_4h) < config_fib.TREND_EMA_PERIOD + 5:
            _bump(reject_sink, "insufficient_history")
            return None
        min_1h_needed = config_fib.SWING_LOOKBACK_BARS + config_fib.SWING_FRACTAL_BARS + config_fib.VOLUME_MA_PERIOD + 10
        if len(closed_1h) < min_1h_needed:
            _bump(reject_sink, "insufficient_history")
            return None

        direction = _detect_trend_4h(closed_4h)
        if direction is None:
            logger.debug("[FIB-REJECT] %s no 4H trend", symbol)
            _bump(reject_sink, "no_trend")
            return None

        ok, reason, details = _detect_fib_entry(closed_1h, direction)
        if not ok:
            logger.debug("[FIB-REJECT] %s %s", symbol, reason)
            _bump(reject_sink, _reason_bucket(reason))
            return None

        if config_fib.ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context_4h()
            if ctx is None:
                _bump(reject_sink, "btc_context_unavailable")
                return None
            btc_ok, btc_reason = _btc_filter_ok_4h(direction, ctx)
            if not btc_ok:
                logger.debug("[FIB-REJECT] %s %s %s", symbol, direction, btc_reason)
                _bump(reject_sink, "btc_filter")
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl_fib(direction, entry, details)
        if tp_sl is None:
            _bump(reject_sink, "stop_too_wide")
            return None
        tp, sl = tp_sl

        if not _valid_trade_geometry(direction, entry, tp, sl):
            _bump(reject_sink, "invalid_geometry")
            return None

        rr = _calc_rr(entry, tp, sl)
        if rr < config_fib.MIN_RR:
            _bump(reject_sink, "rr_below_min")
            return None

        score = _score_candidate_fib(direction, details, rr)

        logger.info(
            "[FIB-CANDIDATE] %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
            symbol, direction, score, entry, tp, sl, rr,
        )

        return FibSignal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            leverage=config_fib.LEVERAGE,
            rr=round(rr, 2),
            score=score,
            leg_start=details["leg_start"],
            leg_end=details["leg_end"],
            zone_lower=details["zone_low"],
            zone_upper=details["zone_high"],
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        logger.error("[FIB-EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
