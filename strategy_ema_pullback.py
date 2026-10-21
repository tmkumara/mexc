"""Pure EMA Trend Pullback + RSI Reset v1 evaluator."""

from __future__ import annotations

import pandas as pd
from datetime import datetime, timedelta
from typing import Mapping

from ema_indicators import (
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    find_confirmed_pivots,
    is_completed_candle,
)


LEVERAGE = 20
STRATEGY_ID = "ema_trend_pullback"
STRATEGY_VERSION = "v1"
TP_MULTIPLIER = 1.015
MIN_STOP_PCT = 0.30
MAX_STOP_PCT = 1.00
MIN_RR = 1.5
SETTLEMENT_SECONDS = 90


def _utc_datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.to_pydatetime()


def _setup_key(
    symbol: str,
    direction: str,
    confirmation_close_at: datetime,
    pivot_timestamp: datetime,
    rsi_reset_timestamp: datetime,
) -> str:
    return "|".join(
        [
            STRATEGY_ID,
            STRATEGY_VERSION,
            symbol,
            direction,
            confirmation_close_at.isoformat(),
            pivot_timestamp.isoformat(),
            rsi_reset_timestamp.isoformat(),
        ]
    )


def _reject(reason: str) -> dict[str, object]:
    return {"accepted": False, "reason": reason}


def _completed(
    candles: pd.DataFrame,
    timeframe: str,
    evaluation_timestamp: pd.Timestamp,
    *,
    trusted_completed_prefix: bool = False,
) -> pd.DataFrame:
    if trusted_completed_prefix:
        return candles
    mask = [
        is_completed_candle(index, timeframe, evaluation_timestamp, SETTLEMENT_SECONDS)
        for index in candles.index
    ]
    return candles.loc[mask].copy()


def _validate_candles(candles: pd.DataFrame) -> None:
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(candles.columns):
        raise ValueError("candles must contain open, high, low, close, volume columns")
    if not isinstance(candles.index, pd.DatetimeIndex) or not candles.index.is_monotonic_increasing:
        raise ValueError("candles must use a sorted DatetimeIndex")


def _cached_series(cache: Mapping[str, object], key: str, index: pd.Index) -> pd.Series:
    values = cache.get(key)
    if not isinstance(values, pd.Series):
        raise ValueError(f"indicator cache is missing series {key!r}")
    return values.reindex(index)


def _trend_aligned(
    hourly: pd.DataFrame,
    direction: str,
    indicator_cache: Mapping[str, object] | None = None,
) -> bool:
    if indicator_cache is None:
        ema50 = calculate_ema(hourly["close"], 50)
        ema200 = calculate_ema(hourly["close"], 200)
    else:
        ema50 = _cached_series(indicator_cache, "ema50_1h", hourly.index)
        ema200 = _cached_series(indicator_cache, "ema200_1h", hourly.index)
    if ema50.iloc[-2:].isna().any() or ema200.iloc[-1:].isna().any():
        return False
    closes = hourly["close"].iloc[-2:]
    slope = ema50.iloc[-1] - ema50.iloc[-2]
    if direction == "LONG":
        return bool(
            ema50.iloc[-1] > ema200.iloc[-1]
            and closes.iloc[-1] > ema50.iloc[-1]
            and closes.iloc[-2] > ema50.iloc[-2]
            and slope >= 0
        )
    return bool(
        ema50.iloc[-1] < ema200.iloc[-1]
        and closes.iloc[-1] < ema50.iloc[-1]
        and closes.iloc[-2] < ema50.iloc[-2]
        and slope <= 0
    )


def evaluate_ema_pullback(
    symbol: str,
    candles_15m: pd.DataFrame,
    candles_1h: pd.DataFrame,
    evaluation_timestamp: pd.Timestamp,
    *,
    indicator_cache: Mapping[str, object] | None = None,
    trusted_completed_prefix: bool = False,
) -> dict[str, object]:
    """Evaluate one completed-candle EMA pullback setup without side effects."""
    _validate_candles(candles_15m)
    _validate_candles(candles_1h)
    evaluation = pd.Timestamp(evaluation_timestamp)

    if not candles_15m.index.tolist():
        return _reject("insufficient_history")
    if not trusted_completed_prefix and not is_completed_candle(
        candles_15m.index[-1], "15min", evaluation, SETTLEMENT_SECONDS
    ):
        return _reject("forming_candle_excluded")

    fifteen = _completed(
        candles_15m,
        "15min",
        evaluation,
        trusted_completed_prefix=trusted_completed_prefix,
    )
    if len(fifteen) < 50:
        return _reject("insufficient_history")

    latest_15_close = fifteen.index[-1] + pd.Timedelta(minutes=15)
    hourly = _completed(
        candles_1h,
        "1h",
        evaluation,
        trusted_completed_prefix=trusted_completed_prefix,
    )
    hourly = hourly.loc[hourly.index + pd.Timedelta(hours=1) <= latest_15_close]
    if len(hourly) < 200:
        return _reject("insufficient_history")

    if indicator_cache is None:
        ema50_15 = calculate_ema(fifteen["close"], 50)
        ema20_15 = calculate_ema(fifteen["close"], 20)
        rsi = calculate_rsi(fifteen["close"], 14)
        atr = calculate_atr(fifteen, 14)
    else:
        ema50_15 = _cached_series(indicator_cache, "ema50_15m", fifteen.index)
        ema20_15 = _cached_series(indicator_cache, "ema20_15m", fifteen.index)
        rsi = _cached_series(indicator_cache, "rsi14_15m", fifteen.index)
        atr = _cached_series(indicator_cache, "atr14_15m", fifteen.index)
    latest = fifteen.iloc[-1]
    previous = fifteen.iloc[-2]
    latest_ema20 = ema20_15.iloc[-1]
    latest_ema50 = ema50_15.iloc[-1]
    latest_rsi = rsi.iloc[-1]
    previous_rsi = rsi.iloc[-2]
    if pd.isna(latest_ema20) or pd.isna(latest_ema50) or pd.isna(latest_rsi) or pd.isna(previous_rsi) or pd.isna(atr.iloc[-1]):
        return _reject("insufficient_history")

    if latest_ema20 > latest_ema50:
        direction = "LONG"
    elif latest_ema20 < latest_ema50:
        direction = "SHORT"
    else:
        return _reject("trend_not_aligned")
    if not _trend_aligned(hourly, direction, indicator_cache):
        return _reject("trend_not_aligned")
    prior = fifteen.iloc[-9:-1]
    prior_ema20 = ema20_15.iloc[-9:-1]
    prior_ema50 = ema50_15.iloc[-9:-1]
    prior_rsi = rsi.iloc[-9:-1]

    if direction == "LONG":
        touches = (prior["low"] <= prior_ema20) | (prior["low"] <= prior_ema50)
        preserved = prior["close"] >= prior_ema50
        reset_rows = prior.index[prior_rsi <= 45]
        crossed = previous_rsi <= 50 and latest_rsi > 50
    else:
        touches = (prior["high"] >= prior_ema20) | (prior["high"] >= prior_ema50)
        preserved = prior["close"] <= prior_ema50
        reset_rows = prior.index[prior_rsi >= 55]
        crossed = previous_rsi >= 50 and latest_rsi < 50

    if not bool((touches & preserved).any()):
        return _reject("no_valid_ema_pullback")
    if len(reset_rows) == 0 or not crossed:
        return _reject("rsi_reset_missing")
    rsi_reset_timestamp = pd.Timestamp(reset_rows[0])

    candle_range = float(latest["high"] - latest["low"])
    body = abs(float(latest["close"] - latest["open"]))
    body_ratio = body / candle_range if candle_range > 0 else 0.0
    if direction == "LONG":
        valid_confirmation = (
            latest["close"] > latest["open"]
            and latest["close"] > latest_ema20
            and latest["close"] > previous["high"]
            and latest["close"] <= latest_ema20 * 1.005
        )
    else:
        valid_confirmation = (
            latest["close"] < latest["open"]
            and latest["close"] < latest_ema20
            and latest["close"] < previous["low"]
            and latest["close"] >= latest_ema20 * 0.995
        )
    if not valid_confirmation or body_ratio < 0.40:
        return _reject("confirmation_invalid")

    confirmation_timestamp = pd.Timestamp(fifteen.index[-1])
    if indicator_cache is None:
        pivots = find_confirmed_pivots(fifteen, left=2, right=2, cutoff=confirmation_timestamp)
    else:
        cached_pivots = indicator_cache.get("pivots_15m")
        if not isinstance(cached_pivots, pd.DataFrame):
            raise ValueError("indicator cache is missing confirmed pivots")
        confirmation_timestamps = indicator_cache.get("pivot_confirmation_timestamps")
        if isinstance(confirmation_timestamps, pd.DatetimeIndex):
            pivot_end = int(confirmation_timestamps.searchsorted(confirmation_timestamp, side="right"))
            pivots = cached_pivots.iloc[:pivot_end]
        else:
            pivots = cached_pivots.loc[
                cached_pivots["confirmation_timestamp"] <= confirmation_timestamp
            ]
    pivots = pivots.loc[pivots["timestamp"] < confirmation_timestamp]
    pivot_type = "low" if direction == "LONG" else "high"
    pivots = pivots.loc[pivots["type"] == pivot_type]
    if pivots.empty:
        return _reject("pivot_missing")
    pivot = pivots.iloc[-1]

    entry = float(latest["close"])
    atr_value = float(atr.iloc[-1])
    if direction == "LONG":
        sl = float(pivot["value"] - 0.5 * atr_value)
        tp = entry * TP_MULTIPLIER
    else:
        sl = float(pivot["value"] + 0.5 * atr_value)
        tp = entry * (2.0 - TP_MULTIPLIER)

    stop_distance = abs(entry - sl)
    stop_pct = stop_distance / entry * 100.0
    if stop_pct > MAX_STOP_PCT:
        return _reject("risk_too_wide")
    if stop_pct < MIN_STOP_PCT:
        return _reject("risk_too_tight")
    target_distance = abs(tp - entry)
    rr = target_distance / stop_distance
    if rr < MIN_RR:
        return _reject("rr_too_low")

    confirmation_candle_open = _utc_datetime(confirmation_timestamp)
    confirmation_close_at = confirmation_candle_open + timedelta(minutes=15)
    pivot_timestamp = _utc_datetime(pivot["timestamp"])
    rsi_reset_timestamp = _utc_datetime(rsi_reset_timestamp)
    setup_key = _setup_key(
        symbol,
        direction,
        confirmation_close_at,
        pivot_timestamp,
        rsi_reset_timestamp,
    )
    return {
        "accepted": True,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "leverage": LEVERAGE,
        "gross_tp_roi_pct": abs(tp - entry) / entry * 100.0 * LEVERAGE,
        "gross_sl_roi_pct": stop_pct * LEVERAGE,
        "rr": rr,
        "confirmation_candle_open": confirmation_candle_open,
        "confirmation_close_at": confirmation_close_at,
        "pivot_timestamp": pivot_timestamp,
        "rsi_reset_timestamp": rsi_reset_timestamp,
        "setup_key": setup_key,
    }
