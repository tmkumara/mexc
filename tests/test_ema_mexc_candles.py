from copy import deepcopy

import pandas as pd
import pytest

from ema_candles import validate_candles
from ema_mexc_candles import parse_mexc_futures_candles


def _payload(*, times=None):
    times = times or [1767355200, 1767356100]
    values = list(range(len(times)))
    data = {
        "time": times,
        "open": [100.0 + index * 0.5 if index == 0 else str(100.0 + index * 0.5) for index in values],
        "high": [101.0 + index * 0.5 for index in values],
        "low": [99.0 + index * 0.5 for index in values],
        "close": [100.5 + index * 0.5 if index == 0 else str(100.5 + index * 0.5) for index in values],
        "vol": [10.0 + index if index == 0 else str(10.0 + index) for index in values],
    }
    return {"success": True, "code": 0, "data": data}


def test_valid_15m_response_is_converted_to_contract():
    result = parse_mexc_futures_candles(_payload(), "15m")

    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert result.index.tolist() == list(pd.to_datetime([1767355200, 1767356100], unit="s", utc=True))
    assert result["volume"].tolist() == [10.0, 11.0]


def test_valid_1h_response_and_numeric_strings_are_supported():
    payload = _payload(times=[1767355200, 1767358800])

    result = parse_mexc_futures_candles(payload, "1h")

    assert result["open"].tolist() == [100.0, 100.5]
    assert str(result.index.tz) == "UTC"


def test_observed_live_field_family_uses_documented_values_and_ignores_extras():
    payload = _payload(times=[1780185600, 1780186500])
    payload["data"].update(
        {
            "amount": [999999, 999998],
            "realOpen": [1.0, 1.0],
            "realClose": [1.0, 1.0],
            "realHigh": [1.0, 1.0],
            "realLow": [1.0, 1.0],
        }
    )
    original = deepcopy(payload)

    result = parse_mexc_futures_candles(payload, "15m")

    assert result["open"].tolist() == [100.0, 100.5]
    assert result["high"].tolist() == [101.0, 101.5]
    assert result["low"].tolist() == [99.0, 99.5]
    assert result["close"].tolist() == [100.5, 101.0]
    assert result["volume"].tolist() == [10.0, 11.0]
    assert result.index.tolist() == list(pd.to_datetime([1780185600, 1780186500], unit="s", utc=True))
    assert payload == original


def test_observed_live_payload_code_zero_is_accepted():
    payload = _payload()
    payload["data"].update(
        {
            "amount": [1000, 1001],
            "realOpen": [90, 90],
            "realClose": [91, 91],
            "realHigh": [92, 92],
            "realLow": [89, 89],
        }
    )

    result = parse_mexc_futures_candles(payload, "15m")

    assert len(result) == 2


def test_explicit_api_failure_is_distinguished():
    payload = {"success": False, "code": 1001, "message": "request rejected"}

    with pytest.raises(ValueError, match="API reported failure"):
        parse_mexc_futures_candles(payload, "15m")


def test_missing_data_is_distinguished():
    with pytest.raises(ValueError, match="missing response data"):
        parse_mexc_futures_candles({"success": True}, "15m")


@pytest.mark.parametrize("field", ["time", "open", "high", "low", "close", "vol"])
def test_missing_required_array_is_rejected(field):
    payload = _payload()
    del payload["data"][field]

    with pytest.raises(ValueError, match="required candle array"):
        parse_mexc_futures_candles(payload, "15m")


def test_mismatched_array_lengths_are_rejected():
    payload = _payload()
    payload["data"]["close"] = [100.5]

    with pytest.raises(ValueError, match="same length"):
        parse_mexc_futures_candles(payload, "15m")


def test_duplicate_timestamps_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        parse_mexc_futures_candles(_payload(times=[1767355200, 1767355200]), "15m")


def test_invalid_prices_and_negative_volume_are_rejected():
    payload = _payload()
    payload["data"]["high"][0] = "98"
    with pytest.raises(ValueError, match="OHLC"):
        parse_mexc_futures_candles(payload, "15m")

    payload = _payload()
    payload["data"]["vol"][0] = "-1"
    with pytest.raises(ValueError, match="volume"):
        parse_mexc_futures_candles(payload, "15m")


def test_unsupported_timeframe_is_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        parse_mexc_futures_candles(_payload(), "5m")


def test_reverse_chronological_api_order_is_normalized():
    payload = _payload(times=[1767356100, 1767355200])

    result = parse_mexc_futures_candles(payload, "15m")

    assert result.index.is_monotonic_increasing
    assert result["open"].tolist() == [100.5, 100.0]


def test_arbitrary_timestamp_order_is_rejected_instead_of_sorted():
    with pytest.raises(ValueError, match="chronological"):
        parse_mexc_futures_candles(_payload(times=[1767356100, 1767355200, 1767357000]), "15m")


def test_input_payload_is_not_mutated():
    payload = _payload()
    original = deepcopy(payload)

    parse_mexc_futures_candles(payload, "15m")

    assert payload == original


def test_output_is_accepted_by_candle_validator():
    result = parse_mexc_futures_candles(_payload(), "15m")

    pd.testing.assert_frame_equal(result, validate_candles(result, "15m"))


def test_empty_successful_response_returns_empty_contract_frame():
    payload = {
        "success": True,
        "data": {
            "time": [],
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "vol": [],
        },
    }

    result = parse_mexc_futures_candles(payload, "1h")

    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(result.index, pd.DatetimeIndex)
    assert len(result) == 0
