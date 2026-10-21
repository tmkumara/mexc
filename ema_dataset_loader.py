"""Read-only integrity-checked loader for offline EMA candle datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from ema_candles import find_missing_candles, validate_candles
from ema_dataset_collector import (
    SUPPORTED_SYMBOLS,
    _DATASET_SCHEMA_VERSION,
    _SOURCE,
    _logical_ranges,
)


_CSV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class EmaHistoricalDataset:
    symbol: str
    candles_15m: pd.DataFrame
    candles_1h: pd.DataFrame
    evaluation_start_at: datetime
    evaluation_end_at: datetime
    warmup_start_at: datetime


def _manifest_timestamp(value: object, field_name: str) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ValueError(f"manifest field {field_name} must be a UTC timestamp")
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"manifest field {field_name} is not a valid timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"manifest field {field_name} must be UTC")
    return timestamp.tz_convert("UTC")


def _iso(timestamp: pd.Timestamp) -> str:
    return timestamp.isoformat().replace("+00:00", "Z")


def _require_mapping(value: object, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"manifest field {field_name} must be an object")
    return value


def _require_count(value: object, timeframe: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"manifest candle count for {timeframe} is invalid")
    return value


def _read_manifest(directory: Path, symbol: str) -> tuple[dict[str, Any], dict[str, pd.Timestamp]]:
    manifest_path = directory / f"{symbol}_manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest file is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")

    required = {
        "dataset_schema_version",
        "symbol",
        "source",
        "timeframes",
        "evaluation_start_at",
        "evaluation_end_at",
        "warmup_start_at",
        "requested_boundaries",
        "first_timestamps",
        "last_timestamps",
        "candle_counts",
        "sha256",
        "collected_at",
    }
    missing = sorted(required - manifest.keys())
    if missing:
        raise ValueError(f"manifest is missing required fields: {', '.join(missing)}")
    if manifest["dataset_schema_version"] != _DATASET_SCHEMA_VERSION:
        raise ValueError("unsupported dataset schema version")
    if manifest["symbol"] != symbol or symbol not in SUPPORTED_SYMBOLS:
        raise ValueError("manifest symbol does not match requested symbol")
    if manifest["source"] != _SOURCE:
        raise ValueError("manifest source does not match collector contract")
    if manifest["timeframes"] != ["15m", "1h"]:
        raise ValueError("manifest timeframes do not match collector contract")

    timestamps = {
        field: _manifest_timestamp(manifest[field], field)
        for field in ("evaluation_start_at", "evaluation_end_at", "warmup_start_at", "collected_at")
    }
    ranges = _logical_ranges(
        timestamps["evaluation_start_at"].to_pydatetime(),
        timestamps["evaluation_end_at"].to_pydatetime(),
    )
    if timestamps["warmup_start_at"] != ranges["warmup_start"]:
        raise ValueError("manifest warm-up boundary is inconsistent")

    boundaries = _require_mapping(manifest["requested_boundaries"], "requested_boundaries")
    for timeframe, end_key in (("15m", "evaluation_end"), ("1h", "one_hour_end")):
        boundary = _require_mapping(boundaries.get(timeframe), f"requested_boundaries.{timeframe}")
        start = _manifest_timestamp(boundary.get("start_at"), f"{timeframe}.start_at")
        end = _manifest_timestamp(boundary.get("end_at"), f"{timeframe}.end_at")
        if start != ranges["warmup_start"] or end != ranges[end_key]:
            raise ValueError(f"manifest {timeframe} boundary is inconsistent")

    for field in ("first_timestamps", "last_timestamps"):
        values = _require_mapping(manifest[field], field)
        for timeframe in ("15m", "1h"):
            _manifest_timestamp(values.get(timeframe), f"{field}.{timeframe}")
    counts = _require_mapping(manifest["candle_counts"], "candle_counts")
    for timeframe in ("15m", "1h"):
        _require_count(counts.get(timeframe), timeframe)
    checksums = _require_mapping(manifest["sha256"], "sha256")
    for timeframe in ("15m", "1h"):
        checksum = checksums.get(timeframe)
        if not isinstance(checksum, str) or len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum.lower()):
            raise ValueError(f"manifest checksum for {timeframe} is invalid")

    return manifest, timestamps


def _read_csv(directory: Path, symbol: str, timeframe: str, expected_checksum: str) -> pd.DataFrame:
    path = directory / f"{symbol}_{timeframe}.csv"
    if not path.is_file():
        raise ValueError(f"CSV file is missing: {path.name}")
    try:
        actual_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(f"could not read CSV file: {path.name}") from exc
    if actual_checksum != expected_checksum:
        raise ValueError(f"CSV checksum mismatch: {path.name}")
    try:
        raw = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        raise ValueError(f"CSV could not be parsed: {path.name}") from exc
    if list(raw.columns) != _CSV_COLUMNS:
        raise ValueError(f"CSV columns do not match contract: {path.name}")
    try:
        timestamps = pd.DatetimeIndex(pd.to_datetime(raw["timestamp"], utc=True, errors="raise"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"CSV timestamps are invalid: {path.name}") from exc
    if timestamps.hasnans:
        raise ValueError(f"CSV timestamps contain missing values: {path.name}")
    frame = raw.drop(columns=["timestamp"]).copy()
    frame.index = timestamps
    try:
        return validate_candles(frame, timeframe)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"CSV candle data is invalid: {path.name}: {exc}") from exc


def _validate_frame(
    frame: pd.DataFrame,
    timeframe: str,
    manifest: dict[str, Any],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    counts = manifest["candle_counts"]
    if len(frame) != counts[timeframe]:
        raise ValueError(f"manifest candle count does not match {timeframe} CSV")
    if frame.empty or frame.index[0] != start or frame.index[-1] != end:
        raise ValueError(f"{timeframe} CSV boundary does not match manifest")
    missing = find_missing_candles(frame, timeframe, start.to_pydatetime(), end.to_pydatetime())
    if missing:
        raise ValueError(f"{timeframe} CSV has missing candle opens")
    return frame


def load_dataset(directory: str | Path, symbol: str) -> EmaHistoricalDataset:
    """Load and integrity-check one collector-produced EMA dataset."""
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(f"unsupported symbol: {symbol}")
    directory_path = Path(directory)
    if not directory_path.is_dir():
        raise ValueError("dataset directory does not exist")
    manifest, timestamps = _read_manifest(directory_path, symbol)
    checksums = manifest["sha256"]
    candles_15m = _read_csv(directory_path, symbol, "15m", checksums["15m"])
    candles_1h = _read_csv(directory_path, symbol, "1h", checksums["1h"])
    ranges = _logical_ranges(
        timestamps["evaluation_start_at"].to_pydatetime(),
        timestamps["evaluation_end_at"].to_pydatetime(),
    )
    candles_15m = _validate_frame(candles_15m, "15m", manifest, ranges["warmup_start"], ranges["evaluation_end"])
    candles_1h = _validate_frame(candles_1h, "1h", manifest, ranges["warmup_start"], ranges["one_hour_end"])

    first = manifest["first_timestamps"]
    last = manifest["last_timestamps"]
    if _manifest_timestamp(first["15m"], "first_timestamps.15m") != candles_15m.index[0] or _manifest_timestamp(last["15m"], "last_timestamps.15m") != candles_15m.index[-1]:
        raise ValueError("15m manifest timestamps do not match CSV")
    if _manifest_timestamp(first["1h"], "first_timestamps.1h") != candles_1h.index[0] or _manifest_timestamp(last["1h"], "last_timestamps.1h") != candles_1h.index[-1]:
        raise ValueError("1h manifest timestamps do not match CSV")
    return EmaHistoricalDataset(
        symbol=symbol,
        candles_15m=candles_15m,
        candles_1h=candles_1h,
        evaluation_start_at=timestamps["evaluation_start_at"].to_pydatetime(),
        evaluation_end_at=timestamps["evaluation_end_at"].to_pydatetime(),
        warmup_start_at=timestamps["warmup_start_at"].to_pydatetime(),
    )
