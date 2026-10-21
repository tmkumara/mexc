from datetime import datetime, timedelta, timezone
import json

import pandas as pd
import pytest
import requests

from ema_mexc_client import EmaMexcClient
from ema_candles import validate_candles


UTC = timezone.utc
START = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
END_15M = datetime(2026, 1, 2, 12, 15, tzinfo=UTC)
END_1H = datetime(2026, 1, 2, 13, 0, tzinfo=UTC)
STEP_15M = timedelta(minutes=15)
STEP_1H = timedelta(hours=1)


def _payload(times=(1767355200, 1767356100)):
    count = len(times)
    return {
        "success": True,
        "data": {
            "time": list(times),
            "open": [100 + index for index in range(count)],
            "high": [101 + index for index in range(count)],
            "low": [99 + index for index in range(count)],
            "close": [100.5 + index for index in range(count)],
            "vol": [10 + index for index in range(count)],
        },
    }


def _times(start, end, step):
    return list(pd.date_range(start, end, freq=step, tz="UTC"))


def _range_payload(start, end, step, reverse=False):
    times = _times(start, end, step)
    epoch_seconds = [int(timestamp.timestamp()) for timestamp in times]
    payload = _payload(tuple(epoch_seconds))
    if reverse:
        for values in payload["data"].values():
            values.reverse()
    return payload


class _Response:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError(self._json_error)
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(session, **kwargs):
    return EmaMexcClient(session=session, retry_delay=0, **kwargs)


def test_valid_15m_fetch_uses_endpoint_and_contract():
    session = _Session([_Response(payload=_payload())])
    client = _client(session)

    result = client.fetch_candles("BTC_USDT", "15m", START, END_15M)

    assert len(result) == 2
    assert result.index.tz == UTC
    assert list(result.columns) == ["open", "high", "low", "close", "volume"]
    assert session.calls[0][0] == "https://contract.mexc.com/api/v1/contract/kline/BTC_USDT"


def test_valid_1h_fetch_maps_interval():
    session = _Session([_Response(payload=_payload((1767355200, 1767358800)))])

    result = _client(session).fetch_candles("ETH_USDT", "1h", START, END_1H)

    assert len(result) == 2
    assert session.calls[0][1]["interval"] == "Min60"


def test_request_uses_symbol_interval_and_utc_unix_seconds():
    session = _Session([_Response(payload=_payload())])

    _client(session).fetch_candles("XRP_USDT", "15m", START, END_15M)

    assert session.calls[0][1] == {
        "interval": "Min15",
        "start": int(START.timestamp()),
        "end": int(END_15M.timestamp()),
    }


@pytest.mark.parametrize("symbol", ["", "btc_usdt", "BTC/USDT", "BTC"])
def test_invalid_symbol_is_rejected_before_http(symbol):
    session = _Session([])

    with pytest.raises(ValueError, match="symbol"):
        _client(session).fetch_candles(symbol, "15m", START, END_15M)
    assert session.calls == []


def test_invalid_timeframe_is_rejected_before_http():
    session = _Session([])

    with pytest.raises(ValueError, match="unsupported"):
        _client(session).fetch_candles("BTC_USDT", "5m", START, END_15M)
    assert session.calls == []


@pytest.mark.parametrize("start, end", [
    (datetime(2026, 1, 2, 12, 1, tzinfo=UTC), END_15M),
    (START, datetime(2026, 1, 2, 12, 14, tzinfo=UTC)),
    (END_15M, START),
    (datetime(2026, 1, 2, 12, 0), END_15M),
])
def test_invalid_boundaries_are_rejected_before_http(start, end):
    session = _Session([])

    with pytest.raises(ValueError, match="boundary|aligned|UTC-aware|precede"):
        _client(session).fetch_candles("BTC_USDT", "15m", start, end)
    assert session.calls == []


def test_timeout_is_retryable_and_distinguished():
    session = _Session([requests.Timeout("read timeout"), _Response(payload=_payload())])

    result = _client(session, retries=1).fetch_candles("BTC_USDT", "15m", START, END_15M)

    assert len(result) == 2
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [429, 500, 502])
def test_rate_limit_and_server_errors_are_retried(status):
    session = _Session([_Response(status_code=status), _Response(payload=_payload())])

    result = _client(session, retries=1).fetch_candles("BTC_USDT", "15m", START, END_15M)

    assert len(result) == 2
    assert len(session.calls) == 2


def test_client_errors_are_not_retried():
    session = _Session([_Response(status_code=400)])

    with pytest.raises(ValueError, match="HTTP 400"):
        _client(session, retries=2).fetch_candles("BTC_USDT", "15m", START, END_15M)
    assert len(session.calls) == 1


def test_retry_exhaustion_is_reported():
    session = _Session([_Response(status_code=503), _Response(status_code=503)])

    with pytest.raises(RuntimeError, match="retryable HTTP failure"):
        _client(session, retries=1).fetch_candles("BTC_USDT", "15m", START, END_15M)


def test_api_level_failure_is_not_retried():
    session = _Session([_Response(payload={"success": False, "message": "bad request"})])

    with pytest.raises(ValueError, match="API reported failure"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)
    assert len(session.calls) == 1


def test_invalid_json_is_not_retried():
    session = _Session([_Response(json_error="not json")])

    with pytest.raises(ValueError, match="valid JSON"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)
    assert len(session.calls) == 1


def test_malformed_candle_response_is_not_retried():
    session = _Session([_Response(payload={"success": True, "data": {}})])

    with pytest.raises(ValueError, match="required candle array"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)
    assert len(session.calls) == 1


def test_retry_exhaustion_after_connection_failure():
    session = _Session([requests.ConnectionError("offline"), requests.ConnectionError("offline")])

    with pytest.raises(RuntimeError, match="request failed after retries"):
        _client(session, retries=1).fetch_candles("BTC_USDT", "15m", START, END_15M)


def test_no_implicit_system_clock_usage(monkeypatch):
    session = _Session([_Response(payload=_payload())])
    monkeypatch.setattr("ema_mexc_client.datetime", None)

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)

    assert len(result) == 2


def test_incomplete_response_is_rejected_instead_of_silently_truncated():
    session = _Session([_Response(payload=_payload((1767355200,)))])

    with pytest.raises(ValueError, match="does not cover requested range"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)


def test_empty_response_is_rejected_as_uncovered_range():
    session = _Session([_Response(payload={
        "success": True,
        "data": {"time": [], "open": [], "high": [], "low": [], "close": [], "vol": []},
    })])

    with pytest.raises(ValueError, match="does not cover requested range"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)


def test_output_is_accepted_by_validate_candles():
    session = _Session([_Response(payload=_payload())])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, END_15M)

    pd.testing.assert_frame_equal(result, validate_candles(result, "15m"))


def test_exactly_1900_candles_uses_one_window():
    end = START + (1900 - 1) * STEP_15M
    session = _Session([_Response(payload=_range_payload(START, end, STEP_15M))])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, end)

    assert len(result) == 1900
    assert len(session.calls) == 1
    assert session.calls[0][1]["start"] == int(START.timestamp())
    assert session.calls[0][1]["end"] == int(end.timestamp())


def test_1901_candles_use_two_windows_with_one_shared_boundary():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=_range_payload(first_end, end, STEP_15M)),
    ])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, end)

    assert len(result) == 1901
    assert result.index.is_unique
    assert len(session.calls) == 2
    assert session.calls[1][1]["start"] == int(first_end.timestamp())


def test_inclusive_window_boundaries_are_requested_and_retained():
    end = START + 2 * STEP_15M
    session = _Session([_Response(payload=_range_payload(START, end, STEP_15M))])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, end)

    assert result.index[0].to_pydatetime() == START
    assert result.index[-1].to_pydatetime() == end
    assert session.calls[0][1]["start"] == int(START.timestamp())
    assert session.calls[0][1]["end"] == int(end.timestamp())


def test_adjacent_windows_share_one_boundary_and_merge_without_gap():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=_range_payload(first_end, end, STEP_15M)),
    ])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, end)

    assert session.calls[1][1]["start"] == int(first_end.timestamp())
    assert len(result) == 1901
    assert result.index.is_unique
    assert result.index[0].to_pydatetime() == START
    assert result.index[-1].to_pydatetime() == end


def test_valid_response_above_2000_candles_is_not_rejected_by_count_alone():
    end = START + 2000 * STEP_15M
    payload = _range_payload(START, end, STEP_15M)

    # The window validator checks coverage and range, not an arbitrary row cap.
    client = _client(_Session([_Response(payload=payload)]))
    frame = client._fetch_window("BTC_USDT", "15m", pd.Timestamp(START), pd.Timestamp(end))

    assert len(frame) == 2001


def test_multi_window_1h_range_is_complete():
    end = START + 1900 * STEP_1H
    first_end = START + 1899 * STEP_1H
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_1H)),
        _Response(payload=_range_payload(first_end, end, STEP_1H)),
    ])

    result = _client(session).fetch_candles("BTC_USDT", "1h", START, end)

    assert len(result) == 1901
    assert result.index[0].to_pydatetime() == START
    assert result.index[-1].to_pydatetime() == end


def test_deterministic_window_generation_for_3801_candles():
    end = START + 3800 * STEP_15M
    first_end = START + 1899 * STEP_15M
    second_end = START + 3798 * STEP_15M
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=_range_payload(first_end, second_end, STEP_15M)),
        _Response(payload=_range_payload(second_end, end, STEP_15M)),
    ])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, end)

    assert len(result) == 3801
    assert [(call[1]["start"], call[1]["end"]) for call in session.calls] == [
        (int(START.timestamp()), int(first_end.timestamp())),
        (int(first_end.timestamp()), int(second_end.timestamp())),
        (int(second_end.timestamp()), int(end.timestamp())),
    ]


def test_missing_shared_boundary_is_incomplete_history():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    second_payload = _range_payload(first_end + STEP_15M, end, STEP_15M)
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=second_payload),
    ])

    with pytest.raises(ValueError, match="does not cover requested range"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, end)


def test_missing_first_candle_is_rejected():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    session = _Session([
        _Response(payload=_range_payload(START + STEP_15M, first_end, STEP_15M)),
        _Response(payload=_range_payload(first_end, end, STEP_15M)),
    ])

    with pytest.raises(ValueError, match="does not cover requested range"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, end)


def test_missing_last_candle_is_rejected():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=_range_payload(first_end, end - STEP_15M, STEP_15M)),
    ])

    with pytest.raises(ValueError, match="does not cover requested range"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, end)


def test_unexpected_duplicate_within_paginated_response_is_rejected():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    duplicate_payload = _range_payload(first_end, end, STEP_15M)
    duplicate_payload["data"]["time"].insert(1, duplicate_payload["data"]["time"][1])
    for field in ("open", "high", "low", "close", "vol"):
        duplicate_payload["data"][field].insert(1, duplicate_payload["data"][field][1])
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=duplicate_payload),
    ])

    with pytest.raises(ValueError, match="duplicate"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, end)


def test_out_of_range_candle_is_rejected():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    out_of_range_payload = _range_payload(first_end, end, STEP_15M)
    out_of_range_payload["data"]["time"].append(int((end + STEP_15M).timestamp()))
    for field, value in (("open", 200), ("high", 201), ("low", 199), ("close", 200.5), ("vol", 10)):
        out_of_range_payload["data"][field].append(value)
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M)),
        _Response(payload=out_of_range_payload),
    ])

    with pytest.raises(ValueError, match="extends beyond requested range|chronological"):
        _client(session).fetch_candles("BTC_USDT", "15m", START, end)


def test_reverse_payload_order_is_normalized_in_each_window():
    end = START + 1900 * STEP_15M
    first_end = START + 1899 * STEP_15M
    session = _Session([
        _Response(payload=_range_payload(START, first_end, STEP_15M, reverse=True)),
        _Response(payload=_range_payload(first_end, end, STEP_15M, reverse=True)),
    ])

    result = _client(session).fetch_candles("BTC_USDT", "15m", START, end)

    assert len(result) == 1901
    assert result.index.is_monotonic_increasing
