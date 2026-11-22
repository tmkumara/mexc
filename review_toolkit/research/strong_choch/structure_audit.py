"""Build a read-only structural audit from the saved research replay."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

from strategy.market_structure.models import FOUR_HOURS

from .accuracy_evaluation import evaluate
from .data import load_candles, utc
from .evaluate import read_rows
from .plot import render
from .replay import write_json, write_rows
from .validation import SYMBOLS, fingerprint


# Requested audit anchors and qualitative observations. They never feed replay or review labels.
KNOWN = (
    ("ETH_USDT", "2026-08-19 16:00", "up",
     "Both layers established a new bullish unprotected bias after the close broke 1922.71. "
     "The prior bearish states had no protected high to break.",
     "Major bullish transition captured as trend establishment. Review whether an independently "
     "confirmed opposite setup should count as a transition subtype for unprotected trends."),
    ("ETH_USDT", "2026-10-07 04:00", "down",
     "Both layers were bullish protected at 2562.96. The close broke newer lows but remained above "
     "that outer protection. Both layers broke 2562.96 at 2026-10-08 12:00 UTC.",
     "Meaningful earlier breakdown of newer structure. Review its structural scale separately "
     "from the valid later external CHoCH."),
    ("SOL_USDT", "2026-10-07 16:00", "down",
     "The external protected low at 112.45 held. Internal structure was unknown after the "
     "2026-09-28 08:00 internal CHoCH and had no protected level.",
     "Local swing break and early warning. The close beyond the broken levels was shallow; "
     "review as an ordinary break unless the levels warrant a higher structural role."),
    ("SOL_USDT", "2026-10-07 20:00", "down",
     "The close broke the 116.63 range floor and nearby swings. External protection at 112.45 held "
     "until 2026-10-08 16:00 UTC, and internal structure remained unknown.",
     "Confirmed local consolidation breakdown. Review whether a separate range-transition "
     "label would be useful. The established outer trend remained intact at this close."),
    ("BNB_USDT", "2026-10-30 00:00", "down",
     "The external bullish state had no assigned protected low. The 734.6 pullback predated "
     "the breakout target pivot at 756.2, so it was ineligible. Internal state was unknown.",
     "Strongest unrecorded opposing transition candidate here. Review whether the 734.6 leg "
     "origin should have been eligible and whether the recent bullish bias was durable."),
)
SAMPLE_SEED = "phase-2.6-v1"
SWING_COLUMNS = tuple(f"previous_external_{side}_{field}" for side in ("high", "low")
                      for field in ("price", "pivot", "confirmation", "activation"))
METRIC_COLUMNS = ("swing_level_count", "broken_level_ids", "max_swing_atr", "max_displacement_atr",
                  "oldest_level_age_bars", "newest_level_age_bars", "prior_level_tests",
                  "external_level_count", "range_break")
AUDIT_COLUMNS = (
    "case_id", "symbol", "event_timestamp", "direction", "open", "high", "low", "close",
    "external_trend", "prior_external_state", "external_state_at_close", "active_external_high",
    "active_external_low", "protected_status", "protected_level", *SWING_COLUMNS,
    "last_external_bos_timestamp", "last_external_bos_level", "last_external_bos_current_trend",
    "prior_internal_state", "internal_state_at_close", "broken_levels", "detected_event_type",
    "algorithm_classification", "strong_rejection_reasons", *METRIC_COLUMNS, "chart_reference",
)


def event_key(row, time_field):
    return utc(row[time_field]), {"bullish": "up", "bearish": "down"}.get(row["direction"], row["direction"])


class SavedRun:
    def __init__(self, directory, symbol, grouped):
        metadata = json.loads((directory / "run_metadata.json").read_text())
        self.symbol = symbol
        self.bars, _ = load_candles(directory / "candles_4h.csv", symbol, "4h",
                                   utc(metadata["evaluation_end_exclusive"]))
        self.indices = {bar.closed_at: i for i, bar in enumerate(self.bars)}
        self.swings = read_rows(directory / "confirmed_swings.csv")
        self.breaks = read_rows(directory / "structure_events.csv")
        self.internal_breaks = read_rows(directory / "internal_events.csv")
        self.internal_swings = read_rows(directory / "internal_swings.csv")
        self.choch = read_rows(directory / "choch_candidates.csv")
        self.external_events = {event_key(row, "break_timestamp"): row for row in self.breaks}
        self.internal_events = {event_key(row, "break_timestamp"): row for row in self.internal_breaks}
        self.choch_events = {event_key(row, "event_timestamp"): row for row in self.choch}
        self.external_states = {utc(row["timestamp"]): row
                                for row in read_rows(directory / "structure_states.csv")}
        self.internal_states = {utc(row["timestamp"]): row["state"]
                                for row in read_rows(directory / "internal_states.csv")}
        self.groups = {event_key(row, "event_timestamp"): row for row in grouped}
        self.discoveries = {}
        for row in read_rows(directory / "discovered_breaks.csv"):
            self.discoveries.setdefault(event_key(row, "break_timestamp"), []).append(row)

    def evidence(self, stamp, direction):
        key = stamp, direction
        previous = stamp - FOUR_HOURS
        external = self.external_states[previous]
        state = external["state"]
        trend = state.split("_")[0]
        prior = [row for row in self.breaks if utc(row["break_timestamp"]) < stamp]
        last_bos = next((row for row in reversed(prior) if row["kind"] == "bos"), {})
        epoch = next((row for row in reversed(prior)
                      if row["kind"] in ("trend_established", "choch")), {})
        current_bos = bool(last_bos and epoch.get("kind") == "trend_established"
                           and utc(last_bos["break_timestamp"]) > utc(epoch["break_timestamp"]))
        side = "low" if trend == "bullish" else "high"
        protected = state.endswith("_protected")
        event = self.external_events.get(key, {})
        internal = self.internal_events.get(key, {})
        choch = self.choch_events.get(key, {})
        group = self.groups.get(key, {})
        classification = (event.get("classification") or internal.get("classification")
                          or ("GROUPED_BREAK" if group else "NONE"))
        if choch.get("strong_choch") == "True":
            classification = "STRONG_EXTERNAL_CHOCH"
        kinds = [row["classification"] for row in self.discoveries.get(key, ())]
        types = list(dict.fromkeys([*kinds, *[row["classification"] for row in (event, internal) if row]]))
        broken = sorted({float(row["broken_level"])
                         for row in [*self.discoveries.get(key, ()), *([event] if event else [])]})
        candle = self.bars[self.indices[stamp]]
        result = dict(
            case_id=f"{self.symbol}:4h:{stamp.isoformat()}:{direction}", symbol=self.symbol,
            event_timestamp=stamp, direction=direction, open=candle.open, high=candle.high,
            low=candle.low, close=candle.close, external_trend=trend, prior_external_state=state,
            external_state_at_close=self.external_states[stamp]["state"],
            active_external_high=external["external_high"], active_external_low=external["external_low"],
            protected_status="protected" if protected else "unknown" if trend == "unknown" else "unprotected",
            protected_level=external[f"external_{side}"] if protected else "",
            last_external_bos_timestamp=last_bos.get("break_timestamp", ""),
            last_external_bos_level=last_bos.get("broken_level", ""),
            last_external_bos_current_trend=current_bos,
            prior_internal_state=self.internal_states[previous], internal_state_at_close=self.internal_states[stamp],
            broken_levels="|".join(format(value, "g") for value in broken),
            detected_event_type="|".join(types), algorithm_classification=classification,
            strong_rejection_reasons=choch.get("rejection_reasons", ""),
            **{name: group.get(name, "") for name in METRIC_COLUMNS},
        )
        for kind in ("high", "low"):
            latest = next((row for row in reversed(self.swings) if row["kind"] == kind
                           and eligible_before(row, stamp)), {})
            for field, column in (("price", "price"), ("pivot", "pivot_timestamp"),
                                  ("confirmation", "confirmation_timestamp"), ("activation", "external_since")):
                result[f"previous_external_{kind}_{field}"] = latest.get(column, "")
        return result

    def chart(self, output, row):
        stamp = row["event_timestamp"]
        index = self.indices[stamp]
        bars = self.bars[max(0, index - 115):index + 19]
        major = {event[field] for event in self.breaks
                 for field in ("pivot_timestamp", "protected_pivot_timestamp")}
        major.update(row[f"previous_external_{kind}_pivot"] for kind in ("high", "low"))
        case_group = self.groups.get((stamp, row["direction"]))
        render(output, bars, [swing for swing in self.swings if swing["pivot_timestamp"] in major],
               self.breaks, self.choch, f"{self.symbol} | {stamp:%Y-%m-%d %H:%M} UTC structure audit",
               self.internal_swings, [event for event in self.internal_breaks if event["kind"] == "choch"],
               grouped_events=[case_group] if case_group else [],
               audit_focus=dict(row, event_timestamp=stamp.isoformat(), external_state=row["external_state_at_close"],
                                internal_state=row["internal_state_at_close"]))


def eligible_before(swing, stamp):
    return bool(swing["external_since"] and utc(swing["confirmation_timestamp"]) < stamp
                and utc(swing["external_since"]) < stamp)


def pick(symbol, keys, limit):
    return sorted(keys, key=lambda key: (sha256(
        f"{SAMPLE_SEED}|{symbol}|{key[0].isoformat()}|{key[1]}".encode()
    ).hexdigest(), key))[:limit]


def selection(run, known):
    result = {key: "known" for key in known}
    result.update({key: "external_choch" for key in run.choch_events if key not in result})
    bos = [key for key, row in run.external_events.items()
           if row["kind"] == "bos" and row["in_evaluation"] == "True" and key not in result]
    result.update({key: "sampled_external_bos" for key in pick(run.symbol, bos, 2)})
    ordinary = {key: row for key, row in run.groups.items()
                if key not in run.external_events and key not in run.internal_events and key not in result}
    for ranged, limit in (("True", 1), ("False", 2)):
        keys = [key for key, row in ordinary.items() if row["range_break"] == ranged]
        result.update({key: "sampled_range_break" if ranged == "True" else "sampled_swing_break"
                       for key in pick(run.symbol, keys, limit)})
    return result


def generate(source, freeze, output):
    frozen = json.loads(freeze.read_text())["fingerprint"]
    if frozen != fingerprint():
        raise ValueError("The Phase 2.5 frozen implementation fingerprint does not match")
    grouped = read_rows(source / "grouped_events.csv")
    runs = {symbol: SavedRun(source / symbol, symbol, [row for row in grouped if row["symbol"] == symbol])
            for symbol in SYMBOLS}
    known_rows, manual = [], []
    output.mkdir(parents=True, exist_ok=False)
    (output / "charts").mkdir()
    for symbol, when, direction, reason, assessment in KNOWN:
        stamp = utc(when)
        row = runs[symbol].evidence(stamp, direction)
        chart_name = f"charts/{symbol}_{stamp:%Y%m%dT%H%MZ}.svg"
        row["chart_reference"] = chart_name
        runs[symbol].chart(output / chart_name, row)
        next_choch = next((event for event in runs[symbol].choch
                           if utc(event["event_timestamp"]) > stamp), {})
        known_rows.append(dict(row, follows_implemented_rules=True, rule_explanation=reason,
                               structural_assessment=assessment, human_review_required=True,
                               next_external_choch=next_choch.get("event_timestamp", "")))
    known_index = {(row["symbol"], row["event_timestamp"], row["direction"]): row for row in known_rows}
    for symbol, run in runs.items():
        keys = [(stamp, direction) for coin, stamp, direction in known_index if coin == symbol]
        for (stamp, direction), category in sorted(selection(run, keys).items()):
            row = run.evidence(stamp, direction)
            known_row = known_index.get((symbol, stamp, direction))
            row["chart_reference"] = (known_row["chart_reference"] if known_row else
                                      f"../phase_2_4/{symbol}/charts/grouped_debug.svg")
            manual.append(dict(row, selection=category, manual_classification="UNREVIEWED", reviewer_notes=""))
    write_rows(output / "known_case_audit.csv", (*AUDIT_COLUMNS, "follows_implemented_rules",
               "rule_explanation", "structural_assessment", "human_review_required", "next_external_choch"),
               known_rows)
    write_rows(output / "manual_review.csv", (*AUDIT_COLUMNS, "selection", "manual_classification",
               "reviewer_notes"), manual)
    write_json(output / "review_status.json", dict(evaluate(manual),
                                                  selection_counts=dict(Counter(row["selection"] for row in manual)),
                                                  frozen_algorithm_sha256=frozen["sha256"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.source, args.freeze, args.output)


if __name__ == "__main__":
    main()
