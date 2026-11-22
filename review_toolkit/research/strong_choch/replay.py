"""Chronological Strong CHoCH replay of local data."""

import argparse
import csv
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from strategy.market_structure.discovery import (
    RANGE_EDGE_FRACTION, BreakDiscovery, DiscoveryEvent, DiscoveryLevel, DiscoveryRange,
)
from strategy.market_structure.internal import InternalStructureDetector
from strategy.market_structure.models import CHoCHEvent, StructureBreak, StructureSnapshot, SwingPoint
from strategy.market_structure.structure import Parameters, StructureDetector

from .data import add_source_arguments, load_from_args, utc, write_candles


def replay(candles, parameters=Parameters()):
    detector = StructureDetector(parameters)
    for candle in candles:
        detector.update(candle)
    return detector


class StructureReplay:
    def __init__(self, parameters=Parameters()):
        self.external = StructureDetector(parameters)
        self.internal = InternalStructureDetector(parameters)
        self.discovery = BreakDiscovery(parameters)

    def update(self, candle):
        external = self.external.snapshots[-1] if self.external.snapshots else None
        internal = self.internal.snapshots[-1] if self.internal.snapshots else None
        self.external.update(candle)
        self.internal.update(candle)
        self.discovery.update(candle, external, internal)


def replay_layers(candles, parameters=Parameters()):
    layers = StructureReplay(parameters)
    for candle in candles:
        layers.update(candle)
    return layers


def write_json(path, payload):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
        handle.write("\n")


def write_rows(path, columns, rows):
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def model_rows(path, cls, items, **extra_columns):
    def rows():
        for item in items:
            row = asdict(item)
            for key, value in row.items():
                if isinstance(value, tuple):
                    row[key] = "|".join(reason.value for reason in value)
                elif hasattr(value, "value"):
                    row[key] = value.value
            row.update({key: extractor(item) for key, extractor in extra_columns.items()})
            yield row

    write_rows(path, [field.name for field in fields(cls)] + list(extra_columns), rows())


def run(args):
    now = datetime.now(timezone.utc)
    parameters = Parameters(**{key: getattr(args, key) for key in asdict(Parameters())})
    bars, dropped = load_from_args(args, now)
    start = args.start or bars[0].timestamp
    end = args.end or bars[-1].closed_at
    bars = [bar for bar in bars if bar.closed_at <= end]
    eligible = [bar for bar in bars if bar.timestamp >= start]
    if not eligible or start != eligible[0].timestamp or end != eligible[-1].closed_at:
        raise ValueError("Evaluation boundaries must be covered, UTC-aligned 4H boundaries")
    warmup = len(bars) - len(eligible)
    layers = replay_layers(bars, parameters)
    detector, internal = layers.external, layers.internal

    events = [event for event in detector.choch_events if event.event_timestamp > start]
    external = detector.external_swings
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "run_metadata.json", {
        "algorithm_version": "2.3.0", "symbol": args.symbol, "timeframe": "4h",
        "source_timeframe": args.source_timeframe, "source_path": str(args.input.resolve()),
        "source_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "executed_at": now.isoformat(), "as_of": (args.as_of or now).isoformat(),
        "evaluation_start_inclusive": start.isoformat(), "evaluation_end_exclusive": end.isoformat(),
        "warmup_start_inclusive": bars[0].timestamp.isoformat() if warmup else None,
        "warmup_end_exclusive": start.isoformat() if warmup else None,
        "warmup_candles": warmup, "processed_candles": len(bars),
        "evaluated_candles": len(eligible), "dropped_trailing_source_rows": dropped,
        "parameters": asdict(parameters),
        "internal_parameters": asdict(internal.parameters),
        "discovery_range_edge_fraction": RANGE_EDGE_FRACTION,
    })
    write_candles(args.output / "candles_4h.csv", bars)
    model_rows(args.output / "confirmed_swings.csv", SwingPoint, detector.confirmed_swings,
               external=lambda swing: swing in external, external_since=external.get)
    model_rows(args.output / "structure_states.csv", StructureSnapshot, detector.snapshots)
    model_rows(args.output / "structure_events.csv", StructureBreak, detector.structure_events,
               in_evaluation=lambda event: event.break_timestamp > start)
    model_rows(args.output / "internal_swings.csv", SwingPoint, internal.confirmed_swings,
               internal=lambda swing: swing in internal.structural_swings,
               internal_since=internal.structural_swings.get)
    write_rows(args.output / "internal_states.csv",
               ("timestamp", "state", "internal_high", "internal_low", "continuation_armed"),
               (dict(timestamp=row.timestamp, state=row.state.value, internal_high=row.external_high,
                     internal_low=row.external_low, continuation_armed=row.continuation_armed)
                for row in internal.snapshots))
    model_rows(args.output / "internal_events.csv", StructureBreak, internal.structure_events,
               in_evaluation=lambda event: event.break_timestamp > start)
    discovery = layers.discovery
    model_rows(args.output / "discovered_levels.csv", DiscoveryLevel, discovery.levels)
    model_rows(args.output / "discovered_ranges.csv", DiscoveryRange, discovery.ranges)
    model_rows(args.output / "discovered_breaks.csv", DiscoveryEvent, discovery.events,
               in_evaluation=lambda event: event.break_timestamp > start)
    model_rows(args.output / "choch_candidates.csv", CHoCHEvent, events)
    model_rows(args.output / "strong_choch_events.csv", CHoCHEvent,
               (event for event in events if event.strong_choch))
    model_rows(args.output / "rejected_candidates.csv", CHoCHEvent,
               (event for event in events if not event.strong_choch))
    print(f"{args.symbol}: {len(bars)} closed 4H bars, {len(events)} CHoCH candidates; {args.output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_source_arguments(parser)
    parser.add_argument("--timeframe", choices=["4h"], default="4h")
    parser.add_argument("--start", type=utc, help="First evaluated candle open; earlier bars warm up")
    parser.add_argument("--end", type=utc, help="Exclusive candle-open boundary")
    parser.add_argument("--output", type=Path, required=True, help="New research run directory")
    for key, value in asdict(Parameters()).items():
        parser.add_argument("--" + key.replace("_", "-"), type=type(value), default=value)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
