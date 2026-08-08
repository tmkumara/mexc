"""
Binocular Pending-Breakout v1.

Trigger: a Chandelier Exit ATR-direction flip + PVT-vs-signal-line
momentum + dual-RSI(fast/slow) regime, transition-detected fresh every
scan (no persisted arm state). SIGNAL_MODE=confirmed (default) also
requires the 6-EMA ribbon (30/35/40/45/50 vs 60) and EMA200 to agree;
SIGNAL_MODE=strict additionally requires daily-VWAP side and multi-
timeframe confirmation. See
docs/superpowers/specs/2026-07-30-binocular-pending-breakout-design.md.

A fresh transition creates a PENDING setup (persisted via
database.armed_setups): a precomputed entry level (buffer beyond the
signal candle's high/low), a structural SL (swing extreme of the signal
candle + prior candle, capped at MAX_SL_PRICE_PCT), and 3 stacked targets
(diff = (high - prev_low) x 2 style). The setup expires after
PENDING_SIGNAL_EXPIRY_CANDLES candles if price never breaks the entry
level, or is invalidated if an opposite transition appears first or SL
is hit before entry. Confirmed setups are tracked through a 3-target
partial-exit ladder (see outcome_check.check_target_ladder) -- 50%/30%/
20% closed at T1/T2/T3, SL moved to breakeven after T1.

LONG signals can be disabled via ENABLE_LONG_SIGNALS (true by default).
The last closed candle must be at least MIN_CANDLE_SETTLE_SECONDS old
before it's used -- MEXC's kline REST data for a just-closed candle can
still get revised shortly after close. Only completed candles are ever
used anywhere in this pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# TODO(Task 6): temporary import block so Task 4/5's new functions can
# resolve their config names ahead of the real bottom-of-file import
# block rewrite. Removed once Task 6 rewrites that block wholesale.
from config import (
    RSI_LONG_RESET_MIN, RSI_LONG_RESET_MAX, RSI_SHORT_RESET_MIN, RSI_SHORT_RESET_MAX,
    VOLUME_CONFIRM_MULT, MAX_CANDLE_BODY_PCT, ATR_MIN_PCT, ATR_MAX_PCT, EMA_TREND_LEN,
    NO_CHASE_MAX_DISTANCE_PCT, PULLBACK_PREFERRED_DISTANCE_PCT,
)


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


def confirmed_mode_ok(direction: str, df: pd.DataFrame) -> bool:
    lengths = (RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN)
    ribbon = calculate_ema_ribbon(df, lengths, RIBBON_BASELINE_LEN)
    ema200 = calculate_ema200(df, BINOCULAR_EMA200_LEN)

    ma1 = float(ribbon["ma1"].iloc[-1]); ma2 = float(ribbon["ma2"].iloc[-1])
    ma3 = float(ribbon["ma3"].iloc[-1]); ma4 = float(ribbon["ma4"].iloc[-1])
    ma5 = float(ribbon["ma5"].iloc[-1]); baseline = float(ribbon["baseline"].iloc[-1])
    close = float(df["close"].iloc[-1])
    ema200_last = float(ema200.iloc[-1])

    if direction == "LONG":
        return (
            ma1 > baseline and ma2 > baseline and ma3 > baseline
            and ma4 > baseline and ma5 > baseline and close > ema200_last
        )
    return (
        ma1 < baseline and ma2 < baseline and ma3 < baseline
        and ma4 < baseline and ma5 < baseline and close < ema200_last
    )


def mtf_signal(df_tf: pd.DataFrame) -> tuple[bool, bool]:
    direction, _, _ = calculate_chandelier_direction(df_tf, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df_tf)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    buy = bool(direction.iloc[-1] == 1 and pvt.iloc[-1] > pvt_signal.iloc[-1])
    sell = bool(direction.iloc[-1] == -1 and pvt.iloc[-1] < pvt_signal.iloc[-1])
    return buy, sell


def strict_mode_ok(direction: str, df: pd.DataFrame, symbol: str) -> tuple[bool, int]:
    vwap = calculate_daily_vwap(df)
    close = float(df["close"].iloc[-1])
    vwap_last = float(vwap.iloc[-1])
    vwap_ok = close > vwap_last if direction == "LONG" else close < vwap_last
    if not vwap_ok:
        return False, 0

    confirmations = 0
    timeframes = [t.strip() for t in CONFIRMATION_TIMEFRAMES.split(",") if t.strip()]
    for tf in timeframes:
        tf_df = get_market_klines(symbol, tf, count=ENTRY_KLINE_COUNT)
        if tf_df is None or tf_df.empty:
            continue
        tf_closed = tf_df.iloc[:-1]
        if len(tf_closed) < 60:
            continue
        buy, sell = mtf_signal(tf_closed)
        if direction == "LONG" and buy:
            confirmations += 1
        elif direction == "SHORT" and sell:
            confirmations += 1

    return confirmations >= MTF_MIN_CONFIRMATIONS, confirmations


def position_size(direction: str, entry: float, sl: float) -> float:
    risk_per_unit = (entry - sl) if direction == "LONG" else (sl - entry)
    if risk_per_unit <= 0:
        return 0.0
    risk_amount = ACCOUNT_BALANCE * RISK_PERCENT_PER_TRADE / 100.0
    return round(risk_amount / risk_per_unit, 6)


def _build_pending_setup(
    symbol: str, direction: str, df: pd.DataFrame, reject_sink: dict | None = None
) -> dict | None:
    high = float(df["high"].iloc[-1])
    low = float(df["low"].iloc[-1])
    prev_high = float(df["high"].iloc[-2])
    prev_low = float(df["low"].iloc[-2])

    if direction == "LONG":
        entry = high * (1 + ENTRY_BUFFER_PCT)
        sl = min(prev_low, low)
        diff = (high - prev_low) * 2
        t1, t2, t3 = high + diff, high + 2 * diff, high + 3 * diff
    else:
        entry = low * (1 - ENTRY_BUFFER_PCT)
        sl = max(prev_high, high)
        diff = (prev_high - low) * 2
        t1, t2, t3 = low - diff, low - 2 * diff, low - 3 * diff

    if not valid_trade_geometry(direction, entry, t1, sl):
        _bump(reject_sink, "invalid_geometry")
        return None

    if abs(entry - sl) / entry > MAX_SL_PRICE_PCT:
        _bump(reject_sink, "stop_too_wide")
        return None

    rr = abs(t1 - entry) / abs(entry - sl)
    if rr < MIN_RR:
        _bump(reject_sink, "rr_below_min")
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "trigger_price": entry,
        "entry_low": entry,
        "entry_high": entry,
        "sl_price": sl,
        "tp_price": round(t1, 8),
        "tp2_price": round(t2, 8),
        "tp3_price": round(t3, 8),
        "rr": round(rr, 2),
        "position_size": position_size(direction, entry, sl),
    }


def _score_pending_setup_binocular_legacy(
    direction: str, df: pd.DataFrame, rr: float, mtf_confirmations: int | None
) -> float:
    # NOTE (interim state, Task 6 deletes this whole old-Binocular function
    # along with the rest of the pipeline below): renamed from
    # _score_pending_setup, which now collides with Task 5's new scoring
    # function of the same name defined earlier in this file -- Python
    # binds the LAST top-level def in the module, so the old one was
    # silently shadowing the new one for every caller, not just this
    # function's own call site below. Renaming here (not the new function)
    # keeps Task 5's function at its brief-specified name.
    pvt = calculate_pvt(df)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(df["close"], RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(df["close"], RSI_SLOW_PERIOD)

    pvt_range = max(abs(pvt.iloc[-20:]).max(), 1e-9)
    pvt_strength = min(1.0, abs(pvt.iloc[-1] - pvt_signal.iloc[-1]) / pvt_range)
    rsi_strength = min(1.0, abs(rsi_fast.iloc[-1] - rsi_slow.iloc[-1]) / 30.0)
    score = 40.0 * ((pvt_strength + rsi_strength) / 2.0)

    if SIGNAL_MODE == "original":
        score += 20.0
    else:
        lengths = (RIBBON_MA1_LEN, RIBBON_MA2_LEN, RIBBON_MA3_LEN, RIBBON_MA4_LEN, RIBBON_MA5_LEN)
        ribbon = calculate_ema_ribbon(df, lengths, RIBBON_BASELINE_LEN)
        atr_last = max(float(calculate_atr(df, ATR_PERIOD).iloc[-1]), 1e-9)
        separation = abs(float(ribbon["ma5"].iloc[-1]) - float(ribbon["baseline"].iloc[-1]))
        score += 20.0 * min(1.0, separation / (atr_last * 2.0))

    rr_quality = (
        min(1.0, max(0.0, (rr - MIN_RR) / (2.0 - MIN_RR))) if MIN_RR < 2.0
        else (1.0 if rr >= MIN_RR else 0.0)
    )
    score += 20.0 * rr_quality

    close = float(df["close"].iloc[-1])
    entry_target = close * (1 + ENTRY_BUFFER_PCT) if direction == "LONG" else close * (1 - ENTRY_BUFFER_PCT)
    clearance_pct = abs(entry_target - close) / close
    score += 20.0 * max(0.0, 1.0 - min(1.0, clearance_pct / 0.01))

    return round(min(100.0, max(0.0, score)), 1)


def _classify_no_trigger_reason(df: pd.DataFrame) -> str:
    direction, _, _ = calculate_chandelier_direction(df, CHANDELIER_ATR_PERIOD, CHANDELIER_MULTIPLIER)
    pvt = calculate_pvt(df)
    pvt_signal = calculate_pvt_signal(pvt, PVT_SIGNAL_LENGTH, PVT_SIGNAL_TYPE)
    rsi_fast = calculate_rsi(df["close"], RSI_FAST_PERIOD)
    rsi_slow = calculate_rsi(df["close"], RSI_SLOW_PERIOD)

    dir_last = int(direction.iloc[-1])
    if dir_last == 1:
        if not (pvt.iloc[-1] > pvt_signal.iloc[-1]):
            return "no_pvt_momentum"
        if not (rsi_fast.iloc[-1] > rsi_slow.iloc[-1]):
            return "no_rsi_regime"
    else:
        if not (pvt.iloc[-1] < pvt_signal.iloc[-1]):
            return "no_pvt_momentum"
        if not (rsi_fast.iloc[-1] < rsi_slow.iloc[-1]):
            return "no_rsi_regime"
    return "no_chandelier_direction"


def detect_pending_setup(symbol: str, reject_sink: dict | None = None) -> dict | None:
    try:
        raw = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
        if raw is None or raw.empty:
            _bump(reject_sink, "missing_data")
            return None

        closed = raw.iloc[:-1].copy()

        min_history = max(
            RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD,
            RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH,
        ) + 10
        if len(closed) < min_history:
            _bump(reject_sink, "insufficient_history")
            return None

        candle_close_time = closed.index[-1].to_pydatetime() + timedelta(minutes=CANDLE_MINUTES)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        trigger = calculate_binocular_trigger(closed)
        direction = detect_transition(trigger)
        if direction is None:
            _bump(reject_sink, _classify_no_trigger_reason(closed))
            return None

        if direction == "LONG" and not ENABLE_LONG_SIGNALS:
            _bump(reject_sink, "long_disabled")
            return None

        if SIGNAL_MODE in ("confirmed", "strict") and not confirmed_mode_ok(direction, closed):
            _bump(reject_sink, "no_ribbon_confirmation")
            return None

        mtf_confirmations = None
        if SIGNAL_MODE == "strict":
            ok, mtf_confirmations = strict_mode_ok(direction, closed, symbol)
            if not ok:
                _bump(reject_sink, "no_vwap_confirmation" if mtf_confirmations == 0 else "no_mtf_confirmation")
                return None

        setup = _build_pending_setup(symbol, direction, closed, reject_sink)
        if setup is None:
            return None

        setup["score"] = _score_pending_setup_binocular_legacy(direction, closed, setup["rr"], mtf_confirmations)
        setup["setup_reason"] = f"Binocular {SIGNAL_MODE} trigger"
        setup["trend_summary"] = "Chandelier/PVT/RSI"
        setup["created_at"] = datetime.now(timezone.utc).isoformat()
        setup["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=PENDING_SIGNAL_EXPIRY_CANDLES * CANDLE_MINUTES)
        ).isoformat()
        return setup
    except Exception as e:
        logger.error("[BINOCULAR-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
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

    if len(closed) >= 2:
        trigger = calculate_binocular_trigger(closed)
        opposite = detect_transition(trigger)
        if opposite is not None and opposite != direction:
            return "invalidated", None

    return "waiting", None


# ── evaluate_symbol pipeline ─────────────────────────────────────────

from market_data import get_market_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, CANDLE_MINUTES,
    ATR_PERIOD, MIN_CANDLE_SETTLE_SECONDS,
    LEVERAGE, MAX_SL_PRICE_PCT, ENABLE_LONG_SIGNALS,
    ENTRY_BUFFER_PCT, PENDING_SIGNAL_EXPIRY_CANDLES,
)
# NOTE (interim state, Tasks 3-6 of the Precision Pullback Scalper v1 plan):
# config.py's Binocular-era constants (RIBBON_*, MIN_RR, SIGNAL_MODE,
# CONFIRMATION_TIMEFRAMES, MTF_MIN_CONFIRMATIONS, ACCOUNT_BALANCE,
# RISK_PERCENT_PER_TRADE, PVT_*, RSI_FAST/SLOW_PERIOD, CHANDELIER_*,
# BINOCULAR_EMA200_LEN, TARGET1/2/3_CLOSE_FRACTION,
# MOVE_SL_TO_BREAKEVEN_AFTER_T1) were removed from config.py in Task 3, one
# task before the old Binocular pipeline below is itself removed in Task 6.
# This import block is trimmed to only the names that still exist so the
# module imports successfully in the interim; the old Binocular functions
# below that reference the removed names as bare globals will raise
# NameError if actually CALLED (not merely imported) until Task 6 deletes
# them -- expected and harmless, since nothing in this plan's new tests
# (Tasks 4-5) calls the old Binocular functions, and the old Binocular
# tests (tests/test_binocular_indicators.py, tests/test_strategy_binocular_pending.py)
# are already scoped for deletion in Task 13, same as the plan's own
# already-accepted pattern from Task 6 onward -- this note just documents
# it starting one task earlier than the plan text anticipated.


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
