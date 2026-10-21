from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from ema_candles import find_missing_candles, validate_candles
from ema_outcome import evaluate_outcome
from ema_signal import EmaSignal


UTC = timezone.utc


def _frame(times, *, volume=None):
    count = len(times)
    return pd.DataFrame(
        {
            "open": [100.0 + index for index in range(count)],
            "high": [101.0 + index for index in range(count)],
            "low": [99.0 + index for index in range(count)],
            "close": [100.5 + index for index in range(count)],
            "volume": volume if volume is not None else [10.0] * count,
        },
        index=pd.DatetimeIndex(times),
    )


def test_valid_15m_and_1h_candles_return_canonical_columns():
    fifteen = validate_candles(_frame(["2026-01-02 12:00", "2026-01-02 12:15"]), "15m")
    hourly = validate_candles(_frame(["2026-01-02 12:00", "2026-01-02 13:00"]), "1h")

    assert list(fifteen.columns) == ["open", "high", "low", "close", "volume"]
    assert list(hourly.columns) == ["open", "high", "low", "close", "volume"]
    assert str(fifteen.index.tz) == "UTC"


def test_timestamps_are_normalized_to_utc():
    candles = _frame(["2026-01-02 07:00-05:00"])

    result = validate_candles(candles, "1h")

    assert result.index[0] == pd.Timestamp("2026-01-02 12:00:00Z")


@pytest.mark.parametrize("bad_columns", [["open", "high", "low", "close"]])
def test_missing_or_noncanonical_columns_rejected(bad_columns):
    frame = _frame(["2026-01-02 12:00"])[bad_columns]

    with pytest.raises(ValueError):
        validate_candles(frame, "15m")


def test_duplicate_timestamps_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        validate_candles(_frame(["2026-01-02 12:00", "2026-01-02 12:00"]), "15m")


def test_unsorted_timestamps_rejected():
    with pytest.raises(ValueError, match="ascending"):
        validate_candles(_frame(["2026-01-02 12:15", "2026-01-02 12:00"]), "15m")


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_nan_and_infinite_values_rejected(value):
    frame = _frame(["2026-01-02 12:00"])
    frame.loc[:, "close"] = value

    with pytest.raises(ValueError, match="finite"):
        validate_candles(frame, "15m")


@pytest.mark.parametrize("column, value", [("high", 98.0), ("low", 102.0)])
def test_invalid_ohlc_relationships_rejected(column, value):
    frame = _frame(["2026-01-02 12:00"])
    frame.loc[:, column] = value

    with pytest.raises(ValueError, match="OHLC"):
        validate_candles(frame, "15m")


def test_negative_volume_rejected():
    with pytest.raises(ValueError, match="volume"):
        validate_candles(_frame(["2026-01-02 12:00"], volume=[-1.0]), "15m")


def test_non_positive_prices_rejected():
    frame = _frame(["2026-01-02 12:00"])
    frame.loc[:, "open"] = 0.0

    with pytest.raises(ValueError, match="positive"):
        validate_candles(frame, "15m")


@pytest.mark.parametrize("timeframe, timestamp", [
    ("15m", "2026-01-02 12:01"),
    ("1h", "2026-01-02 12:15"),
])
def test_misaligned_timestamps_rejected(timeframe, timestamp):
    with pytest.raises(ValueError, match="aligned"):
        validate_candles(_frame([timestamp]), timeframe)


def test_unsupported_timeframe_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        validate_candles(_frame(["2026-01-02 12:00"]), "5m")


def test_missing_candles_are_returned_in_ascending_inclusive_range():
    candles = _frame(["2026-01-02 12:00", "2026-01-02 12:30"])

    missing = find_missing_candles(
        candles,
        "15m",
        datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 12, 45, tzinfo=UTC),
    )

    assert missing == [
        datetime(2026, 1, 2, 12, 15, tzinfo=UTC),
        datetime(2026, 1, 2, 12, 45, tzinfo=UTC),
    ]


def test_complete_candle_range_has_no_missing_timestamps():
    candles = _frame([
        "2026-01-02 12:00",
        "2026-01-02 12:15",
        "2026-01-02 12:30",
    ])

    assert find_missing_candles(
        candles,
        "15m",
        datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 12, 30, tzinfo=UTC),
    ) == []


def test_empty_candle_range_has_no_missing_timestamps():
    candles = _frame(["2026-01-02 12:00"])

    assert find_missing_candles(
        candles,
        "1h",
        datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
    ) == []


def test_reversed_candle_boundaries_are_rejected():
    candles = _frame(["2026-01-02 12:00"])

    with pytest.raises(ValueError, match="precede"):
        find_missing_candles(
            candles,
            "1h",
            datetime(2026, 1, 2, 13, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        )


def test_input_dataframe_is_not_mutated():
    candles = _frame(["2026-01-02 12:00"])
    original = candles.copy(deep=True)

    validate_candles(candles, "15m")

    pd.testing.assert_frame_equal(candles, original)


def test_valid_output_is_accepted_by_existing_outcome_evaluator():
    candles = validate_candles(_frame(["2026-01-02 12:15"]), "15m")
    signal = EmaSignal(
        symbol="TEST_USDT",
        direction="LONG",
        entry_price=100.0,
        tp_price=101.5,
        sl_price=99.0,
        leverage=20,
        tp_roi_pct=30.0,
        sl_roi_pct=20.0,
        rr=1.5,
        strategy_id="ema_trend_pullback",
        strategy_version="v1",
        setup_key="candle-contract",
        confirmation_candle_open=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        confirmation_close_at=datetime(2026, 1, 2, 12, 15, tzinfo=UTC),
        pivot_timestamp=datetime(2026, 1, 2, 11, 30, tzinfo=UTC),
        rsi_reset_timestamp=datetime(2026, 1, 2, 11, 45, tzinfo=UTC),
        generated_at=datetime(2026, 1, 2, 12, 16, tzinfo=UTC),
    )

    result = evaluate_outcome(
        signal,
        candles,
        datetime(2026, 1, 2, 12, 31, tzinfo=UTC),
        datetime(2026, 1, 2, 12, 30, tzinfo=UTC),
    )

    assert result.status == "PENDING"
