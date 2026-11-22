"""Read local OHLCV and aggregate without filling gaps."""

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from strategy.market_structure.models import Candle, FOUR_HOURS
from strategy.market_structure.structure import validate_bar


INTERVALS = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "4h": FOUR_HOURS}
OHLCV = ("open", "high", "low", "close", "volume")


def utc(value):
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def load_candles(path, symbol, frequency, as_of, drop_incomplete_final=False):
    interval = INTERVALS[frequency]
    source = []
    dropped = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not {"timestamp", *OHLCV}.issubset(reader.fieldnames or ()):
            raise ValueError("Expected timestamp, open, high, low, close, volume columns")
        for row in reader:
            if "symbol" in row and row["symbol"] != symbol:
                raise ValueError(f"CSV symbol {row['symbol']} does not match {symbol}")
            candle = Candle(symbol, utc(row["timestamp"]), *(float(row[key]) for key in OHLCV))
            validate_bar(candle, interval, source[-1] if source else None)
            source.append(candle)

    if source and source[-1].timestamp >= as_of:
        raise ValueError("Dataset contains a candle that has not opened at --as-of")
    if source and source[-1].timestamp + interval > as_of:
        if not drop_incomplete_final:
            raise ValueError("Final source candle has not closed at --as-of")
        source.pop()
        dropped += 1
    if not source or source[0].timestamp.hour % 4 or source[0].timestamp.minute:
        raise ValueError("Source must start on a UTC 4H boundary and contain complete data")

    size = FOUR_HOURS // interval
    remainder = len(source) % size
    if remainder:
        if not drop_incomplete_final:
            raise ValueError("Final UTC 4H group is incomplete")
        dropped += remainder
        source = source[:-remainder]
    if not source:
        raise ValueError("No complete 4H candles")

    bars = []
    for index in range(0, len(source), size):
        group = source[index:index + size]
        bars.append(Candle(
            symbol, group[0].timestamp, group[0].open, max(bar.high for bar in group),
            min(bar.low for bar in group), group[-1].close, sum(bar.volume for bar in group),
        ))
    return bars, dropped


def write_candles(path, candles):
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("symbol", "timestamp", *OHLCV))
        writer.writeheader()
        writer.writerows(map(asdict, candles))


def add_source_arguments(parser):
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-timeframe", choices=INTERVALS, default="4h")
    parser.add_argument("--as-of", type=utc, help="UTC availability cutoff, defaults to current UTC")
    parser.add_argument("--drop-incomplete-final", action="store_true")


def load_from_args(args, now):
    return load_candles(args.input, args.symbol, args.source_timeframe,
                        args.as_of or now, args.drop_incomplete_final)


def main():
    parser = argparse.ArgumentParser(description="Prepare a 4H CSV from local OHLCV")
    add_source_arguments(parser)
    parser.add_argument("--output", type=Path, required=True, help="New 4H CSV file")
    args = parser.parse_args()
    bars, dropped = load_from_args(args, datetime.now(timezone.utc))
    write_candles(args.output, bars)
    print(f"Prepared {len(bars)} UTC 4H candles; discarded trailing source rows: {dropped}")


if __name__ == "__main__":
    main()
