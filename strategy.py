"""
Ribbon-Flip Trend-Bar Confirmation v1.

A 6-EMA ribbon (30/35/40/45/50 vs a 60-period baseline) flipping fully
bullish or bearish is "arrow 1"; a Price-Action-Channel Trend Bar
confirming the same direction within RIBBON_LOOKBACK_BARS of that flip is
"arrow 2". If the ribbon reverts before the Trend Bar confirms, the setup
is invalidated -- recomputed fresh every scan, no persisted arm state.
Only completed candles are ever used. See
docs/superpowers/specs/2026-07-29-ribbon-trendbar-confirmation-design.md.

The structural SL (swing since the ribbon flip + a small ATR buffer) is
floored at SL_FLOOR_ATR_MULT x ATR so it's never tighter than normal
15m candle noise. LONG signals can be disabled via ENABLE_LONG_SIGNALS
(true by default) -- LONG underperformed SHORT in every backtest
configuration tested; set to false to run SHORT-only, as backtesting
recommends. The last closed candle must be at least
MIN_CANDLE_SETTLE_SECONDS old before it's used -- MEXC's kline REST data
for a just-closed candle can still get revised shortly after close.
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


def calculate_ema_ribbon(
    df: pd.DataFrame, lengths: tuple[int, int, int, int, int], baseline_length: int
) -> pd.DataFrame:
    close = df["close"]
    ma1, ma2, ma3, ma4, ma5 = (calculate_ema(close, length) for length in lengths)
    baseline = calculate_ema(close, baseline_length)
    return pd.DataFrame(
        {"ma1": ma1, "ma2": ma2, "ma3": ma3, "ma4": ma4, "ma5": ma5, "baseline": baseline},
        index=df.index,
    )


def calculate_pvt(df: pd.DataFrame) -> pd.Series:
    close = df["close"]
    volume = df["volume"]
    pct_change = close.pct_change()
    return (pct_change * volume).fillna(0.0).cumsum()


def calculate_pvt_signal(pvt: pd.Series, length: int, ma_type: str) -> pd.Series:
    if ma_type.upper() == "EMA":
        return pvt.ewm(span=length, adjust=False).mean()
    return pvt.rolling(window=length, min_periods=1).mean()


def calculate_chandelier_direction(
    df: pd.DataFrame, atr_period: int, multiplier: float
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Chandelier Exit direction flip, ported from the Pine script's
    calculation() function's longStop/shortStop/dir recursion. Returns
    (direction, long_stop_prev, short_stop_prev) where the *_prev series
    hold each bar's stop level as it stood going INTO that bar -- what
    the Pine script calls longStopPrev/shortStopPrev, which is what the
    BUY/SELL trigger and the direction flip itself both compare against,
    never the same bar's just-updated stop."""
    close = df["close"]
    atr = calculate_atr(df, atr_period) * multiplier
    highest_close = close.rolling(window=atr_period, min_periods=1).max()
    lowest_close = close.rolling(window=atr_period, min_periods=1).min()

    raw_long = (highest_close - atr).to_numpy()
    raw_short = (lowest_close + atr).to_numpy()
    close_v = close.to_numpy()
    n = len(df)

    long_stop = np.zeros(n)
    short_stop = np.zeros(n)
    direction = np.ones(n, dtype=int)

    long_stop[0] = raw_long[0]
    short_stop[0] = raw_short[0]

    for i in range(1, n):
        long_stop_prev = long_stop[i - 1]
        short_stop_prev = short_stop[i - 1]

        long_stop[i] = (
            max(raw_long[i], long_stop_prev) if close_v[i - 1] > long_stop_prev else raw_long[i]
        )
        short_stop[i] = (
            min(raw_short[i], short_stop_prev) if close_v[i - 1] < short_stop_prev else raw_short[i]
        )

        if close_v[i] > short_stop_prev:
            direction[i] = 1
        elif close_v[i] < long_stop_prev:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    direction_s = pd.Series(direction, index=df.index)
    long_stop_prev_s = pd.Series(long_stop, index=df.index).shift(1).bfill()
    short_stop_prev_s = pd.Series(short_stop, index=df.index).shift(1).bfill()
    return direction_s, long_stop_prev_s, short_stop_prev_s


def calculate_ema200(df: pd.DataFrame, length: int) -> pd.Series:
    return calculate_ema(df["close"], length)


def calculate_daily_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tp_vol = typical * df["volume"]
    day = df.index.normalize()
    cum_tp_vol = tp_vol.groupby(day).cumsum()
    cum_vol = df["volume"].groupby(day).cumsum()
    return cum_tp_vol / cum_vol.replace(0.0, np.nan)


def calculate_binocular_trigger(df: pd.DataFrame) -> pd.DataFrame:
    direction, _, _ = calculate_chandelier_direction(df, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(df["close"], RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(df["close"], RSI_SLOW_PERIOD)

    buy = (direction == 1) & (pvt > pvt_signal) & (rsi_fast > rsi_slow)
    sell = (direction == -1) & (pvt < pvt_signal) & (rsi_fast < rsi_slow)
    return pd.DataFrame({"buy": buy, "sell": sell}, index=df.index)


def detect_transition(trigger: pd.DataFrame) -> str | None:
    if len(trigger) < 2:
        return None
    buy_now, buy_prev = bool(trigger["buy"].iloc[-1]), bool(trigger["buy"].iloc[-2])
    sell_now, sell_prev = bool(trigger["sell"].iloc[-1]), bool(trigger["sell"].iloc[-2])
    if buy_now and not buy_prev:
        return "LONG"
    if sell_now and not sell_prev:
        return "SHORT"
    return None


def calculate_trend_bar(df: pd.DataFrame, pac_length: int) -> pd.Series:
    pac_hi = calculate_ema(df["high"], pac_length)
    pac_lo = calculate_ema(df["low"], pac_length)
    high, low = df["high"], df["low"]

    color = pd.Series("gray", index=df.index, dtype=object)
    color[(low > pac_hi) & (high > pac_hi)] = "green"
    color[(high < pac_lo) & (low < pac_lo)] = "red"
    return color


def _detect_ribbon_flip(
    df: pd.DataFrame,
    lengths: tuple[int, int, int, int, int],
    baseline_length: int,
    lookback_bars: int,
) -> tuple[str | None, int | None]:
    ribbon = calculate_ema_ribbon(df, lengths, baseline_length)
    ma1, ma2, ma3, ma4, ma5, baseline = (
        ribbon["ma1"], ribbon["ma2"], ribbon["ma3"], ribbon["ma4"], ribbon["ma5"], ribbon["baseline"]
    )
    bullish = (ma1 > baseline) & (ma2 > baseline) & (ma3 > baseline) & (ma4 > baseline) & (ma5 > baseline)
    bearish = (ma1 < baseline) & (ma2 < baseline) & (ma3 < baseline) & (ma4 < baseline) & (ma5 < baseline)

    n = len(df)
    last = n - 1
    stop = max(last - lookback_bars, 0)

    if bool(bullish.iloc[last]):
        for j in range(last, stop - 1, -1):
            if not bool(bullish.iloc[j]):
                break
            if j == 0 or not bool(bullish.iloc[j - 1]):
                return "LONG", j
        return None, None

    if bool(bearish.iloc[last]):
        for j in range(last, stop - 1, -1):
            if not bool(bearish.iloc[j]):
                break
            if j == 0 or not bool(bearish.iloc[j - 1]):
                return "SHORT", j
        return None, None

    return None, None


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, CANDLE_MINUTES,
    RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN,
    RIBBON_BASELINE_LEN, ATR_PERIOD, MIN_CANDLE_SETTLE_SECONDS,
    LEVERAGE, MAX_SL_PRICE_PCT, MIN_RR, ENABLE_LONG_SIGNALS,
    SIGNAL_MODE, CONFIRMATION_TIMEFRAMES, MTF_MIN_CONFIRMATIONS,
    ACCOUNT_BALANCE, RISK_PERCENT_PER_TRADE,
    PVT_SIGNAL_TYPE, PVT_SIGNAL_LENGTH, RSI_FAST_PERIOD, RSI_SLOW_PERIOD,
    CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER, BINOCULAR_EMA200_LEN,
    ENTRY_BUFFER_PCT, PENDING_SIGNAL_EXPIRY_CANDLES,
    TARGET1_CLOSE_FRACTION, TARGET2_CLOSE_FRACTION, TARGET3_CLOSE_FRACTION,
    MOVE_SL_TO_BREAKEVEN_AFTER_T1,
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


def _calculate_tp_sl(
    direction: str, entry: float, df: pd.DataFrame, flip_index: int, atr_last: float
) -> tuple[float, float] | None:
    window_low = float(df["low"].iloc[flip_index:].min())
    window_high = float(df["high"].iloc[flip_index:].max())
    floor_dist = atr_last * SL_FLOOR_ATR_MULT

    if direction == "LONG":
        tp = entry * (1 + TP_PRICE_PCT)
        structural_sl = window_low - atr_last * SL_ATR_BUFFER_MULTIPLIER
        # Floor: never let the stop sit closer than SL_FLOOR_ATR_MULT x ATR
        # from entry, even if the swing-since-flip window is tiny -- a
        # tighter stop gets clipped by normal candle noise regardless of
        # whether the directional call is right.
        structural_sl = min(structural_sl, entry - floor_dist)
        if structural_sl >= entry:
            return None
        if (entry - structural_sl) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl
    else:
        tp = entry * (1 - TP_PRICE_PCT)
        structural_sl = window_high + atr_last * SL_ATR_BUFFER_MULTIPLIER
        structural_sl = max(structural_sl, entry + floor_dist)
        if structural_sl <= entry:
            return None
        if (structural_sl - entry) / entry > MAX_SL_PRICE_PCT:
            return None
        return tp, structural_sl


def _score_candidate(direction: str, details: dict, rr: float) -> float:
    atr = max(details["atr"], 1e-9)

    separation = abs(details["ma5_last"] - details["baseline_last"])
    alignment_quality = min(1.0, separation / (atr * 2.0))
    score = 40.0 * alignment_quality

    freshness = 1.0 - min(1.0, details["bars_since_flip"] / max(RIBBON_LOOKBACK_BARS, 1))
    score += 20.0 * freshness

    if direction == "LONG":
        clearance = (details["low_last"] - details["pac_hi_last"]) / atr
    else:
        clearance = (details["pac_lo_last"] - details["high_last"]) / atr
    trend_bar_quality = min(1.0, max(0.0, clearance / 2.0))
    score += 20.0 * trend_bar_quality

    rr_quality = min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0 else (1.0 if rr >= MIN_RR else 0.0)
    score += 20.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def evaluate_symbol(
    symbol: str,
    btc_context=None,
    reject_sink: dict | None = None,
) -> Signal | None:
    try:
        raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if raw is None or raw.empty:
            logger.debug("[REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed = raw.iloc[:-1].copy()

        if len(closed) < RIBBON_BASELINE_LEN + RIBBON_LOOKBACK_BARS + 10:
            logger.debug("[REJECT] %s insufficient candle history", symbol)
            _bump(reject_sink, "insufficient_history")
            return None

        candle_close_time = closed.index[-1].to_pydatetime() + timedelta(minutes=CANDLE_MINUTES)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            logger.debug(
                "[REJECT] %s last closed candle only %.0fs old (need %ds) -- MEXC data may still settle",
                symbol, candle_age, MIN_CANDLE_SETTLE_SECONDS,
            )
            _bump(reject_sink, "candle_not_settled")
            return None

        lengths = (RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN)
        direction, flip_index = _detect_ribbon_flip(closed, lengths, RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS)
        if direction is None:
            logger.debug("[REJECT] %s no ribbon flip", symbol)
            _bump(reject_sink, "no_ribbon_flip")
            return None

        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            logger.debug("[REJECT] %s LONG signals disabled", symbol)
            _bump(reject_sink, "long_disabled")
            return None

        trend_bar = calculate_trend_bar(closed, TREND_BAR_PAC_LENGTH)
        current_color = trend_bar.iloc[-1]
        expected_color = "green" if direction == "LONG" else "red"
        if current_color != expected_color:
            logger.debug("[REJECT] %s no trend bar confirmation", symbol)
            _bump(reject_sink, "no_trend_bar_confirmation")
            return None

        atr_last = float(calculate_atr(closed, ATR_PERIOD).iloc[-1])
        entry = float(closed["close"].iloc[-1])

        tp_sl = _calculate_tp_sl(direction, entry, closed, flip_index, atr_last)
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

        ribbon = calculate_ema_ribbon(closed, lengths, RIBBON_BASELINE_LEN)
        pac_hi = calculate_ema(closed["high"], TREND_BAR_PAC_LENGTH)
        pac_lo = calculate_ema(closed["low"], TREND_BAR_PAC_LENGTH)
        score_details = {
            "ma5_last": float(ribbon["ma5"].iloc[-1]),
            "baseline_last": float(ribbon["baseline"].iloc[-1]),
            "atr": atr_last,
            "bars_since_flip": (len(closed) - 1) - flip_index,
            "low_last": float(closed["low"].iloc[-1]),
            "high_last": float(closed["high"].iloc[-1]),
            "pac_hi_last": float(pac_hi.iloc[-1]),
            "pac_lo_last": float(pac_lo.iloc[-1]),
        }
        score = _score_candidate(direction, score_details, rr)

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
            timeframe_summary="EMA ribbon flip + Trend Bar confirmation",
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
