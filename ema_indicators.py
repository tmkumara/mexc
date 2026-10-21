"""Pure, deterministic indicators for EMA Trend Pullback + RSI Reset v1."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _validate_period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, (int, np.integer)) or period <= 0:
        raise ValueError("period must be a positive integer")


def calculate_ema(values: pd.Series, period: int) -> pd.Series:
    """Return an ``adjust=False`` EMA, with NaN until ``period`` values exist."""
    _validate_period(period)
    if not isinstance(values, pd.Series):
        raise TypeError("values must be a pandas Series")
    return values.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()


def calculate_rsi(values: pd.Series, period: int = 14) -> pd.Series:
    """Return Wilder RSI; the first ``period`` rows are NaN.

    The initial average gain/loss is the arithmetic mean of the first
    ``period`` price changes. Subsequent values use Wilder's recursive
    smoothing. Flat prices are neutral (50), while one-sided movement is
    100 or 0 respectively.
    """
    _validate_period(period)
    if not isinstance(values, pd.Series):
        raise TypeError("values must be a pandas Series")

    prices = values.astype(float)
    result_values = np.full(len(prices), np.nan, dtype=float)
    if len(prices) <= period:
        return pd.Series(result_values, index=prices.index, dtype=float)

    changes = prices.diff().to_numpy(dtype=float)
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)
    average_gain = gains[1 : period + 1].mean()
    average_loss = losses[1 : period + 1].mean()

    def rsi_value(gain: float, loss: float) -> float:
        if loss == 0.0:
            return 50.0 if gain == 0.0 else 100.0
        if gain == 0.0:
            return 0.0
        relative_strength = gain / loss
        return 100.0 - (100.0 / (1.0 + relative_strength))

    result_values[period] = rsi_value(average_gain, average_loss)
    for position in range(period + 1, len(prices)):
        average_gain = ((average_gain * (period - 1)) + gains[position]) / period
        average_loss = ((average_loss * (period - 1)) + losses[position]) / period
        result_values[position] = rsi_value(average_gain, average_loss)
    return pd.Series(result_values, index=prices.index, dtype=float)


def calculate_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Return Wilder ATR seeded by the mean of the first ``period`` TRs."""
    _validate_period(period)
    required = {"high", "low", "close"}
    if not required.issubset(candles.columns):
        raise ValueError("candles must contain high, low, close columns")

    high = candles["high"].astype(float).to_numpy()
    low = candles["low"].astype(float).to_numpy()
    close = candles["close"].astype(float).to_numpy()
    result_values = np.full(len(candles), np.nan, dtype=float)
    if len(candles) < period:
        return pd.Series(result_values, index=candles.index, dtype=float)

    true_range = np.empty(len(candles), dtype=float)
    true_range[0] = high[0] - low[0]
    true_range[1:] = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])]
    )

    atr = true_range[:period].mean()
    result_values[period - 1] = atr
    for position in range(period, len(candles)):
        atr = ((atr * (period - 1)) + true_range[position]) / period
        result_values[position] = atr
    return pd.Series(result_values, index=candles.index, dtype=float)


def find_confirmed_pivots(
    candles: pd.DataFrame,
    left: int = 2,
    right: int = 2,
    cutoff: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Return confirmed unique pivots as timestamp/type/value records.

    A pivot at row ``i`` is confirmed only when rows ``i + 1`` through
    ``i + right`` exist and the last confirmation timestamp is not after
    ``cutoff``. Equal extrema in the full left/pivot/right window are rejected.
    """
    if isinstance(left, bool) or not isinstance(left, (int, np.integer)) or left < 1:
        raise ValueError("left must be a positive integer")
    if isinstance(right, bool) or not isinstance(right, (int, np.integer)) or right < 1:
        raise ValueError("right must be a positive integer")
    required = {"high", "low"}
    if not required.issubset(candles.columns):
        raise ValueError("candles must contain high and low columns")

    cutoff_timestamp = pd.Timestamp(cutoff) if cutoff is not None else None
    rows: list[dict[str, object]] = []
    for position in range(left, len(candles) - right):
        window = candles.iloc[position - left : position + right + 1]
        high = float(candles.iloc[position]["high"])
        low = float(candles.iloc[position]["low"])
        confirmation_timestamp = pd.Timestamp(candles.index[position + right])
        if cutoff_timestamp is not None and confirmation_timestamp > cutoff_timestamp:
            continue

        if (window["high"] == high).sum() == 1 and high == float(window["high"].max()):
            rows.append(
                {
                    "timestamp": pd.Timestamp(candles.index[position]),
                    "confirmation_timestamp": confirmation_timestamp,
                    "type": "high",
                    "value": high,
                }
            )
        if (window["low"] == low).sum() == 1 and low == float(window["low"].min()):
            rows.append(
                {
                    "timestamp": pd.Timestamp(candles.index[position]),
                    "confirmation_timestamp": confirmation_timestamp,
                    "type": "low",
                    "value": low,
                }
            )

    return pd.DataFrame(
        rows,
        columns=["timestamp", "confirmation_timestamp", "type", "value"],
    )


def is_completed_candle(
    candle_open: pd.Timestamp,
    timeframe: str | pd.Timedelta,
    evaluation_timestamp: pd.Timestamp,
    settlement_seconds: int = 90,
) -> bool:
    """Return whether a candle is complete at evaluation time.

    Completion is inclusive at ``candle_open + timeframe + settlement``.
    """
    if settlement_seconds < 0:
        raise ValueError("settlement_seconds must be non-negative")
    if isinstance(timeframe, pd.Timedelta):
        duration = timeframe
    elif isinstance(timeframe, str):
        try:
            duration = pd.Timedelta(timeframe)
        except ValueError as exc:
            raise ValueError("timeframe must be a valid duration") from exc
    else:
        raise ValueError("timeframe must be a valid duration")
    if duration <= pd.Timedelta(0):
        raise ValueError("timeframe must be positive")

    opened = pd.Timestamp(candle_open)
    evaluated_at = pd.Timestamp(evaluation_timestamp)
    completed_at = opened + duration + pd.Timedelta(seconds=settlement_seconds)
    return evaluated_at >= completed_at
