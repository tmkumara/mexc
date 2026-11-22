"""Observe opposite trend establishments without changing external structure."""

import argparse
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path
import json

from strategy.market_structure.models import MarketTrend, StructureState
from strategy.market_structure.structure import Parameters

from .data import utc
from .evaluate import read_rows
from .replay import StructureReplay, write_json, write_rows
from .structure_audit import SavedRun
from .plot import render
from .validation import SYMBOLS, fingerprint


UNPROTECTED = {
    StructureState.BULLISH_UNPROTECTED: (MarketTrend.BULLISH, MarketTrend.BEARISH),
    StructureState.BEARISH_UNPROTECTED: (MarketTrend.BEARISH, MarketTrend.BULLISH),
}
REASON = (
    "No protection and no qualifying continuation break; four confirmed significant pivots "
    "from the current trend epoch formed the opposite setup, and the completed candle crossed "
    "its eligible directional swing."
)


def confirmed_transition(previous, event, closed_at):
    pair = UNPROTECTED.get(previous.state) if previous else None
    return bool(pair and event and event.scope == "external" and event.kind == "trend_established"
                and event.break_timestamp == closed_at
                and (event.previous_trend, event.new_structure_state) == pair
                and event.direction == pair[1])


class DiagnosticReplay:
    def __init__(self, parameters=Parameters(), groups=None):
        self.layers = StructureReplay(parameters)
        self.groups = groups or {}
        self.transitions = []

    def update(self, candle):
        external = self.layers.external
        before = external.snapshots[-1] if external.snapshots else None
        setup = external.active_swings[-4:]
        self.layers.update(candle)
        event = external.structure_events[-1] if external.structure_events else None
        stamp = candle.closed_at
        if not confirmed_transition(before, event, stamp):
            return None
        direction = "up" if event.direction == MarketTrend.BULLISH else "down"
        group = self.groups.get((stamp, direction), {})
        displacement = abs(event.break_close - event.broken_level)
        row = dict(
            classification="UNPROTECTED_TREND_TRANSITION", symbol=candle.symbol, event_timestamp=stamp,
            previous_trend=event.previous_trend.value, new_trend=event.direction.value,
            previous_protection_state=before.state.value, previous_protected_level=None,
            resulting_state=external.state.value, new_protected_level=event.protected_level,
            broken_swing_level=event.broken_level, broken_swing_pivot=event.pivot_timestamp,
            broken_swing_confirmation=event.pivot_confirmation_timestamp, break_close=event.break_close,
            atr=event.atr, displacement=displacement,
            displacement_atr=displacement / event.atr if event.atr else None,
            grouped_event_id=group.get("event_id"), grouped_swing_count=group.get("swing_level_count"),
            grouped_broken_level_ids=group.get("broken_level_ids"),
            grouped_max_displacement_atr=group.get("max_displacement_atr"),
            grouped_range_break=group.get("range_break"),
            setup_pivots="|".join(f"{swing.kind}:{swing.price:g}@{swing.pivot_timestamp.isoformat()}"
                                  for swing in setup),
            reason=REASON,
        )
        self.transitions.append(row)
        return row


def replay_diagnostics(candles, parameters=Parameters(), groups=None):
    observer = DiagnosticReplay(parameters, groups)
    for candle in candles:
        observer.update(candle)
    return observer


def comparable(value):
    if hasattr(value, "value"):
        return str(value.value)
    if isinstance(value, tuple):
        return "|".join(comparable(item) for item in value)
    return str(value) if value is not None else ""


def verify_replay(observer, saved):
    """Reject divergence before writing diagnostics for the saved development run."""
    layers = observer.layers
    for actual, expected in ((layers.external.structure_events, saved.breaks),
                             (layers.internal.structure_events, saved.internal_breaks),
                             ([row for row in layers.external.choch_events
                               if row.event_timestamp in {key[0] for key in saved.choch_events}],
                              saved.choch)):
        if len(actual) != len(expected) or any(
            any(comparable(getattr(event, field.name)) != row[field.name] for field in fields(event))
            for event, row in zip(actual, expected)
        ):
            raise ValueError(f"{saved.symbol}: replay diverges from saved structural events")
    for external, internal in zip(layers.external.snapshots, layers.internal.snapshots, strict=True):
        row = saved.external_states[external.timestamp]
        if (any(comparable(getattr(external, field.name)) != row[field.name] for field in fields(external))
                or internal.state.value != saved.internal_states[internal.timestamp]):
            raise ValueError(f"{saved.symbol}: replay diverges from saved states")


def summarize_symbol(saved, observer, start, high_displacement):
    counts = Counter()
    snapshots = observer.layers.external.snapshots
    transitions = {row["event_timestamp"]: row for row in observer.transitions
                   if row["event_timestamp"] > start}
    counts["diagnostic_transitions"] = len(transitions)
    counts["bullish_to_bearish"] = sum(row["new_trend"] == "bearish" for row in transitions.values())
    counts["bearish_to_bullish"] = len(transitions) - counts["bullish_to_bearish"]
    counts["external_choch"] = len(saved.choch)
    counts["strong_external_choch"] = sum(row["strong_choch"] == "True" for row in saved.choch)
    counts["same_close_external_choch"] = sum(key[0] in transitions for key in saved.choch_events)
    counts["transition_then_external_choch_within_24"] = sum(any(
        0 < saved.indices[close] - saved.indices[stamp] <= 24 for close, _ in saved.choch_events
    ) for stamp in transitions)
    audit_groups = []
    earlier = None
    for before, after in zip(snapshots, snapshots[1:]):
        stamp = after.timestamp
        if stamp <= start:
            continue
        pair = UNPROTECTED.get(before.state)
        if pair:
            trend, opposite = pair
            counts[f"{trend.value}_unprotected_candles"] += 1
            if before.state != earlier:
                counts[f"{trend.value}_unprotected_episodes"] += 1
            for direction in ("up", "down"):
                group = saved.groups.get((stamp, direction))
                if group is None:
                    continue
                opposite_break = direction == ("up" if opposite == MarketTrend.BULLISH else "down")
                displacement = group["max_displacement_atr"]
                significant = (group["range_break"] == "True" or int(group["external_level_count"]) > 0
                               or bool(displacement and float(displacement) > high_displacement))
                transition = transitions.get(stamp, {}).get("grouped_event_id") == group["event_id"]
                counts["grouped_breaks_during_unprotected"] += 1
                counts["opposite_grouped_breaks"] += opposite_break
                counts["significant_opposite_without_transition"] += opposite_break and significant and not transition
                audit_groups.append(dict(group, significant=significant, opposite_direction=opposite_break,
                                         diagnostic_transition=transition))
        earlier = before.state
    return counts, audit_groups


COUNT_COLUMNS = (
    "bullish_unprotected_candles", "bearish_unprotected_candles",
    "bullish_unprotected_episodes", "bearish_unprotected_episodes",
    "diagnostic_transitions", "bullish_to_bearish", "bearish_to_bullish",
    "grouped_breaks_during_unprotected", "opposite_grouped_breaks",
    "significant_opposite_without_transition", "external_choch", "strong_external_choch",
    "same_close_external_choch", "transition_then_external_choch_within_24",
)
TRANSITION_COLUMNS = (
    "classification", "symbol", "event_timestamp", "previous_trend", "new_trend",
    "previous_protection_state", "previous_protected_level", "resulting_state", "new_protected_level",
    "broken_swing_level", "broken_swing_pivot", "broken_swing_confirmation", "break_close",
    "atr", "displacement", "displacement_atr", "grouped_event_id", "grouped_swing_count",
    "grouped_broken_level_ids", "grouped_max_displacement_atr", "grouped_range_break",
    "setup_pivots", "reason",
)


def chart_for(path, saved, stamp, direction, transitions, high_displacement):
    index = saved.indices[stamp]
    bars = saved.bars[max(0, index - 96):index + 17]
    dates = {bar.closed_at for bar in bars}
    row = saved.evidence(stamp, direction)
    major = {event[field] for event in saved.breaks
             for field in ("pivot_timestamp", "protected_pivot_timestamp")}
    major.update(row[f"previous_external_{kind}_pivot"] for kind in ("high", "low"))
    groups = [group for (close, _), group in saved.groups.items() if close in dates
              and (close == stamp or group["range_break"] == "True"
                   or (group["max_displacement_atr"] and float(group["max_displacement_atr"]) > high_displacement))]
    visible = [dict(event, event_timestamp=str(event["event_timestamp"]))
               for event in transitions if event["event_timestamp"] in dates]
    render(path, bars, [swing for swing in saved.swings if swing["pivot_timestamp"] in major],
           saved.breaks, saved.choch, f"{saved.symbol} | {stamp:%Y-%m-%d %H:%M} UTC",
           grouped_events=groups, transitions=visible, audit_focus=dict(
               row, event_timestamp=stamp.isoformat(), external_state=row["external_state_at_close"],
               internal_state=row["internal_state_at_close"]))


def generate(source, freeze, output, chart_cases=()):
    lock = json.loads(freeze.read_text())
    if lock["fingerprint"] != fingerprint():
        raise ValueError("Phase 2.5 fingerprint mismatch")
    parameters = Parameters(**lock["parameters"])
    start = utc(lock["evaluation_start_inclusive"])
    high_displacement = lock["bands"]["max_displacement_atr"][1]
    groups = read_rows(source / "grouped_events.csv")
    runs, transitions, comparison, audit, totals = {}, [], [], [], Counter()
    for symbol in SYMBOLS:
        run = SavedRun(source / symbol, symbol, [row for row in groups if row["symbol"] == symbol])
        observer = replay_diagnostics(run.bars, parameters, run.groups)
        verify_replay(observer, run)
        counts, rows = summarize_symbol(run, observer, start, high_displacement)
        runs[symbol] = run
        totals.update(counts)
        transitions.extend(row for row in observer.transitions if row["event_timestamp"] > start)
        comparison.append(dict(symbol=symbol, **{key: counts[key] for key in COUNT_COLUMNS}))
        audit.extend(rows)
    comparison.append(dict(symbol="TOTAL", **{key: totals[key] for key in COUNT_COLUMNS}))
    output.mkdir(parents=True, exist_ok=False)
    write_rows(output / "diagnostic_transitions.csv", TRANSITION_COLUMNS, transitions)
    write_rows(output / "historical_comparison.csv", ("symbol", *COUNT_COLUMNS), comparison)
    write_rows(output / "unprotected_grouped_breaks.csv", (*groups[0], "significant",
               "opposite_direction", "diagnostic_transition"), audit)
    write_json(output / "run_metadata.json", dict(fingerprint=lock["fingerprint"]["sha256"],
               evaluation_start=lock["evaluation_start_inclusive"],
               evaluation_end=lock["evaluation_end_exclusive"], replay_parity=True,
               significance_definition="Range break, external-level association, or displacement above frozen development upper tertile",
               high_displacement_cut=high_displacement))
    charts = output / "charts"
    charts.mkdir()
    focuses = {(row["symbol"], row["event_timestamp"],
                "up" if row["new_trend"] == "bullish" else "down") for row in transitions}
    focuses.update(chart_cases)
    for symbol, stamp, direction in sorted(focuses):
        chart_for(charts / f"{symbol}_{stamp:%Y%m%dT%H%MZ}_{direction}.svg",
                  runs[symbol], stamp, direction, [row for row in transitions if row["symbol"] == symbol],
                  high_displacement)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chart", action="append", default=[], metavar="SYMBOL=UTC_TIMESTAMP=DIRECTION")
    args = parser.parse_args()
    cases = [(symbol, utc(stamp), direction)
             for symbol, stamp, direction in (value.split("=") for value in args.chart)]
    generate(args.source, args.freeze, args.output, cases)


if __name__ == "__main__":
    main()
