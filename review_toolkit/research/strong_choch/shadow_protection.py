"""Isolated, stateful replay of immediate significant leg protection."""

import argparse
from collections import Counter
from dataclasses import fields
from hashlib import sha256
import json
from pathlib import Path

from strategy.market_structure.models import FOUR_HOURS, MarketTrend, ProtectedLevel
from strategy.market_structure.structure import Parameters, StructureDetector

from .data import utc
from .evaluate import read_rows
from .plot import render
from .replay import write_json, write_rows
from .structure_audit import SavedRun
from .unprotected import DiagnosticReplay, comparable, verify_replay
from .validation import SYMBOLS, fingerprint


AUDIT_CASES = (
    ("BNB_USDT", "2026-10-30 00:00", "down"),
    ("ETH_USDT", "2026-08-19 16:00", "up"),
    ("ETH_USDT", "2026-09-02 12:00", "down"),
    ("ETH_USDT", "2026-09-21 12:00", "up"),
    ("ETH_USDT", "2026-10-07 04:00", "down"),
    ("SOL_USDT", "2026-10-07 16:00", "down"),
    ("SOL_USDT", "2026-10-07 20:00", "down"),
)
DECISION_COLUMNS = (
    "symbol", "timestamp", "trend", "target_price", "target_pivot", "candidate_price",
    "candidate_pivot", "candidate_confirmation", "first_usable_close", "accepted", "reason",
)
STATE_FIELDS = ("state", "external_high", "external_low", "continuation_armed", "protected_level",
                "protected_pivot", "protected_since", "trend_started_at", "continuation_count")
EVENT_FIELDS = ("classification", "direction", "broken_level", "pivot_timestamp",
                "pivot_confirmation_timestamp", "resulting_state", "protected_level",
                "protected_pivot_timestamp", "associated_protected_level", "protected_since",
                "strong_rejection_reasons")
CONTEXT_FIELDS = (*[f"prior_{key}" for key in STATE_FIELDS], "open", "high", "low", "close")


def leg_candidates(setup, target, direction, candle, consumed, through, history):
    """Consider only significant pivots from the four-swing establishment setup."""
    side = "low" if direction == MarketTrend.BULLISH else "high"
    index = setup.index(target) if target in setup else 0
    immediate = setup[index - 1] if index else None
    for candidate in (swing for swing in setup if swing.kind == side):
        if candidate != immediate:
            reason = "not_immediate_significant_predecessor_of_target"
        elif candidate.confirmation_timestamp + FOUR_HOURS > candle.closed_at:
            reason = "not_usable_at_decision_close"
        elif candidate.pivot_timestamp >= target.pivot_timestamp:
            reason = "does_not_precede_target"
        elif candidate in consumed:
            reason = "already_structurally_consumed"
        elif candidate in through or any(
            candidate.confirmation_timestamp < bar.closed_at <= candle.closed_at
            and (bar.close < candidate.price if side == "low" else bar.close > candidate.price)
            for bar in history
        ):
            reason = "opposing_close_already_crossed_candidate"
        else:
            reason = "intact_immediate_significant_pre_target_leg_origin"
        yield candidate, reason, reason == "intact_immediate_significant_pre_target_leg_origin"


class ShadowDetector(StructureDetector):
    """An independent external detector with a fallback at establishment only."""

    def __init__(self, parameters=Parameters()):
        super().__init__(parameters)
        self.history = []
        self.protection_decisions = []

    def update(self, candle):
        event = super().update(candle)
        self.history.append(candle)
        return event

    def record_break(self, candle, kind, direction, swing, previous_trend, atr,
                     broken_protection=None):
        if kind == "trend_established":
            if self.protected:
                self.log_decision(candle, swing, direction, self.protected.swing, True,
                                  "frozen_post_target_protection_retained")
                for candidate in self.active_swings[-4:]:
                    if candidate.kind == self.protected.swing.kind and candidate != self.protected.swing:
                        self.log_decision(candle, swing, direction, candidate, False,
                                          "fallback_not_considered_frozen_protection_assigned")
            else:
                for candidate, reason, accepted in leg_candidates(
                    self.active_swings[-4:], swing, direction, candle,
                    self.broken_swings, self.closed_through, (*self.history, candle),
                ):
                    self.log_decision(candle, swing, direction, candidate, accepted, reason)
                    if accepted:
                        self.protected = ProtectedLevel(candidate, candle.closed_at)
                        self.structural_swings.setdefault(candidate, candle.closed_at)
        super().record_break(candle, kind, direction, swing, previous_trend, atr, broken_protection)

    def log_decision(self, candle, target, direction, candidate, accepted, reason):
        self.protection_decisions.append(dict(
            symbol=candle.symbol, timestamp=candle.closed_at, trend=direction.value,
            target_price=target.price, target_pivot=target.pivot_timestamp,
            candidate_price=candidate.price, candidate_pivot=candidate.pivot_timestamp,
            candidate_confirmation=candidate.confirmation_timestamp,
            first_usable_close=candidate.confirmation_timestamp + FOUR_HOURS,
            accepted=accepted, reason=reason,
        ))


def current_state(engine):
    snapshot = engine.snapshots[-1] if engine.snapshots else None
    protected = engine.protected
    return dict(
        state=engine.state.value,
        external_high=snapshot.external_high if snapshot else None,
        external_low=snapshot.external_low if snapshot else None,
        continuation_armed=snapshot.continuation_armed if snapshot else False,
        protected_level=protected.swing.price if protected else None,
        protected_pivot=protected.swing.pivot_timestamp if protected else None,
        protected_since=protected.established_at if protected else None,
        trend_started_at=engine.trend_started_at, continuation_count=engine.continuations,
    )


def event_at(engine, stamp):
    event = engine.structure_events[-1] if engine.structure_events else None
    if event is None or event.break_timestamp != stamp:
        return dict.fromkeys(EVENT_FIELDS)
    row = {key: getattr(event, key) for key in EVENT_FIELDS if key != "strong_rejection_reasons"}
    row["direction"] = event.direction.value
    row["strong_rejection_reasons"] = None
    if event.kind == "choch":
        choch = engine.choch_events[-1]
        row["classification"] = "STRONG_EXTERNAL_CHOCH" if choch.strong_choch else event.classification
        row["strong_rejection_reasons"] = comparable(choch.rejection_reasons)
    return row


def pair_fields(frozen, shadow):
    return {f"{engine}_{key}": value for engine, data in (("frozen", frozen), ("shadow", shadow))
            for key, value in data.items()}


class PairedReplay:
    def __init__(self, parameters=Parameters()):
        self.frozen = DiagnosticReplay(parameters)
        self.shadow = ShadowDetector(parameters)
        self.states = []
        self.events = []
        self.prior_states = {}

    def update(self, candle):
        frozen = self.frozen.layers.external
        before_frozen, before_shadow = current_state(frozen), current_state(self.shadow)
        self.frozen.update(candle)
        self.shadow.update(candle)
        stamp = candle.closed_at
        self.prior_states[stamp] = before_frozen, before_shadow
        after_frozen, after_shadow = current_state(frozen), current_state(self.shadow)
        frozen_event, shadow_event = event_at(frozen, stamp), event_at(self.shadow, stamp)
        candle_key = dict(symbol=candle.symbol, timestamp=stamp)
        if before_frozen != before_shadow or after_frozen != after_shadow:
            differing = [field for field in STATE_FIELDS if after_frozen[field] != after_shadow[field]]
            self.states.append(dict(
                **candle_key, differs_after_close=bool(differing), differing_fields="|".join(differing),
                **pair_fields(before_frozen, before_shadow), **{
                    f"{engine}_after_{key}": value
                    for engine, data in (("frozen", after_frozen), ("shadow", after_shadow))
                    for key, value in data.items()
                },
            ))
        if frozen_event["classification"] or shadow_event["classification"]:
            differing = [field for field in EVENT_FIELDS if frozen_event[field] != shadow_event[field]]
            self.events.append(dict(
                **candle_key, changed=bool(differing), differing_fields="|".join(differing),
                **pair_fields(frozen_event, shadow_event), **pair_fields(
                    {f"prior_{key}": value for key, value in before_frozen.items()},
                    {f"prior_{key}": value for key, value in before_shadow.items()}),
                open=candle.open, high=candle.high, low=candle.low, close=candle.close,
            ))


def replay_pairs(candles, parameters=Parameters()):
    pair = PairedReplay(parameters)
    for candle in candles:
        pair.update(candle)
    return pair


def is_choch(label):
    return label in ("EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH")


def summarize(events, divergence):
    def exact_choch_match(row):
        return (is_choch(row["frozen_classification"]) and is_choch(row["shadow_classification"])
                and all(row[f"frozen_{key}"] == row[f"shadow_{key}"]
                        for key in ("direction", "broken_level", "pivot_timestamp")))

    return dict(
        frozen=Counter(row["frozen_classification"] for row in events if row["frozen_classification"]),
        shadow=Counter(row["shadow_classification"] for row in events if row["shadow_classification"]),
        matched_unprotected_establishments_gaining_protection=sum(
            row["frozen_classification"] == row["shadow_classification"] == "EXTERNAL_TREND_ESTABLISHED"
            and row["frozen_direction"] == row["shadow_direction"]
            and row["frozen_pivot_timestamp"] == row["shadow_pivot_timestamp"]
            and row["frozen_protected_level"] is None and row["shadow_protected_level"] is not None
            for row in events
        ),
        new_choch_closes=sum(is_choch(row["shadow_classification"]) and not is_choch(row["frozen_classification"])
                             for row in events),
        lost_choch_closes=sum(is_choch(row["frozen_classification"]) and not is_choch(row["shadow_classification"])
                              for row in events),
        retained_exact_choch=sum(exact_choch_match(row) for row in events),
        changed_same_close_choch=sum(is_choch(row["frozen_classification"]) and is_choch(row["shadow_classification"])
                                     and not exact_choch_match(row) for row in events),
        changed_event_closes=sum(row["changed"] for row in events),
        changed_classification_closes=sum(row["frozen_classification"] != row["shadow_classification"]
                                          for row in events),
        divergent_state_candles=sum(row["differs_after_close"] for row in divergence),
        divergent_trend_candles=sum(row["frozen_after_state"].split("_")[0] !=
                                    row["shadow_after_state"].split("_")[0] for row in divergence),
    )


def model_row(item):
    return {field.name: comparable(getattr(item, field.name)) for field in fields(item)}


def charts(path, saved, engine, stamp, direction, label):
    index = saved.indices[stamp]
    bars = saved.bars[max(0, index - 115):index + 19]
    breaks = [model_row(item) for item in engine.structure_events]
    candidates = [model_row(item) for item in engine.choch_events]
    major = {item[key] for item in breaks for key in ("pivot_timestamp", "protected_pivot_timestamp")}
    swings = [dict(model_row(item), external="True", external_since=comparable(activation))
              for item, activation in sorted(engine.structural_swings.items(),
                                             key=lambda pair: pair[0].pivot_timestamp)
              if comparable(item.pivot_timestamp) in major]
    row = saved.evidence(stamp, direction)
    group = saved.groups.get((stamp, direction))
    render(path, bars, swings, breaks, candidates,
           f"{saved.symbol} | {stamp:%Y-%m-%d %H:%M} UTC | {label}",
           grouped_events=[group] if group else [], audit_focus=dict(
               event_timestamp=stamp.isoformat(), prior_external_state=engine.snapshots[index - 1].state.value,
               external_state=engine.snapshots[index].state.value,
               prior_internal_state=row["prior_internal_state"], internal_state=row["internal_state_at_close"]))


def generate(source, freeze, phase6_review, output):
    lock = json.loads(freeze.read_text())
    if lock["fingerprint"] != fingerprint():
        raise ValueError("Frozen detector fingerprint mismatch")
    start = utc(lock["evaluation_start_inclusive"])
    parameters = Parameters(**lock["parameters"])
    grouped = read_rows(source / "grouped_events.csv")
    review_source = read_rows(phase6_review)
    existing = {(row["symbol"], utc(row["event_timestamp"]), row["direction"]): row for row in review_source}
    runs, pairs, events, states, decisions, by_symbol = {}, {}, [], [], [], {}
    for symbol in SYMBOLS:
        saved = SavedRun(source / symbol, symbol, [row for row in grouped if row["symbol"] == symbol])
        pair = replay_pairs(saved.bars, parameters)
        verify_replay(pair.frozen, saved)
        runs[symbol], pairs[symbol] = saved, pair
        evaluated = [row for row in pair.events if row["timestamp"] > start]
        divergence = [row for row in pair.states if row["timestamp"] > start]
        events.extend(evaluated)
        states.extend(divergence)
        decisions.extend(row for row in pair.shadow.protection_decisions if row["timestamp"] > start)
        by_symbol[symbol] = summarize(evaluated, divergence)
    queue = []
    selected = {}
    events_by_close = {(row["symbol"], row["timestamp"]): row for row in events}
    for row in events:
        if row["changed"]:
            selected[row["symbol"], row["timestamp"]] = "changed"
    for symbol, timestamp, _ in AUDIT_CASES:
        selected.setdefault((symbol, utc(timestamp)), "named_audit")
    for symbol in SYMBOLS:
        stable = [(key, row) for key, row in existing.items()
                  if key[0] == symbol and key[:2] not in selected]
        stable.sort(key=lambda item: sha256(f"phase-2.9|{item[1]['case_id']}".encode()).hexdigest())
        for key, _ in stable[:2]:
            selected[key[:2]] = "sampled_unchanged_phase_2_6"
    for (symbol, stamp), category in sorted(selected.items()):
        event = events_by_close.get((symbol, stamp), {})
        audit_direction = next((side for coin, time, side in AUDIT_CASES
                                if coin == symbol and utc(time) == stamp), None)
        direction = event.get("shadow_direction") or event.get("frozen_direction")
        direction = {"bullish": "up", "bearish": "down"}.get(direction) or audit_direction
        if direction is None:
            direction = next(side for coin, time, side in existing if (coin, time) == (symbol, stamp))
        old = existing.get((symbol, stamp, direction), {})
        context = runs[symbol].evidence(stamp, direction)
        if not event:
            event = dict.fromkeys(key for key in events[0] if key.startswith(("frozen_", "shadow_"))
                                  or key == "differing_fields")
            before_frozen, before_shadow = pairs[symbol].prior_states[stamp]
            event.update(pair_fields({f"prior_{key}": value for key, value in before_frozen.items()},
                                     {f"prior_{key}": value for key, value in before_shadow.items()}))
        frozen_label, shadow_label = event["frozen_classification"], event["shadow_classification"]
        if is_choch(shadow_label) and not is_choch(frozen_label):
            review_focus = "new_shadow_choch_check_potential_false_positive"
        elif shadow_label == "STRONG_EXTERNAL_CHOCH" and frozen_label == "EXTERNAL_CHOCH":
            review_focus = "strength_upgrade_check_continuation_history"
        else:
            review_focus = ""
        queue.append(dict(
            symbol=symbol, timestamp=stamp, direction=direction, reason=category, review_focus=review_focus,
            phase_2_6_case_id=old.get("case_id", ""), phase_2_6_chart_reference=old.get("chart_reference", ""),
            **{key: value for key, value in event.items() if key.startswith(("frozen_", "shadow_"))
               or key == "differing_fields"},
            previous_external_high=context["previous_external_high_price"],
            previous_external_low=context["previous_external_low_price"],
            last_frozen_bos=context["last_external_bos_timestamp"],
            grouped_broken_levels=context["broken_levels"], close=context["close"],
            manual_classification="UNREVIEWED", reviewer_notes="",
        ))
    output.mkdir(parents=True, exist_ok=False)
    (output / "charts").mkdir()
    write_rows(output / "frozen_vs_shadow_events.csv", tuple(events[0]), events)
    write_rows(output / "state_divergence.csv", tuple(states[0]), states)
    write_rows(output / "protection_decisions.csv", DECISION_COLUMNS, decisions)
    write_rows(output / "manual_review_queue.csv", tuple(dict.fromkeys(key for row in queue for key in row)), queue)
    write_json(output / "run_metadata.json", dict(
        fingerprint=lock["fingerprint"]["sha256"], frozen_replay_parity=True,
        evaluation_start=lock["evaluation_start_inclusive"], evaluation_end=lock["evaluation_end_exclusive"],
        phase_2_6_review_sha256=sha256(phase6_review.read_bytes()).hexdigest(),
        review_rows=len(queue), summary=summarize(events, states), by_symbol=by_symbol,
    ))
    for symbol, when, direction in AUDIT_CASES:
        stamp = utc(when)
        pair = pairs[symbol]
        for label, engine in (("Frozen", pair.frozen.layers.external), ("Shadow", pair.shadow)):
            charts(output / "charts" / f"{symbol}_{stamp:%Y%m%dT%H%MZ}_{label.lower()}.svg",
                   runs[symbol], engine, stamp, direction, label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("source", "freeze", "review", "output"):
        parser.add_argument(f"--{key}", required=True, type=Path)
    args = parser.parse_args()
    generate(args.source, args.freeze, args.review, args.output)


if __name__ == "__main__":
    main()
