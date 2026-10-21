"""Offline CSV dataset collector for the standalone EMA bot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import pandas as pd

from ema_candles import find_missing_candles, validate_candles


SUPPORTED_SYMBOLS = frozenset({
    "SOL_USDT",
    "XRP_USDT",
    "DOGE_USDT",
    "LINK_USDT",
    "AVAX_USDT",
})
_CSV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_DATASET_SCHEMA_VERSION = "1"
_SOURCE = "mexc_futures_public_api"


def _utc_boundary(value: object, field_name: str) -> pd.Timestamp:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be UTC-aware")
    timestamp = pd.Timestamp(value)
    if timestamp.minute % 15 != 0 or timestamp.second != 0 or timestamp.microsecond != 0:
        raise ValueError(f"{field_name} is not aligned to 15m")
    return timestamp.tz_convert("UTC")


def _iso(timestamp: pd.Timestamp | datetime) -> str:
    return pd.Timestamp(timestamp).tz_convert("UTC").isoformat().replace("+00:00", "Z")


def _logical_ranges(evaluation_start_at: datetime, evaluation_end_at: datetime) -> dict[str, pd.Timestamp]:
    evaluation_start = _utc_boundary(evaluation_start_at, "evaluation_start_at")
    evaluation_end = _utc_boundary(evaluation_end_at, "evaluation_end_at")
    if evaluation_start >= evaluation_end:
        raise ValueError("evaluation_start_at must be before evaluation_end_at")

    warmup_start = (evaluation_start - pd.Timedelta(hours=300)).floor("h")
    latest_completed_1h = evaluation_end.floor("h") - pd.Timedelta(hours=1)
    service_end = evaluation_end.ceil("h")
    return {
        "evaluation_start": evaluation_start,
        "evaluation_end": evaluation_end,
        "warmup_start": warmup_start,
        "one_hour_end": latest_completed_1h,
        "service_end": service_end,
    }


def _complete_range(frame: pd.DataFrame, timeframe: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    validated = validate_candles(frame, timeframe)
    trimmed = validated.loc[(validated.index >= start) & (validated.index <= end)].copy()
    missing = find_missing_candles(trimmed, timeframe, start.to_pydatetime(), end.to_pydatetime())
    if missing:
        raise ValueError(f"{timeframe} history is missing candle opens")
    return trimmed


def _csv_bytes(frame: pd.DataFrame) -> bytes:
    output = pd.DataFrame(
        {
            "timestamp": [_iso(timestamp) for timestamp in frame.index],
            "open": frame["open"].tolist(),
            "high": frame["high"].tolist(),
            "low": frame["low"].tolist(),
            "close": frame["close"].tolist(),
            "volume": frame["volume"].tolist(),
        },
        columns=_CSV_COLUMNS,
    )
    buffer = io.StringIO()
    output.to_csv(buffer, index=False, float_format="%.17g", lineterminator="\n")
    return buffer.getvalue().encode("utf-8")


def _write_files_atomically(output_directory: Path, files: dict[Path, bytes]) -> None:
    temporary_paths: list[Path] = []
    installed_paths: list[Path] = []
    try:
        for final_path in files:
            if final_path.exists():
                raise FileExistsError(f"dataset file already exists: {final_path.name}")

        for final_path, content in files.items():
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{final_path.name}.",
                suffix=".tmp",
                dir=output_directory,
            )
            temporary_path = Path(temporary_name)
            temporary_paths.append(temporary_path)
            os.close(handle)
            temporary_path.write_bytes(content)

        for temporary_path, final_path in zip(temporary_paths, files):
            os.replace(temporary_path, final_path)
            installed_paths.append(final_path)
    except BaseException:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
        for path in installed_paths:
            path.unlink(missing_ok=True)
        raise


def collect_dataset(
    service: Any,
    symbol: str,
    evaluation_start_at: datetime,
    evaluation_end_at: datetime,
    output_directory: str | os.PathLike[str],
) -> dict[str, Path]:
    """Collect a validated EMA history into CSV files and a JSON manifest.

    Evaluation boundaries are inclusive 15m candle-open timestamps. The
    1h output ends at the last 1h candle open whose close is no later than the
    evaluation end; a still-forming 1h candle is never included. The service
    may receive the next hour as a common request boundary, but that extra
    candle is discarded before validation and serialization.
    """
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError(f"unsupported symbol: {symbol}")
    if not callable(getattr(service, "fetch_history", None)):
        raise TypeError("service must provide fetch_history()")
    ranges = _logical_ranges(evaluation_start_at, evaluation_end_at)

    history = service.fetch_history(symbol, ranges["warmup_start"].to_pydatetime(), ranges["service_end"].to_pydatetime())
    candles_15m = _complete_range(
        history.candles_15m,
        "15m",
        ranges["warmup_start"],
        ranges["evaluation_end"],
    )
    candles_1h = _complete_range(
        history.candles_1h,
        "1h",
        ranges["warmup_start"],
        ranges["one_hour_end"],
    )

    output_path = Path(output_directory)
    output_path.mkdir(parents=True, exist_ok=True)
    csv_contents = {
        "15m": _csv_bytes(candles_15m),
        "1h": _csv_bytes(candles_1h),
    }
    csv_paths = {
        timeframe: output_path / f"{symbol}_{timeframe}.csv"
        for timeframe in ("15m", "1h")
    }
    checksums = {
        timeframe: hashlib.sha256(content).hexdigest()
        for timeframe, content in csv_contents.items()
    }
    manifest = {
        "dataset_schema_version": _DATASET_SCHEMA_VERSION,
        "symbol": symbol,
        "source": _SOURCE,
        "timeframes": ["15m", "1h"],
        "evaluation_start_at": _iso(ranges["evaluation_start"]),
        "evaluation_end_at": _iso(ranges["evaluation_end"]),
        "warmup_start_at": _iso(ranges["warmup_start"]),
        "requested_boundaries": {
            "15m": {
                "start_at": _iso(ranges["warmup_start"]),
                "end_at": _iso(ranges["evaluation_end"]),
            },
            "1h": {
                "start_at": _iso(ranges["warmup_start"]),
                "end_at": _iso(ranges["one_hour_end"]),
            },
        },
        "first_timestamps": {
            "15m": _iso(candles_15m.index[0]),
            "1h": _iso(candles_1h.index[0]),
        },
        "last_timestamps": {
            "15m": _iso(candles_15m.index[-1]),
            "1h": _iso(candles_1h.index[-1]),
        },
        "candle_counts": {
            "15m": len(candles_15m),
            "1h": len(candles_1h),
        },
        "sha256": checksums,
        "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    manifest_contents = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path = output_path / f"{symbol}_manifest.json"
    files = {
        csv_paths["15m"]: csv_contents["15m"],
        csv_paths["1h"]: csv_contents["1h"],
        manifest_path: manifest_contents,
    }
    _write_files_atomically(output_path, files)
    return {"15m": csv_paths["15m"], "1h": csv_paths["1h"], "manifest": manifest_path}
