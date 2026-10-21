from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pandas as pd
import pytest

from ema_dataset_collector import collect_dataset


UTC = timezone.utc
EVALUATION_START = datetime(2026, 1, 2, 12, 15, tzinfo=UTC)
EVALUATION_END = datetime(2026, 1, 2, 13, 45, tzinfo=UTC)
WARMUP_START = datetime(2025, 12, 21, 0, 0, tzinfo=UTC)
ONE_HOUR_END = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _frame(start, end, step):
    index = pd.date_range(start=start, end=end, freq=step, tz="UTC")
    values = list(range(len(index)))
    return pd.DataFrame(
        {
            "open": [100.0 + value for value in values],
            "high": [101.0 + value for value in values],
            "low": [99.0 + value for value in values],
            "close": [100.5 + value for value in values],
            "volume": [10.0 + value for value in values],
        },
        index=index,
    )


class FakeService:
    def __init__(self, *, missing=None, fail=False):
        self.missing = missing
        self.fail = fail
        self.calls = []
        self.frames = {
            "15m": _frame(WARMUP_START, EVALUATION_END, "15min"),
            "1h": _frame(WARMUP_START, EVALUATION_END + timedelta(hours=1), "1h"),
        }

    def fetch_history(self, symbol, start_at, end_at):
        self.calls.append((symbol, start_at, end_at))
        if self.fail:
            raise RuntimeError("service failure")
        frames = {key: value.copy(deep=True) for key, value in self.frames.items()}
        if self.missing:
            frames[self.missing] = frames[self.missing].drop(frames[self.missing].index[10])
        return type("History", (), {"candles_15m": frames["15m"], "candles_1h": frames["1h"]})()


def _collect(tmp_path, service=None, **kwargs):
    return collect_dataset(
        service or FakeService(),
        "SOL_USDT",
        EVALUATION_START,
        EVALUATION_END,
        tmp_path,
        **kwargs,
    )


def test_valid_collection_writes_two_csv_files_and_manifest(tmp_path):
    result = _collect(tmp_path)

    assert set(result) == {"15m", "1h", "manifest"}
    assert (tmp_path / "SOL_USDT_15m.csv").exists()
    assert (tmp_path / "SOL_USDT_1h.csv").exists()
    assert (tmp_path / "SOL_USDT_manifest.json").exists()


@pytest.mark.parametrize("symbol", ["SOL_USDT", "XRP_USDT", "DOGE_USDT", "LINK_USDT", "AVAX_USDT"])
def test_supported_symbols_are_passed_to_service(tmp_path, symbol):
    service = FakeService()

    collect_dataset(service, symbol, EVALUATION_START, EVALUATION_END, tmp_path)

    assert service.calls[0][0] == symbol


def test_warmup_is_300_hours_and_15m_starts_at_same_boundary(tmp_path):
    service = FakeService()

    _collect(tmp_path, service)

    assert service.calls == [("SOL_USDT", WARMUP_START, datetime(2026, 1, 2, 14, 0, tzinfo=UTC))]
    manifest = json.loads((tmp_path / "SOL_USDT_manifest.json").read_text())
    assert manifest["warmup_start_at"] == WARMUP_START.isoformat().replace("+00:00", "Z")
    assert manifest["requested_boundaries"]["15m"]["start_at"] == manifest["warmup_start_at"]
    assert manifest["requested_boundaries"]["1h"]["start_at"] == manifest["warmup_start_at"]


def test_boundaries_are_inclusive_and_1h_excludes_forming_candle(tmp_path):
    _collect(tmp_path)

    frame_15m = pd.read_csv(tmp_path / "SOL_USDT_15m.csv")
    frame_1h = pd.read_csv(tmp_path / "SOL_USDT_1h.csv")
    assert frame_15m["timestamp"].iloc[-1] == EVALUATION_END.isoformat().replace("+00:00", "Z")
    assert frame_1h["timestamp"].iloc[-1] == ONE_HOUR_END.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize(
    "start,end,pattern",
    [
        (datetime(2026, 1, 2, 12, 15), EVALUATION_END, "UTC-aware"),
        (datetime(2026, 1, 2, 12, 7, tzinfo=UTC), EVALUATION_END, "15m"),
    ],
)
def test_invalid_evaluation_boundaries_are_rejected(tmp_path, start, end, pattern):
    with pytest.raises(ValueError, match=pattern):
        collect_dataset(FakeService(), "SOL_USDT", start, end, tmp_path)


def test_end_before_start_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="before evaluation_end"):
        collect_dataset(FakeService(), "SOL_USDT", EVALUATION_END, EVALUATION_START, tmp_path)


def test_unsupported_symbol_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported symbol"):
        collect_dataset(FakeService(), "BTC_USDT", EVALUATION_START, EVALUATION_END, tmp_path)


@pytest.mark.parametrize("missing", ["15m", "1h"])
def test_missing_candle_is_rejected(tmp_path, missing):
    with pytest.raises(ValueError, match="missing"):
        _collect(tmp_path, FakeService(missing=missing))


def test_csv_schema_order_and_ascending_timestamps(tmp_path):
    _collect(tmp_path)

    for timeframe in ("15m", "1h"):
        frame = pd.read_csv(tmp_path / f"SOL_USDT_{timeframe}.csv")
        assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        assert timestamps.is_monotonic_increasing
        assert timestamps.is_unique


def test_manifest_counts_boundaries_and_checksums_are_correct(tmp_path):
    _collect(tmp_path)
    manifest = json.loads((tmp_path / "SOL_USDT_manifest.json").read_text())

    assert manifest["dataset_schema_version"] == "1"
    assert manifest["symbol"] == "SOL_USDT"
    assert manifest["timeframes"] == ["15m", "1h"]
    for timeframe in ("15m", "1h"):
        path = tmp_path / f"SOL_USDT_{timeframe}.csv"
        assert manifest["candle_counts"][timeframe] == len(path.read_text().splitlines()) - 1
        assert manifest["sha256"][timeframe] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_existing_files_are_not_overwritten_by_default(tmp_path):
    path = tmp_path / "SOL_USDT_15m.csv"
    path.write_text("keep me")

    with pytest.raises(FileExistsError, match="already exists"):
        _collect(tmp_path)

    assert path.read_text() == "keep me"


def test_serialization_failure_cleans_temporary_files(tmp_path, monkeypatch):
    def fail_to_csv(self, *args, **kwargs):
        raise OSError("serialization failed")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail_to_csv)

    with pytest.raises(OSError, match="serialization failed"):
        _collect(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_file_write_failure_cleans_temporary_and_final_files(tmp_path, monkeypatch):
    import ema_dataset_collector

    original_replace = ema_dataset_collector.os.replace

    def fail_on_second_replace(source, destination):
        if str(destination).endswith("SOL_USDT_1h.csv"):
            raise OSError("replace failed")
        original_replace(source, destination)

    monkeypatch.setattr(ema_dataset_collector.os, "replace", fail_on_second_replace)

    with pytest.raises(OSError, match="replace failed"):
        _collect(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_service_failure_does_not_create_partial_output(tmp_path):
    output_directory = tmp_path / "out"
    with pytest.raises(RuntimeError, match="service failure"):
        _collect(output_directory, FakeService(fail=True))

    assert not output_directory.exists()


def test_input_frames_are_not_mutated(tmp_path):
    service = FakeService()
    original = {key: deepcopy(value) for key, value in service.frames.items()}

    _collect(tmp_path, service)

    for key in original:
        pd.testing.assert_frame_equal(service.frames[key], original[key])


def test_collector_does_not_use_live_api_or_database(tmp_path):
    service = FakeService()

    _collect(tmp_path, service)

    assert len(service.calls) == 1
