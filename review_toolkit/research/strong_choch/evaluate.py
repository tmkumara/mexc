"""Measure future price behavior after recorded CHoCH candidates."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
from statistics import mean

from .data import load_candles, utc
from .replay import write_json, write_rows


HORIZONS = (3, 6, 12, 24)
METRICS = ("maximum_favorable_excursion_pct", "maximum_adverse_excursion_pct",
           "directional_close_return_pct")
COLUMNS = ("event_id", "symbol", "direction", "event_timestamp", "strong_choch",
           "horizon", "available_bars", "complete", *METRICS)
RELATION_COLUMNS = (
    "symbol", "classification", "direction", "event_timestamp", "horizon",
    "available_bars", "complete", "external_choch_follows", "same_direction_follows",
    "next_external_choch_timestamp", "bars_to_next_external_choch",
    "next_same_direction_timestamp", "bars_to_next_same_direction",
)


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def forward_rows(candles, candidates):
    indices = {candle.closed_at: index for index, candle in enumerate(candles)}
    for event in candidates:
        index = indices[utc(event["event_timestamp"])]
        base = float(event["break_close"])
        bullish = event["direction"] == "bullish"
        for horizon in HORIZONS:
            future = candles[index + 1:index + 1 + horizon]
            row = {key: event[key] for key in COLUMNS[:5]}
            row.update(horizon=horizon, available_bars=len(future), complete=len(future) == horizon)
            if row["complete"]:
                high = max(candle.high for candle in future)
                low = min(candle.low for candle in future)
                favorable = high - base if bullish else base - low
                adverse = base - low if bullish else high - base
                change = future[-1].close - base if bullish else base - future[-1].close
                row.update(zip(METRICS, (100 * max(0, favorable) / base,
                                         100 * max(0, adverse) / base, 100 * change / base)))
            yield row


def internal_relationships(candles, internal_events, external_choch):
    indices = {candle.closed_at: index for index, candle in enumerate(candles)}
    all_times = sorted(utc(event["event_timestamp"]) for event in external_choch)
    by_direction = defaultdict(list)
    for event in sorted(external_choch, key=lambda row: utc(row["event_timestamp"])):
        by_direction[event["direction"]].append(utc(event["event_timestamp"]))

    def next_after(times, stamp):
        offset = bisect_right(times, stamp)
        if offset == len(times):
            return None, None
        following = times[offset]
        return following, indices[following] - indices[stamp]

    for event in internal_events:
        stamp = utc(event["break_timestamp"])
        available = len(candles) - 1 - indices[stamp]
        following, distance = next_after(all_times, stamp)
        matching, same_distance = next_after(by_direction[event["direction"]], stamp)
        for horizon in HORIZONS:
            complete = available >= horizon
            follows = distance is not None and distance <= horizon
            same = same_distance is not None and same_distance <= horizon
            yield dict(
                symbol=event["symbol"], classification=event["classification"],
                direction=event["direction"], event_timestamp=stamp, horizon=horizon,
                available_bars=min(available, horizon), complete=complete,
                external_choch_follows=follows if complete or follows else None,
                same_direction_follows=same if complete or same else None,
                next_external_choch_timestamp=following, bars_to_next_external_choch=distance,
                next_same_direction_timestamp=matching, bars_to_next_same_direction=same_distance,
            )


def summarize_internal(events, relationships):
    counts = Counter(event["kind"] for event in events)
    return {
        "internal_bos": counts["bos"], "internal_choch": counts["choch"],
        "by_horizon": [
            {
                "classification": kind, "horizon": horizon,
                "external_follows": sum(row["external_choch_follows"] is True for row in window),
                "same_direction_follows": sum(row["same_direction_follows"] is True for row in window),
                "no_external_follows": sum(row["external_choch_follows"] is False for row in window),
                "unknown_due_to_truncation": sum(row["external_choch_follows"] is None for row in window),
            }
            for (kind, horizon), window in sorted(_relationship_groups(relationships).items())
        ],
    }


def _relationship_groups(relationships):
    groups = defaultdict(list)
    for row in relationships:
        groups[row["classification"], row["horizon"]].append(row)
    return groups


def summarize(rows, events, structures):
    groups = defaultdict(list)
    for row in rows:
        quality = "strong" if row["strong_choch"] == "True" else "rejected"
        for symbol, direction in (("all", "all"), (row["symbol"], row["direction"])):
            for label in ("all", quality):
                groups[symbol, direction, label, row["horizon"]].append(row)
    horizons = []
    for (symbol, direction, quality, horizon), group in sorted(groups.items()):
        complete = [row for row in group if row["complete"]]
        horizons.append({
            "symbol": symbol, "direction": direction, "quality": quality, "horizon": horizon,
            "complete": len(complete), "incomplete": len(group) - len(complete),
            **{f"mean_{key}": mean(row[key] for row in complete) if complete else None
               for key in METRICS},
        })

    counts = Counter((event["symbol"], event["direction"], event["strong_choch"])
                     for event in events)
    rejections = Counter(reason for event in events
                         for reason in event["rejection_reasons"].split("|") if reason)
    strong = sum(event["strong_choch"] == "True" for event in events)
    return {
        "bos": sum(event["kind"] == "bos" and event["in_evaluation"] == "True"
                   for event in structures),
        "choch_candidates": len(events), "strong_choch": strong, "rejected": len(events) - strong,
        "rejection_reasons": dict(rejections),
        "breakdown": [{"symbol": symbol, "direction": direction, "strong": quality == "True",
                       "count": count} for (symbol, direction, quality), count in sorted(counts.items())],
        "forward_horizons": list(HORIZONS), "forward_statistics": horizons,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Replay output directory")
    directory = parser.parse_args().input
    internal_path = directory / "internal_events.csv"
    names = ["forward_evaluation.csv", "summary.json"]
    if internal_path.exists():
        names.append("internal_external_relationships.csv")
    for name in names:
        if (directory / name).exists():
            parser.error(f"Output already exists: {directory / name}")
    metadata = json.loads((directory / "run_metadata.json").read_text(encoding="utf-8"))
    bars, _ = load_candles(directory / "candles_4h.csv", metadata["symbol"], "4h",
                           utc(metadata["evaluation_end_exclusive"]))
    events = read_rows(directory / "choch_candidates.csv")
    rows = list(forward_rows(bars, events))
    summary = summarize(rows, events, read_rows(directory / "structure_events.csv"))
    summary["symbol"] = metadata["symbol"]
    if internal_path.exists():
        internal_events = [row for row in read_rows(internal_path)
                           if row["in_evaluation"] == "True" and row["kind"] in ("bos", "choch")]
        relationships = list(internal_relationships(bars, internal_events, events))
        summary["internal_structure"] = summarize_internal(internal_events, relationships)
        write_rows(directory / "internal_external_relationships.csv", RELATION_COLUMNS, relationships)
    discovery_path = directory / "discovered_breaks.csv"
    if discovery_path.exists():
        summary["discovered_breaks"] = dict(Counter(
            row["classification"] for row in read_rows(discovery_path)
            if row["in_evaluation"] == "True"
        ))
    write_rows(directory / "forward_evaluation.csv", COLUMNS, rows)
    write_json(directory / "summary.json", summary)
    print(f"{metadata['symbol']}: {summary['bos']} BOS, {len(events)} candidates, "
          f"{summary['strong_choch']} strong; {directory / 'summary.json'}")


if __name__ == "__main__":
    main()
