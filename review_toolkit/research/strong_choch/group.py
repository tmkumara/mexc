"""Group confirmed discoveries and evaluate the groups after replay."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import mean

from strategy.market_structure.models import FOUR_HOURS

from .data import load_candles, utc
from .evaluate import COLUMNS, HORIZONS, METRICS, forward_rows, internal_relationships, read_rows
from .replay import write_json, write_rows


METRIC_COLUMNS = (
    "max_swing_atr", "max_displacement_atr", "oldest_level_age_bars",
    "prior_level_tests", "swing_level_count", "external_level_count", "range_break",
)
GROUP_COLUMNS = (
    "event_id", "symbol", "timeframe", "event_timestamp", "direction",
    "open", "high", "low", "break_close", "swing_level_count", "broken_level_ids",
    "oldest_level_age_bars", "newest_level_age_bars", "max_swing_atr", "max_displacement_atr",
    "prior_level_tests", "range_break", "external_level_count",
    "prior_external_state", "prior_internal_state", "external_trend", "internal_trend",
    "external_state", "internal_state", "external_protected",
    "external_choch_same_candle", "internal_choch_same_candle", "strong_external_choch_same_candle",
)
FORWARD_COLUMNS = (
    *[column for column in COLUMNS if column != "strong_choch"], "range_break",
    "external_choch_follows", "same_direction_external_choch_follows",
    "next_external_choch_timestamp", "bars_to_next_external_choch",
)


def level_id(symbol, row):
    return f"{symbol}:4h:{row['direction']}:{utc(row['pivot_timestamp']).isoformat()}"


def group_candle(candle, discoveries, activated_external, external_state, internal_state,
                 external_choch=None, internal_choch=None):
    stamp = candle.closed_at
    by_direction = defaultdict(list)
    for row in discoveries:
        if row["symbol"] != candle.symbol or utc(row["break_timestamp"]) != stamp:
            raise ValueError("Discovery row does not belong to the supplied candle")
        by_direction[row["direction"]].append(row)

    for direction, rows in sorted(by_direction.items()):
        event_id = f"{candle.symbol}:4h:{stamp.isoformat()}:{direction}"
        swings = {}
        ranges = []
        for row in sorted(rows, key=lambda item: (item["pivot_timestamp"], item["classification"])):
            if row["classification"].startswith("RANGE_"):
                ranges.append(row)
            else:
                swings[level_id(candle.symbol, row)] = row
        levels = dict(swings)
        for row in ranges:
            levels.setdefault(level_id(candle.symbol, row), row)
        external_ids = []
        for key, row in levels.items():
            activated = activated_external.get(utc(row["pivot_timestamp"]))
            if activated is not None and activated <= stamp:
                external_ids.append(key)
        # For range-only evidence use swing confirmation for level age. The
        # discovery row's age is measured from box activation in that case.
        ages = [(stamp - utc(row["confirmation_timestamp"])) // FOUR_HOURS
                for row in levels.values()]
        maxima = {key: max((float(row[key]) for row in levels.values() if row[key] != ""),
                           default=None) for key in ("swing_move_atr", "break_distance_atr")}
        trend = "bullish" if direction == "up" else "bearish"
        external_match = external_choch is not None and external_choch["direction"] == trend
        internal_match = internal_choch is not None and internal_choch["direction"] == trend
        group = dict(
            event_id=event_id, symbol=candle.symbol, timeframe="4h", event_timestamp=stamp,
            direction=direction, open=candle.open, high=candle.high, low=candle.low,
            break_close=candle.close, swing_level_count=len(swings), broken_level_ids="|".join(levels),
            oldest_level_age_bars=int(max(ages)), newest_level_age_bars=int(min(ages)),
            max_swing_atr=maxima["swing_move_atr"], max_displacement_atr=maxima["break_distance_atr"],
            prior_level_tests=sum(int(row["prior_touches"]) for row in levels.values()),
            range_break=bool(ranges), external_level_count=len(external_ids),
            prior_external_state=rows[0]["prior_external_state"],
            prior_internal_state=rows[0]["prior_internal_state"],
            external_trend=external_state.split("_", 1)[0],
            internal_trend=internal_state.split("_", 1)[0],
            external_state=external_state, internal_state=internal_state,
            external_protected=external_state.endswith("_protected"),
            external_choch_same_candle=external_match, internal_choch_same_candle=internal_match,
            strong_external_choch_same_candle=external_match and external_choch["strong_choch"] == "True",
        )
        yield group, dict(event_id=event_id, swing_breaks=list(swings.values()), range_breaks=ranges,
                          external_level_ids=external_ids, external_choch_at_close=external_choch,
                          internal_choch_at_close=internal_choch)


def group_replay(candles, discoveries, activated_external, external_states, internal_states,
                 external_events, internal_events):
    by_close = defaultdict(list)
    for row in discoveries:
        by_close[utc(row["break_timestamp"])].append(row)
    for candle in candles:
        stamp = candle.closed_at
        if stamp in by_close:
            yield from group_candle(candle, by_close[stamp], activated_external,
                                    external_states[stamp], internal_states[stamp],
                                    external_events.get(stamp), internal_events.get(stamp))


def metric_ranks(groups):
    """Descriptive whole-cohort ranks, kept out of causal grouped evidence."""
    ranks = {row["event_id"]: {} for row in groups}
    for metric in METRIC_COLUMNS:
        values = sorted((row[metric] for row in groups if row[metric] is not None), reverse=True)
        first = {}
        for index, value in enumerate(values, 1):
            first.setdefault(value, index)
        for row in groups:
            ranks[row["event_id"]][f"{metric}_rank"] = first.get(row[metric])
    return ranks


def evaluate_groups(candles, groups, external):
    by_id = {group["event_id"]: group for group in groups}
    candidates = [dict(group, direction="bullish" if group["direction"] == "up" else "bearish",
                       event_timestamp=str(group["event_timestamp"]), strong_choch=False)
                  for group in groups]
    relationships = (dict(row, break_timestamp=str(row["event_timestamp"]),
                          classification="GROUPED_BREAK") for row in candidates)
    for forward, match in zip(forward_rows(candles, candidates),
                              internal_relationships(candles, relationships, external), strict=True):
        group = by_id[forward["event_id"]]
        forward.pop("strong_choch")
        yield dict(forward, direction=group["direction"], range_break=group["range_break"],
                   external_choch_follows=match["external_choch_follows"],
                   same_direction_external_choch_follows=match["same_direction_follows"],
                   next_external_choch_timestamp=match["next_external_choch_timestamp"],
                   bars_to_next_external_choch=match["bars_to_next_external_choch"])


def summarize(groups, forward, raw):
    summary = {
        "raw_swing_breaks": sum(row["classification"].startswith("SWING_") for row in raw),
        "raw_range_breaks": sum(row["classification"].startswith("RANGE_") for row in raw),
        "grouped_events": len(groups), "range_groups": sum(row["range_break"] for row in groups),
        "range_only_groups": sum(row["range_break"] and not row["swing_level_count"] for row in groups),
        "multiple_swing_groups": sum(row["swing_level_count"] > 1 for row in groups),
        "same_candle_external_choch": sum(row["external_choch_same_candle"] for row in groups),
        "groups_with_later_external_choch_in_sample": sum(
            row["next_external_choch_timestamp"] is not None
            for row in forward if row["horizon"] == HORIZONS[0]
        ),
        "forward": [],
    }
    summary["groups_without_later_external_choch_in_sample"] = (
        len(groups) - summary["groups_with_later_external_choch_in_sample"]
    )
    for horizon in HORIZONS:
        window = [row for row in forward if row["horizon"] == horizon]
        complete = [row for row in window if row["complete"]]
        summary["forward"].append(dict(
            horizon=horizon, complete=len(complete), incomplete=len(window) - len(complete),
            followed=sum(row["external_choch_follows"] is True for row in window),
            followed_same_direction=sum(row["same_direction_external_choch_follows"] is True
                                        for row in window),
            not_followed=sum(row["external_choch_follows"] is False for row in window),
            unknown=sum(row["external_choch_follows"] is None for row in window),
            **{f"mean_{metric}": mean(row[metric] for row in complete) if complete else None
               for metric in METRICS},
        ))
    return summary


def analyze_run(directory, symbol):
    metadata = json.loads((directory / "run_metadata.json").read_text(encoding="utf-8"))
    candles, _ = load_candles(directory / "candles_4h.csv", symbol, "4h",
                              utc(metadata["evaluation_end_exclusive"]))
    raw = [row for row in read_rows(directory / "discovered_breaks.csv")
           if row["in_evaluation"] == "True"]
    activated = {utc(row["pivot_timestamp"]): utc(row["external_since"])
                 for row in read_rows(directory / "confirmed_swings.csv") if row["external_since"]}
    external_states = {utc(row["timestamp"]): row["state"]
                       for row in read_rows(directory / "structure_states.csv")}
    internal_states = {utc(row["timestamp"]): row["state"]
                       for row in read_rows(directory / "internal_states.csv")}
    external = read_rows(directory / "choch_candidates.csv")
    internal = {utc(row["break_timestamp"]): row for row in read_rows(directory / "internal_events.csv")
                if row["kind"] == "choch"}
    grouped = list(group_replay(candles, raw, activated, external_states, internal_states,
                                {utc(row["event_timestamp"]): row for row in external}, internal))
    groups = [row for row, _ in grouped]
    return grouped, list(evaluate_groups(candles, groups, external)), raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Root containing freshly replayed symbol directories")
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--audit", action="append", default=[], metavar="SYMBOL=UTC_TIMESTAMP")
    args = parser.parse_args()
    ranks_columns = tuple(f"{metric}_rank" for metric in METRIC_COLUMNS)
    outputs = ("grouped_events.csv", "grouped_event_details.json", "grouped_metric_ranks.csv",
               "forward_evaluation.csv", "summary.json", "audit_comparison.csv")
    if any((args.input / name).exists() for name in outputs):
        parser.error("Grouped output already exists")
    by_symbol = {}
    paired, forward, raw = [], [], []
    for symbol in sorted(set(args.symbol)):
        pairs, evaluations, discoveries = analyze_run(args.input / symbol, symbol)
        by_symbol[symbol] = summarize([row for row, _ in pairs], evaluations, discoveries)
        paired.extend(pairs)
        forward.extend(evaluations)
        raw.extend(discoveries)
    groups = [row for row, _ in paired]
    ranks = metric_ranks(groups)
    audit = []
    first_window = {row["event_id"]: row for row in forward if row["horizon"] == HORIZONS[0]}
    for case in args.audit:
        symbol, timestamp = case.split("=", 1)
        stamp = utc(timestamp)
        matched = [group for group in groups if group["symbol"] == symbol and group["event_timestamp"] == stamp]
        for group in matched:
            evidence = first_window[group["event_id"]]
            audit.append(dict(group, **ranks[group["event_id"]], cohort_size=len(groups),
                              next_external_choch_timestamp=evidence["next_external_choch_timestamp"],
                              bars_to_next_external_choch=evidence["bars_to_next_external_choch"]))
        if not matched:
            audit.append(dict(symbol=symbol, event_timestamp=stamp, cohort_size=len(groups)))
    write_rows(args.input / outputs[0], GROUP_COLUMNS, groups)
    write_json(args.input / outputs[1], [details for _, details in paired])
    write_rows(args.input / outputs[2], ("event_id", *ranks_columns),
               (dict(event_id=event_id, **rank) for event_id, rank in ranks.items()))
    write_rows(args.input / outputs[3], FORWARD_COLUMNS, forward)
    write_json(args.input / outputs[4], dict(summarize(groups, forward, raw), by_symbol=by_symbol,
                                            ranks="descending whole-cohort competition ranks per metric"))
    write_rows(args.input / outputs[5], (*GROUP_COLUMNS, *ranks_columns, "cohort_size",
               "next_external_choch_timestamp", "bars_to_next_external_choch"), audit)
    print(f"{len(groups)} groups from {len(raw)} discovery rows; {args.input}")


if __name__ == "__main__":
    main()
