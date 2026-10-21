from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pandas as pd
import pytest

from ema_dataset_collector import collect_dataset
from ema_dataset_loader import load_dataset


UTC = timezone.utc
START = datetime(2026, 1, 2, 12, 15, tzinfo=UTC)
END = datetime(2026, 1, 2, 13, 45, tzinfo=UTC)
WARMUP = datetime(2025, 12, 21, 0, 0, tzinfo=UTC)
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
    def __init__(self):
        self.frames = {
            "15m": _frame(WARMUP, END, "15min"),
            "1h": _frame(WARMUP, END + timedelta(hours=1), "1h"),
        }
        self.calls = []

    def fetch_history(self, symbol, start_at, end_at):
        self.calls.append((symbol, start_at, end_at))
        return type(
            "History",
            (),
            {
                "candles_15m": self.frames["15m"].copy(deep=True),
                "candles_1h": self.frames["1h"].copy(deep=True),
            },
        )()


def _create_dataset(directory, symbol="SOL_USDT"):
    collect_dataset(FakeService(), symbol, START, END, directory)


def _manifest(directory, symbol="SOL_USDT"):
    path = directory / f"{symbol}_manifest.json"
    return json.loads(path.read_text())


def _write_manifest(directory, manifest, symbol="SOL_USDT"):
    (directory / f"{symbol}_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _refresh_checksum(directory, timeframe, manifest):
    path = directory / f"SOL_USDT_{timeframe}.csv"
    manifest["sha256"][timeframe] = hashlib.sha256(path.read_bytes()).hexdigest()


def test_successful_load_returns_immutable_dataset_and_metadata(tmp_path):
    _create_dataset(tmp_path)

    result = load_dataset(tmp_path, "SOL_USDT")

    assert result.symbol == "SOL_USDT"
    assert result.evaluation_start_at == START
    assert result.evaluation_end_at == END
    assert result.warmup_start_at == WARMUP
    assert result.candles_15m.index.tz is not None
    assert result.candles_1h.index.tz is not None
    with pytest.raises(Exception):
        result.symbol = "XRP_USDT"


@pytest.mark.parametrize("symbol", ["SOL_USDT", "XRP_USDT", "DOGE_USDT", "LINK_USDT", "AVAX_USDT"])
def test_all_supported_symbols_load(tmp_path, symbol):
    directory = tmp_path / symbol
    _create_dataset(directory, symbol)

    assert load_dataset(directory, symbol).symbol == symbol


def test_missing_manifest_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="manifest"):
        load_dataset(tmp_path, "SOL_USDT")


def test_invalid_manifest_json_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    (tmp_path / "SOL_USDT_manifest.json").write_text("not json")

    with pytest.raises(ValueError, match="JSON"):
        load_dataset(tmp_path, "SOL_USDT")


def test_unsupported_schema_version_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["dataset_schema_version"] = "2"
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="schema"):
        load_dataset(tmp_path, "SOL_USDT")


def test_symbol_mismatch_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["symbol"] = "XRP_USDT"
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="symbol"):
        load_dataset(tmp_path, "SOL_USDT")


@pytest.mark.parametrize("filename", ["SOL_USDT_15m.csv", "SOL_USDT_1h.csv"])
def test_missing_csv_is_rejected(tmp_path, filename):
    _create_dataset(tmp_path)
    (tmp_path / filename).unlink()

    with pytest.raises(ValueError, match="CSV"):
        load_dataset(tmp_path, "SOL_USDT")


def test_checksum_mismatch_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    path.write_bytes(path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="checksum"):
        load_dataset(tmp_path, "SOL_USDT")


def test_modified_csv_content_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    path.write_text(path.read_text().replace(",100.5,", ",999.5,", 1))

    with pytest.raises(ValueError, match="checksum"):
        load_dataset(tmp_path, "SOL_USDT")


def test_missing_candle_is_rejected_after_checksum_update(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    frame = pd.read_csv(path).drop(index=10)
    frame.to_csv(path, index=False)
    _refresh_checksum(tmp_path, "15m", manifest)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="count|missing|continuity"):
        load_dataset(tmp_path, "SOL_USDT")


def test_duplicate_timestamp_is_rejected_after_checksum_update(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    frame = pd.read_csv(path)
    frame.loc[11, "timestamp"] = frame.loc[10, "timestamp"]
    frame.to_csv(path, index=False)
    _refresh_checksum(tmp_path, "15m", manifest)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(tmp_path, "SOL_USDT")


def test_out_of_order_timestamp_is_rejected_after_checksum_update(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    frame = pd.read_csv(path)
    frame.iloc[[10, 11]] = frame.iloc[[11, 10]].to_numpy()
    frame.to_csv(path, index=False)
    _refresh_checksum(tmp_path, "15m", manifest)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="ascending|chronological"):
        load_dataset(tmp_path, "SOL_USDT")


def test_invalid_ohlcv_is_rejected_after_checksum_update(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    frame = pd.read_csv(path)
    frame.loc[0, "high"] = 1
    frame.to_csv(path, index=False)
    _refresh_checksum(tmp_path, "15m", manifest)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="OHLC"):
        load_dataset(tmp_path, "SOL_USDT")


def test_incorrect_manifest_count_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["candle_counts"]["15m"] += 1
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="count"):
        load_dataset(tmp_path, "SOL_USDT")


def test_incorrect_manifest_boundaries_are_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["requested_boundaries"]["15m"]["end_at"] = "2026-01-02T13:30:00Z"
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="boundary"):
        load_dataset(tmp_path, "SOL_USDT")


def test_incorrect_1h_logical_end_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["requested_boundaries"]["1h"]["end_at"] = "2026-01-02T13:00:00Z"
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="boundary"):
        load_dataset(tmp_path, "SOL_USDT")


def test_missing_warmup_candle_is_rejected_after_checksum_update(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    path = tmp_path / "SOL_USDT_1h.csv"
    frame = pd.read_csv(path).drop(index=0)
    frame.to_csv(path, index=False)
    _refresh_checksum(tmp_path, "1h", manifest)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="count|missing|boundary"):
        load_dataset(tmp_path, "SOL_USDT")


def test_invalid_timezone_is_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["evaluation_start_at"] = "2026-01-02T12:15:00"
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="UTC"):
        load_dataset(tmp_path, "SOL_USDT")


def test_unexpected_csv_columns_are_rejected(tmp_path):
    _create_dataset(tmp_path)
    manifest = _manifest(tmp_path)
    path = tmp_path / "SOL_USDT_15m.csv"
    frame = pd.read_csv(path)
    frame["extra"] = 1
    frame.to_csv(path, index=False)
    _refresh_checksum(tmp_path, "15m", manifest)
    _write_manifest(tmp_path, manifest)

    with pytest.raises(ValueError, match="columns"):
        load_dataset(tmp_path, "SOL_USDT")


def test_loaded_values_are_exact_and_files_are_not_mutated(tmp_path):
    _create_dataset(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    result = load_dataset(tmp_path, "SOL_USDT")

    assert result.candles_15m.iloc[0]["open"] == 100.0
    assert result.candles_15m.iloc[0]["close"] == 100.5
    assert result.candles_1h.iloc[-1]["volume"] == 310.0
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert after == before


def test_loader_has_no_network_or_database_dependency(tmp_path):
    _create_dataset(tmp_path)

    result = load_dataset(tmp_path, "SOL_USDT")

    assert len(result.candles_15m) > 0
    assert len(result.candles_1h) > 0
