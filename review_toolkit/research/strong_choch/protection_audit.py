"""Compare establishment protection candidates without mutating the frozen replay."""

import argparse
from collections import Counter
from pathlib import Path
import json

from strategy.market_structure.models import FOUR_HOURS, MarketTrend
from strategy.market_structure.structure import Parameters

from .data import utc
from .evaluate import read_rows
from .plot import render
from .replay import write_rows, write_json
from .structure_audit import SavedRun
from .unprotected import DiagnosticReplay, verify_replay
from .validation import SYMBOLS, fingerprint


APPROACHES = ("frozen", "latest_intact", "leg_associated", "pre_target")


def crossed(price, close, bullish):
    return close < price if bullish else close > price


def inspect_candidates(event, confirmed, setup, consumed, tracked_through, candles):
    bullish = event.direction == MarketTrend.BULLISH
    side = "low" if bullish else "high"
    target = event.pivot_timestamp
    close = event.break_timestamp
    latest_active = next((item for item in reversed(setup) if item.kind == side), None)
    target_index = next(i for i, item in enumerate(setup) if item.pivot_timestamp == target)
    predecessor = setup[target_index - 1] if target_index else None
    rows = []
    for swing in confirmed:
        usable = swing.confirmation_timestamp + FOUR_HOURS
        if swing.kind != side or usable > close:
            continue
        timestamp = swing.pivot_timestamp
        used = swing in consumed
        first_through = next((bar.closed_at for bar in candles
                              if usable <= bar.closed_at <= close
                              and crossed(swing.price, bar.close, bullish)), None)
        intact = not used and first_through is None
        if swing == predecessor:
            relationship = "immediate_significant_pre_target_leg_origin"
        elif swing == latest_active and timestamp > target:
            relationship = "latest_significant_post_target_pullback"
        elif swing in setup:
            relationship = "earlier_significant_setup_swing"
        else:
            relationship = "outside_significant_establishment_setup"
        accepted = timestamp == event.protected_pivot_timestamp
        reasons = []
        if not accepted:
            if swing != latest_active:
                reasons.append("not_latest_active_significant_opposite")
            if timestamp <= target:
                reasons.append("origin_precedes_target")
            if used:
                reasons.append("already_structurally_broken")
            if swing in tracked_through or first_through == close:
                reasons.append("closed_through")
        rows.append(dict(
            symbol=event.symbol, establishment_timestamp=close, trend=event.direction.value,
            target_price=event.broken_level, target_pivot_timestamp=target,
            target_confirmation_timestamp=event.pivot_confirmation_timestamp,
            candidate_price=swing.price, candidate_pivot_timestamp=timestamp,
            candidate_confirmation_timestamp=swing.confirmation_timestamp, first_usable_close=usable,
            age_bars=(close - usable) // FOUR_HOURS, relationship=relationship,
            in_significant_setup=swing in setup, pre_target=timestamp < target,
            first_prior_close_through=first_through, structurally_consumed=used, intact=intact,
            frozen_accepted=accepted, frozen_reasons="|".join(reasons),
        ))
    return rows


def choices(rows):
    rows = tuple(rows)
    frozen = next((row for row in rows if row["frozen_accepted"]), None)
    intact = sorted((row for row in rows if row["intact"]),
                    key=lambda row: row["candidate_pivot_timestamp"], reverse=True)
    return {
        "frozen": frozen,
        "latest_intact": next(iter(intact), None),
        "leg_associated": frozen or next((row for row in intact if row["relationship"] ==
                                         "immediate_significant_pre_target_leg_origin"), None),
        "pre_target": frozen or next((row for row in intact if row["pre_target"]), None),
    }


def observe_establishments(saved, parameters):
    observer = DiagnosticReplay(parameters)
    establishments = []
    history = []
    for bar in saved.bars:
        engine = observer.layers.external
        setup = engine.active_swings[-4:]
        size = len(engine.structure_events)
        history.append(bar)
        observer.update(bar)
        if len(engine.structure_events) == size:
            continue
        event = engine.structure_events[-1]
        if event.kind == "trend_established":
            rows = inspect_candidates(event, engine.confirmed_swings, setup, engine.broken_swings,
                                      engine.closed_through, history)
            if bool(event.protected_level is not None) != any(row["frozen_accepted"] for row in rows):
                raise ValueError("Could not reconstruct the frozen protection candidate")
            establishments.append((event, rows))
    verify_replay(observer, saved)
    return establishments, observer.layers.external.structure_events


def horizon(event, later_events, end):
    for other in later_events:
        if other.break_timestamp <= event.break_timestamp:
            continue
        if other.kind in ("choch", "trend_established") or (
            other.kind == "bos" and other.protected_since == other.break_timestamp
        ):
            return other.break_timestamp, other.kind
    return end, "sample_end"


def shadow(event, candidate, bars, stop):
    """Hold a hypothetical level fixed through the first frozen lifecycle change."""
    if candidate is None:
        return None
    bullish = event.direction == MarketTrend.BULLISH
    price = candidate["candidate_price"]
    previous_close = event.break_close
    for bar in bars:
        if bar.closed_at <= event.break_timestamp:
            continue
        if bar.closed_at > stop:
            break
        if not crossed(price, previous_close, bullish) and crossed(price, bar.close, bullish):
            return bar
        previous_close = bar.close
    return None


def compare(event, rows, bars, events):
    selected = choices(rows)
    stop, stop_reason = horizon(event, events, bars[-1].closed_at)
    actual_choch = stop if stop_reason == "choch" else None
    output = []
    for approach, candidate in selected.items():
        first = shadow(event, candidate, bars, stop)
        stamp = first.closed_at if first else None
        elapsed = (stamp - event.break_timestamp) // FOUR_HOURS if stamp else None
        sensitivity = elapsed is not None and elapsed <= 3
        additional = stamp is not None and stamp != actual_choch
        if actual_choch and stamp != actual_choch:
            change = "earlier_shadow_break" if stamp else "frozen_choch_level_not_crossed"
        elif actual_choch and candidate["candidate_price"] != event.protected_level:
            change = "same_close_different_level"
        else:
            change = ""
        output.append(dict(
            symbol=event.symbol, establishment_timestamp=event.break_timestamp, trend=event.direction.value,
            approach=approach, frozen_level=event.protected_level,
            candidate_price=candidate["candidate_price"] if candidate else None,
            candidate_pivot_timestamp=candidate["candidate_pivot_timestamp"] if candidate else None,
            candidate_confirmation_timestamp=candidate["candidate_confirmation_timestamp"] if candidate else None,
            candidate_relationship=candidate["relationship"] if candidate else None,
            gains_protection=candidate is not None and event.protected_level is None,
            outside_setup=candidate is not None and not candidate["in_significant_setup"],
            monitoring_end=stop, monitoring_end_reason=stop_reason,
            first_opposing_close_timestamp=stamp, first_opposing_close=first.close if first else None,
            bars_to_opposing_close=elapsed, rapid_break_within_3_bars=sensitivity,
            additional_opposing_break=additional, potential_additional_choch_candidate=additional,
            competes_with_frozen_establishment_or_bos=additional and stamp == stop
            and stop_reason in ("trend_established", "bos"),
            frozen_choch_timestamp=actual_choch, potential_frozen_choch_change=change,
        ))
    return output


def make_chart(path, saved, event, comparisons):
    stamp = event.break_timestamp
    index = saved.indices[stamp]
    # Keep the first relevant shadow break in view when it is close enough.
    bars = saved.bars[max(0, index - 34):index + 31]
    direction = "up" if event.direction == MarketTrend.BULLISH else "down"
    row = saved.evidence(stamp, direction)
    major = {item[field] for item in saved.breaks
             for field in ("pivot_timestamp", "protected_pivot_timestamp")}
    levels = [item for item in comparisons if item["approach"] != "frozen"
              and item["candidate_price"] is not None and item["candidate_price"] != event.protected_level]
    render(path, bars, [swing for swing in saved.swings if swing["pivot_timestamp"] in major],
           saved.breaks, saved.choch, f"{saved.symbol} | {stamp:%Y-%m-%d %H:%M} UTC protection audit",
           audit_focus=dict(row, event_timestamp=stamp.isoformat(), external_state=row["external_state_at_close"],
                            internal_state=row["internal_state_at_close"]),
           protection_review=(stamp, levels, event.broken_level))


def generate(source, freeze, output, chart_cases=()):
    lock = json.loads(freeze.read_text())
    if lock["fingerprint"] != fingerprint():
        raise ValueError("Frozen implementation fingerprint mismatch")
    start = utc(lock["evaluation_start_inclusive"])
    groups = read_rows(source / "grouped_events.csv")
    candidates, comparisons, charts = [], [], []
    chart_cases = set(chart_cases)
    for symbol in SYMBOLS:
        saved = SavedRun(source / symbol, symbol, [row for row in groups if row["symbol"] == symbol])
        found, events = observe_establishments(saved, Parameters(**lock["parameters"]))
        for event, rows in found:
            if event.break_timestamp <= start:
                continue
            selections = choices(rows)
            for row in rows:
                row["selected_by"] = "|".join(name for name, chosen in selections.items() if chosen is row)
            result = compare(event, rows, saved.bars, events)
            comparisons.extend(result)
            candidates.extend(rows)
            if (symbol, event.break_timestamp) in chart_cases:
                charts.append((saved, event, result))
    if len(charts) != len(chart_cases):
        raise ValueError("Requested chart does not refer to a development trend establishment")
    output.mkdir(parents=True, exist_ok=False)
    (output / "charts").mkdir()
    write_rows(output / "protection_candidates.csv", tuple(candidates[0]), candidates)
    write_rows(output / "counterfactual_comparison.csv", tuple(comparisons[0]), comparisons)
    counts = {}
    for approach in APPROACHES:
        rows = [row for row in comparisons if row["approach"] == approach]
        counts[approach] = {
            key: sum(bool(row[key]) for row in rows) for key in
            ("gains_protection", "outside_setup", "rapid_break_within_3_bars", "additional_opposing_break",
             "potential_frozen_choch_change", "competes_with_frozen_establishment_or_bos")
        }
        counts[approach]["choch_change_types"] = dict(Counter(row["potential_frozen_choch_change"]
                                                              for row in rows if row["potential_frozen_choch_change"]))
    write_json(output / "run_metadata.json", dict(frozen_fingerprint=lock["fingerprint"]["sha256"],
               replay_parity=True, development_establishments=len(comparisons) // len(APPROACHES),
               candidate_rows=len(candidates), comparison_counts=counts))
    for saved, event, rows in charts:
        make_chart(output / "charts" / f"{saved.symbol}_{event.break_timestamp:%Y%m%dT%H%MZ}.svg",
                   saved, event, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "freeze", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--chart", action="append", default=[], metavar="SYMBOL=UTC_TIMESTAMP")
    args = parser.parse_args()
    cases = [(symbol, utc(stamp)) for symbol, stamp in (value.split("=", 1) for value in args.chart)]
    generate(args.source, args.freeze, args.output, cases)


if __name__ == "__main__":
    main()
