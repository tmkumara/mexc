"""
SMC Structure + Squeeze Momentum v1.

Single-pass confluence model (no pending-setup state machine, unlike the
retired Zero-Lag MTF Pullback v1 -- see backup/zero-lag-mtf-pullback-v1).

detect_signal(symbol) evaluates ENTRY_TF (30m) market structure (fractal
swing pivots -> BOS/CHoCH break detection, modeled on the LuxAlgo "Smart
Money Concepts" indicator) together with Squeeze Momentum (LazyBear
SQZMOM_LB -- BB vs KC compression/release, rolling linreg momentum). A
signal only fires when a structure break and the squeeze-momentum
direction agree, scored 0-10 on break quality, squeeze-fire freshness,
momentum strength, TREND_TF (1h) EMA50 alignment, and volume. Stop-loss
anchors to the swing point behind the break (+ a small buffer); risk is
floored/capped (MIN_RISK_PCT..MAX_RISK_PCT) so every signal guarantees
>= MIN_WIN_ROI_PCT return on a win at LEVERAGE, and take-profit is always
exactly RISK_REWARD_RATIO x the risk distance (fixed 1:2 RR).

Only completed candles are ever used -- the last (still-forming) bar is
always dropped via iloc[:-1], and the last closed ENTRY_TF candle must be
at least MIN_CANDLE_SETTLE_SECONDS old before it's used (MEXC's kline REST
data for a just-closed candle can still get revised shortly after close).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from market_data import get_market_klines
from config import (
    ENTRY_TF, TREND_TF, ENTRY_KLINE_COUNT, TREND_KLINE_COUNT,
    _TF_MINUTES, MIN_CANDLE_SETTLE_SECONDS,
    SQZ_BB_LENGTH, SQZ_BB_MULT, SQZ_KC_LENGTH, SQZ_KC_MULT, SQZ_LOOKBACK_BARS,
    STRUCTURE_LEFT, STRUCTURE_RIGHT, STRUCTURE_LOOKBACK_BARS,
    SIGNAL_SCORE_THRESHOLD, RISK_REWARD_RATIO, MIN_RISK_PCT, MAX_RISK_PCT,
    SL_BUFFER_PCT, LEVERAGE,
)

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

def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _linreg_last(values: np.ndarray) -> float:
    """Value of the linear-regression line at the last point of the window."""
    n = len(values)
    x = np.arange(n)
    slope, intercept = np.polyfit(x, values, 1)
    return slope * (n - 1) + intercept


def squeeze_momentum(
    df: pd.DataFrame,
    bb_length: int = SQZ_BB_LENGTH,
    bb_mult: float = SQZ_BB_MULT,
    kc_length: int = SQZ_KC_LENGTH,
    kc_mult: float = SQZ_KC_MULT,
) -> pd.DataFrame:
    """LazyBear's Squeeze Momentum Indicator (SQZMOM_LB), matching the
    20/2/20/1.5 settings used on the live TradingView chart. Adds columns:
    sqz_on (BB inside KC -- compression), sqz_off (squeeze just released
    this bar), sqz_mom (rolling linreg momentum value)."""
    close, high, low = df["close"], df["high"], df["low"]

    basis = close.rolling(bb_length).mean()
    dev = bb_mult * close.rolling(bb_length).std()
    bb_upper, bb_lower = basis + dev, basis - dev

    kc_ma = close.rolling(kc_length).mean()
    rng_ma = true_range(df).rolling(kc_length).mean()
    kc_upper, kc_lower = kc_ma + rng_ma * kc_mult, kc_ma - rng_ma * kc_mult

    sqz_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    sqz_on_prev = sqz_on.shift(1, fill_value=False)
    sqz_off = sqz_on_prev & (~sqz_on)

    highest_high = high.rolling(kc_length).max()
    lowest_low = low.rolling(kc_length).min()
    donchian_mid = ((highest_high + lowest_low) / 2 + kc_ma) / 2
    delta = close - donchian_mid

    mom = delta.rolling(kc_length).apply(lambda w: _linreg_last(w.to_numpy()), raw=False)

    df = df.copy()
    df["sqz_on"], df["sqz_off"], df["sqz_mom"] = sqz_on, sqz_off, mom
    return df


def pivot_points(df: pd.DataFrame, left: int = STRUCTURE_LEFT, right: int = STRUCTURE_RIGHT):
    """Return (pivot_high_idx, pivot_low_idx) -- lists of confirmed fractal indices."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    pivot_high_idx, pivot_low_idx = [], []
    for i in range(left, n - right):
        window_h = highs[i - left: i + right + 1]
        if highs[i] == window_h.max() and np.sum(window_h == highs[i]) == 1:
            pivot_high_idx.append(i)
        window_l = lows[i - left: i + right + 1]
        if lows[i] == window_l.min() and np.sum(window_l == lows[i]) == 1:
            pivot_low_idx.append(i)
    return pivot_high_idx, pivot_low_idx


def market_structure(df: pd.DataFrame, left: int = STRUCTURE_LEFT, right: int = STRUCTURE_RIGHT):
    """Smart-Money-Concepts style market structure: Break of Structure (BOS,
    trend continuation) and Change of Character (CHoCH, trend reversal) from
    confirmed fractal swing points. Returns a list of events, each
    (bar_index, event_type, broken_level, opposite_level) where event_type
    is one of BOS_UP/CHOCH_UP/BOS_DOWN/CHOCH_DOWN, broken_level is the swing
    price that was broken, and opposite_level is the most recent swing on
    the *other* side at that moment (used for stop-loss placement)."""
    closes, highs, lows = df["close"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)

    pivot_high_idx, pivot_low_idx = pivot_points(df, left, right)

    events = []
    trend = None
    last_high = last_low = None
    ph_ptr = pl_ptr = 0

    for i in range(n):
        while ph_ptr < len(pivot_high_idx) and pivot_high_idx[ph_ptr] + right == i:
            last_high = highs[pivot_high_idx[ph_ptr]]
            ph_ptr += 1
        while pl_ptr < len(pivot_low_idx) and pivot_low_idx[pl_ptr] + right == i:
            last_low = lows[pivot_low_idx[pl_ptr]]
            pl_ptr += 1

        if last_high is not None and closes[i] > last_high:
            event_type = "CHOCH_UP" if trend in (None, "down") else "BOS_UP"
            events.append((i, event_type, last_high, last_low))
            trend = "up"
            last_high = None

        if last_low is not None and closes[i] < last_low:
            event_type = "CHOCH_DOWN" if trend in (None, "up") else "BOS_DOWN"
            events.append((i, event_type, last_low, last_high))
            trend = "down"
            last_low = None

    return events, trend, last_high, last_low


# ── evaluate_symbol pipeline ─────────────────────────────────────────

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


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def detect_signal(symbol: str, reject_sink: dict | None = None) -> Signal | None:
    try:
        raw_entry = get_market_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)
        if raw_entry is None or raw_entry.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed = raw_entry.iloc[:-1].copy()

        raw_trend = get_market_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        if raw_trend is None or raw_trend.empty:
            _bump(reject_sink, "missing_data")
            return None
        closed_trend = raw_trend.iloc[:-1].copy()

        min_entry_history = max(SQZ_BB_LENGTH, SQZ_KC_LENGTH) + STRUCTURE_LEFT + STRUCTURE_RIGHT + 10
        if len(closed) < min_entry_history or len(closed_trend) < 55:
            _bump(reject_sink, "insufficient_history")
            return None

        entry_tf_minutes = _TF_MINUTES.get(ENTRY_TF, 30)
        candle_close_time = closed.index[-1].to_pydatetime() + timedelta(minutes=entry_tf_minutes)
        candle_age = (datetime.utcnow() - candle_close_time).total_seconds()
        if candle_age < MIN_CANDLE_SETTLE_SECONDS:
            _bump(reject_sink, "candle_not_settled")
            return None

        df = squeeze_momentum(closed)
        last = df.iloc[-1]
        if pd.isna(last["sqz_mom"]):
            _bump(reject_sink, "insufficient_history")
            return None

        events, _trend, _pending_high, _pending_low = market_structure(df)
        if not events:
            _bump(reject_sink, "no_structure_break")
            return None

        last_idx = len(df) - 1
        ev_idx, ev_type, broken_level, opp_level = events[-1]
        if last_idx - ev_idx > STRUCTURE_LOOKBACK_BARS:
            _bump(reject_sink, "structure_break_stale")
            return None
        if opp_level is None:
            _bump(reject_sink, "no_stop_reference")
            return None

        price = float(last["close"])
        mom = float(last["sqz_mom"])
        prev_mom = float(df["sqz_mom"].iloc[-2])

        bullish_break = ev_type in ("BOS_UP", "CHOCH_UP")
        bearish_break = ev_type in ("BOS_DOWN", "CHOCH_DOWN")

        if bullish_break and mom > 0:
            direction = "LONG"
            sl_ref = opp_level
        elif bearish_break and mom < 0:
            direction = "SHORT"
            sl_ref = opp_level
        else:
            _bump(reject_sink, "momentum_disagrees")
            return None

        score = 0.0
        reasons: list[str] = []

        is_choch = ev_type in ("CHOCH_UP", "CHOCH_DOWN")
        if is_choch:
            score += 3.0
            reasons.append(f"CHoCH {direction.lower()}")
        else:
            score += 2.0
            reasons.append(f"BOS {direction.lower()}")

        if bool(df["sqz_off"].iloc[-1]):
            score += 3.0
            reasons.append("squeeze fired this candle")
        elif bool(df["sqz_off"].iloc[-(SQZ_LOOKBACK_BARS + 1):].any()):
            score += 1.5
            reasons.append(f"squeeze fired within {SQZ_LOOKBACK_BARS} candles")

        if direction == "LONG" and mom > prev_mom:
            score += 2.0
            reasons.append("momentum rising")
        elif direction == "SHORT" and mom < prev_mom:
            score += 2.0
            reasons.append("momentum falling")

        trend_ema50 = calculate_ema(closed_trend["close"], 50)
        trend_ema_val = float(trend_ema50.iloc[-1])
        if not pd.isna(trend_ema_val):
            trend_up = float(closed_trend["close"].iloc[-1]) > trend_ema_val
            if (direction == "LONG" and trend_up) or (direction == "SHORT" and not trend_up):
                score += 1.0
                reasons.append("1h trend aligned")

        vol_ma20 = df["volume"].rolling(20).mean()
        vol_ratio = float(df["volume"].iloc[-1] / (vol_ma20.iloc[-1] + 1e-10))
        if not pd.isna(vol_ratio) and vol_ratio >= 1.3:
            score += 1.0
            reasons.append(f"volume {vol_ratio:.1f}x avg")

        if score < SIGNAL_SCORE_THRESHOLD:
            _bump(reject_sink, "score_below_min")
            return None

        buffer = price * SL_BUFFER_PCT
        if direction == "LONG":
            structure_sl = sl_ref - buffer
            structure_risk = price - structure_sl
        else:
            structure_sl = sl_ref + buffer
            structure_risk = structure_sl - price

        if structure_risk <= 0:
            _bump(reject_sink, "invalid_structure_risk")
            return None

        structure_risk_pct = structure_risk / price
        if structure_risk_pct > MAX_RISK_PCT:
            _bump(reject_sink, "risk_too_wide")
            return None
        risk_pct = max(structure_risk_pct, MIN_RISK_PCT)
        risk = price * risk_pct
        reward = risk * RISK_REWARD_RATIO

        if direction == "LONG":
            sl = round(price - risk, 8)
            tp = round(price + reward, 8)
        else:
            sl = round(price + risk, 8)
            tp = round(price - reward, 8)

        if not valid_trade_geometry(direction, price, tp, sl):
            _bump(reject_sink, "invalid_geometry")
            return None

        tp_roi = risk_pct * RISK_REWARD_RATIO * LEVERAGE * 100.0
        sl_roi = risk_pct * LEVERAGE * 100.0

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=price,
            tp_price=tp,
            sl_price=sl,
            leverage=LEVERAGE,
            tp_roi_pct=round(tp_roi, 2),
            sl_roi_pct=round(sl_roi, 2),
            timeframe_summary=f"30m:{ev_type} 1h:Trend | {', '.join(reasons)}",
            generated_at=datetime.now(timezone.utc),
            rr=RISK_REWARD_RATIO,
            score=round(score, 1),
            entry_low=price,
            entry_high=price,
        )
    except Exception as e:
        logger.error("[SMC-SQZ-DETECT-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
