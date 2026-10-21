import numpy as np
import pandas as pd
import pytest

from ema_indicators import (
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    find_confirmed_pivots,
    is_completed_candle,
)


def test_ema_matches_pandas_adjust_false_and_warms_up():
    values = pd.Series([10.0, 12.0, 14.0, 13.0, 15.0], index=pd.RangeIndex(5))

    actual = calculate_ema(values, 3)
    expected = values.ewm(span=3, adjust=False, min_periods=3).mean()

    pd.testing.assert_series_equal(actual, expected)
    assert actual.iloc[:2].isna().all()


def test_ema_rejects_invalid_period_and_preserves_datetime_index():
    index = pd.date_range("2026-01-01", periods=3, freq="h")
    values = pd.Series([1.0, 2.0, 3.0], index=index)

    with pytest.raises(ValueError, match="period"):
        calculate_ema(values, 0)

    assert calculate_ema(values, 2).index.equals(index)


def test_rsi_rising_prices_uses_wilder_reference_values():
    prices = pd.Series([100, 101, 102, 103, 104, 105, 106], dtype=float)

    actual = calculate_rsi(prices, 3)
    assert actual.iloc[:3].isna().all()
    assert actual.iloc[3:].eq(100.0).all()


def test_rsi_falling_prices_returns_zero_after_warmup():
    prices = pd.Series([106, 105, 104, 103, 102, 101, 100], dtype=float)

    actual = calculate_rsi(prices, 3)
    assert actual.iloc[:3].isna().all()
    assert actual.iloc[3:].eq(0.0).all()


def test_rsi_flat_prices_returns_neutral_50_after_warmup():
    prices = pd.Series([100, 100, 100, 100, 100, 100], dtype=float)

    actual = calculate_rsi(prices, 3)
    assert actual.iloc[:3].isna().all()
    assert actual.iloc[3:].eq(50.0).all()


def test_atr_uses_true_range_and_wilder_seed_without_lookahead():
    candles = pd.DataFrame(
        {
            "high": [10.0, 13.0, 14.0, 15.0, 16.0],
            "low": [8.0, 9.0, 11.0, 12.0, 13.0],
            "close": [9.0, 12.0, 12.5, 14.0, 15.0],
        }
    )
    # TR = [2, 4, 3, 3, 3]; seed(3)=3, then Wilder recurrence gives 3.
    actual = calculate_atr(candles, 3)

    expected = pd.Series([np.nan, np.nan, 3.0, 3.0, 3.0])
    pd.testing.assert_series_equal(actual, expected)


def test_atr_requires_ohlc_and_preserves_index():
    index = pd.date_range("2026-01-01", periods=3, freq="15min")
    candles = pd.DataFrame(
        {"high": [3.0, 4.0, 5.0], "low": [1.0, 2.0, 3.0], "close": [2.0, 3.0, 4.0]},
        index=index,
    )

    assert calculate_atr(candles, 2).index.equals(index)
    with pytest.raises(ValueError, match="high.*low.*close"):
        calculate_atr(candles.drop(columns="close"), 2)


def test_confirmed_pivots_return_unique_highs_and_lows_with_confirmation_time():
    index = pd.date_range("2026-01-01", periods=7, freq="15min")
    candles = pd.DataFrame(
        {
            "high": [10, 12, 15, 13, 11, 12, 10],
            "low": [8, 7, 9, 6, 8, 7, 8],
        },
        index=index,
    )

    actual = find_confirmed_pivots(candles, left=2, right=2)

    assert list(actual["type"]) == ["high", "low"]
    assert list(actual["timestamp"]) == [index[2], index[3]]
    assert list(actual["confirmation_timestamp"]) == [index[4], index[5]]
    assert list(actual["value"]) == [15, 6]


def test_pivot_is_not_returned_before_right_confirmation_cutoff():
    index = pd.date_range("2026-01-01", periods=7, freq="15min")
    candles = pd.DataFrame(
        {"high": [10, 12, 15, 13, 11, 12, 10], "low": [8, 7, 9, 6, 8, 7, 8]},
        index=index,
    )

    actual = find_confirmed_pivots(candles, cutoff=index[3])

    assert actual.empty


def test_equal_high_and_equal_low_are_rejected_as_pivots():
    index = pd.date_range("2026-01-01", periods=7, freq="15min")
    candles = pd.DataFrame(
        {
            "high": [10, 12, 15, 15, 11, 12, 10],
            "low": [8, 7, 9, 6, 6, 7, 8],
        },
        index=index,
    )

    actual = find_confirmed_pivots(candles)

    assert actual.empty


def test_completed_candle_requires_timeframe_plus_settlement():
    opened = pd.Timestamp("2026-01-01 00:00:00")

    assert not is_completed_candle(opened, "15m", opened + pd.Timedelta(minutes=15, seconds=89))
    assert is_completed_candle(opened, "15m", opened + pd.Timedelta(minutes=15, seconds=90))
    assert is_completed_candle(opened, "15m", opened + pd.Timedelta(minutes=16, seconds=30))


def test_completed_candle_accepts_timedelta_and_rejects_invalid_timeframe():
    opened = pd.Timestamp("2026-01-01 00:00:00")

    assert is_completed_candle(
        opened,
        pd.Timedelta(hours=1),
        opened + pd.Timedelta(hours=1, seconds=90),
    )
    with pytest.raises(ValueError, match="timeframe"):
        is_completed_candle(opened, "bad", opened)


def test_insufficient_history_is_nan_or_empty_not_partial_indicator_values():
    prices = pd.Series([100.0, 101.0])
    candles = pd.DataFrame({"high": [2.0, 3.0], "low": [0.0, 1.0], "close": [1.0, 2.0]})

    assert calculate_ema(prices, 3).isna().all()
    assert calculate_rsi(prices, 3).isna().all()
    assert calculate_atr(candles, 3).isna().all()
    assert find_confirmed_pivots(candles).empty
