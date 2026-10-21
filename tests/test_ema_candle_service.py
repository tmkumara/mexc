from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ema_candle_service import EmaCandleService


UTC = timezone.utc
START = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
END = datetime(2026, 1, 2, 13, 0, tzinfo=UTC)


def _frame(timeframe):
    if timeframe == "15m":
        index = pd.to_datetime(["2026-01-02T12:00:00Z", "2026-01-02T12:15:00Z"])
    else:
        index = pd.to_datetime(["2026-01-02T12:00:00Z", "2026-01-02T13:00:00Z"])
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
        },
        index=index,
    )


class FakeClient:
    def __init__(self, *, fail_timeframe=None, invalid_timeframe=None):
        self.fail_timeframe = fail_timeframe
        self.invalid_timeframe = invalid_timeframe
        self.calls = []
        self.frames = {"15m": _frame("15m"), "1h": _frame("1h")}

    def fetch_candles(self, symbol, timeframe, start_at, end_at):
        self.calls.append((symbol, timeframe, start_at, end_at))
        if timeframe == self.fail_timeframe:
            raise RuntimeError(f"{timeframe} fetch failed")
        frame = self.frames[timeframe]
        if timeframe == self.invalid_timeframe:
            frame = frame.copy()
            frame.iloc[0, frame.columns.get_loc("high")] = 98.0
        return frame


@pytest.mark.parametrize("symbol", ["SOL_USDT", "XRP_USDT", "DOGE_USDT", "LINK_USDT", "AVAX_USDT"])
def test_each_supported_symbol_is_accepted(symbol):
    client = FakeClient()

    result = EmaCandleService(client).fetch_history(symbol, START, END)

    assert list(result.candles_15m.index) == list(_frame("15m").index)
    assert list(result.candles_1h.index) == list(_frame("1h").index)
    assert len(client.calls) == 2


def test_unsupported_symbol_is_rejected_before_client_call():
    client = FakeClient()

    with pytest.raises(ValueError, match="unsupported symbol"):
        EmaCandleService(client).fetch_history("BTC_USDT", START, END)

    assert client.calls == []


def test_naive_datetime_is_rejected_before_client_call():
    client = FakeClient()

    with pytest.raises(ValueError, match="UTC-aware"):
        EmaCandleService(client).fetch_history("SOL_USDT", datetime(2026, 1, 2, 12), END)

    assert client.calls == []


def test_non_utc_aware_datetime_is_rejected_before_client_call():
    client = FakeClient()
    non_utc = datetime(2026, 1, 2, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    non_utc_end = datetime(2026, 1, 2, 15, 0, tzinfo=timezone(timedelta(hours=2)))

    with pytest.raises(ValueError, match="UTC-aware"):
        EmaCandleService(client).fetch_history("SOL_USDT", non_utc, non_utc_end)

    assert client.calls == []


def test_invalid_start_end_order_is_rejected_before_client_call():
    client = FakeClient()

    with pytest.raises(ValueError, match="before end"):
        EmaCandleService(client).fetch_history("SOL_USDT", END, START)

    assert client.calls == []


def test_client_receives_correct_timeframes_and_boundaries():
    client = FakeClient()

    EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    assert client.calls == [
        ("SOL_USDT", "15m", START, END),
        ("SOL_USDT", "1h", START, END),
    ]


def test_both_datasets_are_returned_separately_and_validated():
    client = FakeClient()

    result = EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    assert result.candles_15m is not client.frames["15m"]
    assert result.candles_1h is not client.frames["1h"]
    assert result.candles_15m.index.is_monotonic_increasing
    assert result.candles_1h.index.is_monotonic_increasing
    assert result.candles_15m.index.tz is not None
    assert result.candles_1h.index.tz is not None


def test_15m_fetch_failure_returns_no_partial_result():
    client = FakeClient(fail_timeframe="15m")

    with pytest.raises(RuntimeError, match="15m fetch failed"):
        EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    assert [call[1] for call in client.calls] == ["15m"]


def test_1h_fetch_failure_returns_no_partial_result():
    client = FakeClient(fail_timeframe="1h")

    with pytest.raises(RuntimeError, match="1h fetch failed"):
        EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    assert [call[1] for call in client.calls] == ["15m", "1h"]


def test_service_does_not_retry_at_service_layer():
    client = FakeClient(fail_timeframe="15m")

    with pytest.raises(RuntimeError):
        EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    assert len(client.calls) == 1


def test_client_frames_are_not_mutated():
    client = FakeClient()
    original = {timeframe: deepcopy(frame) for timeframe, frame in client.frames.items()}

    EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    for timeframe in ("15m", "1h"):
        pd.testing.assert_frame_equal(client.frames[timeframe], original[timeframe])


def test_invalid_client_candle_data_is_rejected():
    client = FakeClient(invalid_timeframe="1h")

    with pytest.raises(ValueError, match="OHLC"):
        EmaCandleService(client).fetch_history("SOL_USDT", START, END)


def test_candle_open_timestamps_are_preserved_exactly():
    client = FakeClient()

    result = EmaCandleService(client).fetch_history("SOL_USDT", START, END)

    assert result.candles_15m.index[0].to_pydatetime() == START
    assert result.candles_15m.index[1].to_pydatetime() == datetime(2026, 1, 2, 12, 15, tzinfo=UTC)
    assert result.candles_1h.index[0].to_pydatetime() == START
