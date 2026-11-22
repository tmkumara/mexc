"""Prepare review charts by replaying only the prefix ending at each case."""

from collections import defaultdict
import json
from math import hypot
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFont

from strategy.market_structure.models import FOUR_HOURS
from strategy.market_structure.structure import Parameters

from ..data import utc
from ..evaluate import read_rows
from ..plot import render
from ..protection_audit import inspect_candidates
from ..replay import write_json, write_rows
from ..shadow_protection import PairedReplay, event_at, model_row
from ..structure_audit import SavedRun
from ..validation import fingerprint
from .queue import load_queue


def rasterize(svg):
    """Rasterize the limited SVG primitives emitted by the local chart renderer."""
    root = ElementTree.parse(svg).getroot()
    canvas = Image.new("RGB", (int(root.get("width")), int(root.get("height"))))
    draw = ImageDraw.Draw(canvas)
    fonts = {}
    for element in root:
        kind = element.tag.split("}")[-1]
        a = element.attrib

        def numbers(*keys):
            return [float(a.get(key, 0)) for key in keys]

        if kind == "rect":
            x, y, width, height = numbers("x", "y", "width", "height")
            draw.rectangle((x, y, x + width, y + height), fill=a["fill"])
        elif kind == "line":
            x1, y1, x2, y2 = numbers("x1", "y1", "x2", "y2")
            width = max(1, round(float(a["stroke-width"])))
            pattern = [float(part) for part in a.get("stroke-dasharray", "").split()]
            length = hypot(x2 - x1, y2 - y1)
            if not pattern or not length:
                draw.line((x1, y1, x2, y2), fill=a["stroke"], width=width)
            else:
                cursor = 0
                while cursor < length:
                    stop = min(length, cursor + pattern[0])
                    draw.line(tuple(coordinate + (delta * point / length)
                                    for point in (cursor, stop)
                                    for coordinate, delta in ((x1, x2 - x1), (y1, y2 - y1))),
                              fill=a["stroke"], width=width)
                    cursor += sum(pattern)
        elif kind == "circle":
            x, y, radius = numbers("cx", "cy", "r")
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         fill=a["fill"], outline=a["stroke"], width=2)
        elif kind == "polygon":
            points = [tuple(map(float, pair.split(","))) for pair in a["points"].split()]
            draw.polygon(points, fill=a["fill"], outline=a["stroke"])
        elif kind == "text":
            size = int(a["font-size"])
            if size not in fonts:
                for filename in ("C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"):
                    try:
                        fonts[size] = ImageFont.truetype(filename, size)
                        break
                    except OSError:
                        continue
                else:
                    fonts[size] = ImageFont.load_default()
            text = (element.text or "").replace(
                "Candles right of the audit marker are subsequent context.",
                "This chart ends at the reviewed close.")
            draw.text(tuple(numbers("x", "y")), text, fill=a["fill"],
                      font=fonts[size], anchor="ls")
        else:
            raise ValueError(f"Unsupported local chart primitive: {kind}")
    return canvas


def evidence_at(engine, stamp, prior_state):
    current = event_at(engine, stamp)
    pivot = prior_state["protected_pivot"] or current.get("protected_pivot_timestamp")
    protected = next((swing for swing in engine.confirmed_swings
                      if swing.pivot_timestamp == pivot), None)
    latest_setup = next((row for row in reversed(engine.structure_events)
                         if row.kind == "trend_established"), None)
    reasons = [row for row in getattr(engine, "protection_decisions", ())
               if latest_setup and row["timestamp"] == latest_setup.break_timestamp]
    return dict(
        classification=current["classification"], prior=prior_state, current=current,
        protected_pivot=protected.pivot_timestamp if protected else None,
        protected_confirmation=protected.confirmation_timestamp if protected else None,
        protected_eligible=protected.confirmation_timestamp + FOUR_HOURS if protected else None,
        latest_establishment=latest_setup.break_timestamp if latest_setup else None,
        latest_establishment_target=latest_setup.broken_level if latest_setup else None,
        protection_decisions=reasons,
    )


def render_review(case, saved, parameters, destination):
    stamp = utc(case.timestamp)
    prefix = saved.bars[:saved.indices[stamp] + 1]
    paired = PairedReplay(parameters)
    frozen = paired.frozen.layers.external
    frozen_selection = []
    for index, candle in enumerate(prefix):
        setup = frozen.active_swings[-4:]
        paired.update(candle)
        event = frozen.structure_events[-1] if frozen.structure_events else None
        if event and event.kind == "trend_established" and event.break_timestamp == candle.closed_at:
            rows = inspect_candidates(event, frozen.confirmed_swings, setup, frozen.broken_swings,
                                      frozen.closed_through, prefix[:index + 1])
            frozen_selection = [dict(candidate_price=row["candidate_price"],
                                     reason=row["frozen_reasons"] or "frozen_post_target_protection_assigned")
                                for row in rows if row["in_significant_setup"]]
    bars = prefix[-116:]
    group = saved.groups.get((stamp, case.direction))
    panels, details = [], {}
    with TemporaryDirectory() as temp:
        for i, (label, engine) in enumerate((("frozen", paired.frozen.layers.external),
                                             ("shadow", paired.shadow))):
            detail = evidence_at(engine, stamp, paired.prior_states[stamp][i])
            if label == "frozen":
                detail["protection_decisions"] = frozen_selection
            if (detail["classification"] or "") != case.evidence[f"{label}_classification"]:
                raise ValueError(f"{case.case_id}: {label} prefix does not reproduce queued classification")
            details[label] = detail
            breaks = [model_row(event) for event in engine.structure_events]
            candidates = [model_row(event) for event in engine.choch_events]
            major = {item[key] for item in breaks
                     for key in ("pivot_timestamp", "protected_pivot_timestamp")}
            if detail["protected_pivot"]:
                major.add(str(detail["protected_pivot"]))
            swings = [dict(model_row(pivot), external="True", external_since=str(activation))
                      for pivot, activation in engine.structural_swings.items() if str(pivot.pivot_timestamp) in major]
            svg = Path(temp) / f"{label}.svg"
            render(svg, bars, swings, breaks, candidates,
                   f"{case.symbol} | {stamp:%Y-%m-%d %H:%M} UTC | {label.title()}",
                   discoveries=saved.discoveries.get((stamp, case.direction), ()),
                   grouped_events=[group] if group else [], audit_focus=dict(
                       event_timestamp=stamp.isoformat(), prior_external_state=detail["prior"]["state"],
                       external_state=engine.state.value, prior_internal_state=case.evidence.get("prior_internal_state", "context only"),
                       internal_state=case.evidence.get("prior_internal_state", "context only")))
            panels.append(rasterize(svg))
    pair = Image.new("RGB", (max(panel.width for panel in panels), sum(panel.height for panel in panels)))
    pair.paste(panels[0], (0, 0))
    pair.paste(panels[1], (0, panels[0].height))
    with destination.open("xb") as handle:
        pair.save(handle, format="PNG", optimize=True)
    return dict(case_id=case.case_id, first_candle_open=bars[0].timestamp,
                last_candle_open=bars[-1].timestamp, last_candle_close=bars[-1].closed_at,
                candle_count=len(bars), evidence=details)


def prepare(source, queue, freeze, output):
    lock = json.loads(freeze.read_text())
    if lock["fingerprint"] != fingerprint():
        raise ValueError("Frozen replay fingerprint mismatch")
    parameters = Parameters(**lock["parameters"])
    cases = load_queue(queue)
    groups = defaultdict(list)
    for group in read_rows(source / "grouped_events.csv"):
        groups[group["symbol"]].append(group)
    runs = {symbol: SavedRun(source / symbol, symbol, groups[symbol])
            for symbol in sorted({case.symbol for case in cases})}
    output.mkdir(parents=True, exist_ok=False)
    (output / "charts").mkdir()
    rows, manifest = [], []
    for case in cases:
        row = dict(case.evidence, case_id=case.case_id, event_id=case.event_id,
                   manual_classification="UNREVIEWED", reviewer_notes="")
        rows.append(row)
        manifest.append(render_review(case, runs[case.symbol], parameters,
                                      output / "charts" / f"{case.case_id}.png"))
    write_rows(output / "review_queue.csv", tuple(rows[0]), rows)
    write_json(output / "chart_manifest.json", manifest)
    return len(rows)
