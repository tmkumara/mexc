"""Render standalone SVG candlestick charts from a replay directory."""

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

from .data import load_candles, utc
from .evaluate import read_rows


def render(path, bars, swings, breaks, candidates, title, internal_swings=(), internal_events=(),
           discoveries=(), simplified=False, grouped_events=(), audit_focus=None, transitions=(),
           protection_review=None):
    width, height = 1500, 706 if grouped_events and discoveries else 680
    if audit_focus:
        height = 741 if discoveries else 716
    if transitions:
        height = max(height, 740)
    alternatives = {}
    if protection_review:
        establishment, reviews, target = protection_review
        for review in reviews:
            key = review["candidate_pivot_timestamp"], review["candidate_price"]
            alternatives.setdefault(key, []).append(review)
        height = max(height, 756 + 17 * len(alternatives))
    left, right, top, bottom = 82, 1468, 80, 586
    step = (right - left) / len(bars)
    starts = {bar.timestamp: index for index, bar in enumerate(bars)}
    closes = {bar.closed_at: index for index, bar in enumerate(bars)}
    prior = [event for event in breaks if utc(event["break_timestamp"]) < bars[0].closed_at]
    visible = [event for event in breaks if utc(event["break_timestamp"]) in closes]
    local_breaks = [event for event in internal_events if event["kind"] in ("bos", "choch")
                    and utc(event["break_timestamp"]) in closes]
    discovery_groups = {}
    for event in discoveries:
        stamp = utc(event["break_timestamp"])
        if stamp in closes:
            discovery_groups.setdefault((stamp, event["classification"]), []).append(event)
    displayed = prior[-1:] + visible
    levels = [float(event["protected_level"]) for event in displayed if event["protected_level"]]
    levels.extend(float(event["broken_level"]) for event in visible + local_breaks)
    levels.extend(float(event["broken_level"]) for group in discovery_groups.values()
                  for event in group)
    levels.extend(price for _, price in alternatives)
    levels.extend(float(swing["price"]) for swing in swings if swing["external"] == "True"
                  and (utc(swing["confirmation_timestamp"]) in closes
                       or utc(swing.get("external_since") or swing["confirmation_timestamp"]) in closes))
    low = min([bar.low for bar in bars] + levels)
    high = max([bar.high for bar in bars] + levels)
    padding = (high - low or high) * .06
    low, high = low - padding, high + padding
    marks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Arial, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#101923"/>',
    ]

    def x(index):
        return left + (index + .5) * step

    def y(price):
        return bottom - (price - low) / (high - low) * (bottom - top)

    def line(x1, y1, x2, y2, color, dash="", weight=1):
        marks.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                     f'stroke="{color}" stroke-width="{weight}" stroke-dasharray="{dash}"/>')

    def label(px, py, value, color="#dce7f0", size=13):
        marks.append(f'<text x="{px:.2f}" y="{py:.2f}" fill="{color}" '
                     f'font-size="{size}">{escape(value)}</text>')

    def marker(px, py, color, tooltip, radius=4, fill=None):
        marks.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="{radius}" stroke="{color}" '
                     f'stroke-width="2" fill="{fill or color}"><title>{escape(tooltip)}</title></circle>')

    def local_marker(px, py, color, tooltip, points):
        vertices = " ".join(f"{px + dx:.2f},{py + dy:.2f}" for dx, dy in points)
        marks.append(f'<polygon points="{vertices}" fill="#101923" stroke="{color}" '
                     f'stroke-width="1.5"><title>{escape(tooltip)}</title></polygon>')

    label(left, 35, title, size=23)
    label(left, 59, "4H UTC | Dotted: pivot to confirmation | Dashed: confirmation to external activation")
    for tick in range(7):
        price = low + (high - low) * tick / 6
        line(left, y(price), right, y(price), "#293a4a")
        label(8, y(price) + 4, f"{price:.6g}")
    for i, bar in enumerate(bars):
        color = "#43bea0" if bar.close >= bar.open else "#f17680"
        line(x(i), y(bar.high), x(i), y(bar.low), color)
        body_width = max(.8, min(11, step * .64))
        marks.append(f'<rect x="{x(i) - body_width / 2:.2f}" y="{y(max(bar.open, bar.close)):.2f}" '
                     f'width="{body_width:.2f}" height="{max(1, abs(y(bar.open) - y(bar.close))):.2f}" '
                     f'fill="{color}"/>')
        if i % max(1, (len(bars) + 9) // 10) == 0:
            label(x(i), bottom + 23, bar.timestamp.strftime("%m-%d %H:%M"), size=11)
    if audit_focus:
        stamp = utc(audit_focus["event_timestamp"])
        audit_x = x(closes[stamp])
        line(audit_x, top, audit_x, bottom, "#e6eef7", "3 5")
        label(min(audit_x + 8, right - 190), top + 13, f"Audit close {stamp:%m-%d %H:%M} UTC")
        label(left, 691, "Confirmed pivots first enter break decisions on the next close. "
              "Candles right of the audit marker are subsequent context.")
        label(left, 708,
              f"External {audit_focus['prior_external_state']} -> {audit_focus['external_state']} | "
              f"Internal {audit_focus['prior_internal_state']} -> {audit_focus['internal_state']}")

    for swing in swings:
        if swing["external"] != "True":
            continue
        origin = starts.get(utc(swing["pivot_timestamp"]))
        confirmation = closes.get(utc(swing["confirmation_timestamp"]))
        activation_stamp = swing.get("external_since") or swing["confirmation_timestamp"]
        activation = closes.get(utc(activation_stamp))
        if confirmation is None and activation is None:
            continue
        level = y(float(swing["price"]))
        color = "#71cfff" if swing["kind"] == "high" else "#ffd17c"
        if origin is not None:
            marker(x(origin), level, color, f"Swing {swing['kind']} origin", 3, "#101923")
        if confirmation is not None:
            line(x(origin) if origin is not None else left, level, x(confirmation), level, color, "2 4")
            marker(x(confirmation), level, color,
                   f"Swing {swing['kind']} confirmed {swing['confirmation_timestamp']}", 3,
                   "#101923" if activation_stamp != swing["confirmation_timestamp"] else color)
        if activation is not None and activation != confirmation:
            line(x(confirmation) if confirmation is not None else left, level,
                 x(activation), level, color, "5 4")
            marker(x(activation), level, color, f"External since {activation_stamp}", 3)

    protected = None
    protected_start = None
    events = {utc(event["event_timestamp"]): event for event in candidates}
    for event in displayed:
        stamp = utc(event["break_timestamp"])
        if stamp > bars[-1].closed_at:
            break
        index = closes.get(stamp)
        next_protected = float(event["protected_level"]) if event["protected_level"] else None
        if index is not None:
            if protected is not None:
                line(protected_start, y(protected), x(index), y(protected), "#e8aa55", "7 4", 2)
            level = y(float(event["broken_level"]))
            confirmed = closes.get(utc(event["pivot_confirmation_timestamp"]))
            eligible_x = x(confirmed) if confirmed is not None else left
            if event["kind"] != "choch":
                line(eligible_x, level, x(index), level, "#9381dd", "4 3")
                tooltip = f"{event['kind']} {event['direction']} {stamp}"
                if audit_focus:
                    local_marker(x(index), level, "#a896ff", tooltip,
                                 ((-1, -5), (1, -5), (1, -1), (5, -1), (5, 1), (1, 1),
                                  (1, 5), (-1, 5), (-1, 1), (-5, 1), (-5, -1), (-1, -1)))
                else:
                    marker(x(index), level, "#a896ff", tooltip, 4)
            else:
                candidate = events.get(stamp)
                strong = candidate and candidate["strong_choch"] == "True"
                color = "#6fffe0" if event["direction"] == "bullish" else "#ff727c"
                marker(x(index), y(float(event["break_close"])), color,
                       f"{'Strong ' if strong else ''}CHoCH {event['direction']} {stamp}",
                       8 if strong else 5, color if strong else "#101923")
                if strong:
                    label(x(index) + 9, y(float(event["break_close"])) - 7, "Strong", color, 11)
        protected = next_protected
        protected_start = x(index) if index is not None else left
    if protected is not None:
        line(protected_start, y(protected), right, y(protected), "#e8aa55", "7 4", 2)

    local_activations = {swing["pivot_timestamp"]: swing["internal_since"] for swing in internal_swings
                         if swing["internal"] == "True"}
    for event in local_breaks:
        stamp = utc(event["break_timestamp"])
        index = closes[stamp]
        activation = closes.get(utc(local_activations.get(event["pivot_timestamp"])
                                    or event["pivot_confirmation_timestamp"]))
        level = y(float(event["broken_level"]))
        line(x(activation) if activation is not None else left, level,
             x(index), level, "#778a9d", "2 4")
        if activation is not None:
            marker(x(activation), level, "#98adbf", "Internal swing became active", 2)
        tooltip = f"{event['classification']} {event['direction']} {stamp}"
        if event["kind"] == "bos":
            local_marker(x(index), level, "#a9c8ef", tooltip,
                         ((-3, -3), (3, -3), (3, 3), (-3, 3)))
        else:
            tip = -5 if event["direction"] == "bullish" else 5
            local_marker(x(index), y(float(event["break_close"])), "#f4bcfd", tooltip,
                         ((0, tip), (-5, -tip), (5, -tip)))

    for (stamp, classification), group in discovery_groups.items():
        index = closes[stamp]
        upward = group[0]["direction"] == "up"
        prices = sorted({float(event["broken_level"]) for event in group})
        tooltip = f"{classification} {stamp} | " + ", ".join(f"{price:g}" for price in prices)
        if classification.startswith("RANGE"):
            event = group[0]
            color = "#ffb485"
            level = y(float(event["broken_level"]))
            activation = closes.get(utc(event["activation_timestamp"]))
            line(x(activation) if activation is not None else left, level,
                 x(index), level, color, "7 4")
            tip = -6 if upward else 6
            local_marker(x(index), level, color, tooltip, ((0, tip), (-5, 0), (0, -tip), (5, 0)))
        else:
            visible_prices = prices if grouped_events else [prices[-1] if upward else prices[0]]
            for price in visible_prices:
                marker(x(index), y(price), "#9de292", tooltip, 2.5, "#101923")

    for event in grouped_events:
        stamp = utc(event["event_timestamp"])
        if stamp not in closes:
            continue
        index = closes[stamp]
        up = event["direction"] == "up"
        color = "#86e9ae" if up else "#fb9d81"
        px = x(index)
        py = y(float(event["break_close"])) + (10 if up else -10)
        tip = -4 if up else 4
        tooltip = f"Grouped break {event['direction']} {stamp} | {event['swing_level_count']} swings"
        points = ((-4, -4), (4, -4), (4, 4), (-4, 4)) if audit_focus else (
            (0, tip), (-4, -tip), (4, -tip))
        local_marker(px, py, color, tooltip, points)
        if event["range_break"] == "True":
            local_marker(px + 8, py, "#ffc56e", f"Grouped range break {stamp}",
                         ((0, -4), (-4, 0), (0, 4), (4, 0)))

    for event in transitions:
        index = closes.get(utc(event["event_timestamp"]))
        if index is None:
            continue
        py = y(float(event["break_close"])) + (17 if event["new_trend"] == "bullish" else -17)
        local_marker(x(index), py, "#60e5fa",
                     f"UNPROTECTED_TREND_TRANSITION {event['previous_trend']} to {event['new_trend']}",
                     ((-6, -4), (0, -7), (6, -4), (6, 4), (0, 7), (-6, 4)))
    if transitions:
        label(left, 730, "Cyan hexagon: opposite trend established from an unprotected state", "#60e5fa")

    if protection_review:
        color = "#f1abf8"
        label(left, 729, f"Broken target {target:g} | Pink dashed: hypothetical protection | "
              "Pink diamond: first opposing close", color)
        for i, ((pivot, price), reviews) in enumerate(alternatives.items()):
            review = reviews[0]
            confirmation = review["candidate_confirmation_timestamp"]
            start = x(closes[establishment])
            end_stamp = review["first_opposing_close_timestamp"] or review["monitoring_end"]
            end = x(closes[end_stamp]) if end_stamp in closes else right
            level = y(price)
            line(start, level, end, level, color, "7 4", 1.6)
            if pivot in starts and confirmation in closes:
                line(x(starts[pivot]), level, x(closes[confirmation]), level, color, "2 4")
                marker(x(starts[pivot]), level, color, f"Candidate pivot {pivot}", 3, "#101923")
                marker(x(closes[confirmation]), level, color, f"Candidate confirmed {confirmation}", 3)
            if review["first_opposing_close_timestamp"] in closes:
                local_marker(end, level, color, f"First opposing close {end_stamp}",
                             ((0, -5), (-5, 0), (0, 5), (5, 0)))
            names = " / ".join(item["approach"].replace("_", " ") for item in reviews)
            label(left, 749 + i * 17, f"{price:g} | {names} | pivot {pivot:%m-%d %H:%M} UTC | "
                  f"confirmed {confirmation:%m-%d %H:%M} UTC", color)

    legend = (("#71cfff", "External high"), ("#ffd17c", "External low"),
              ("#a896ff", "Cross: BOS / trend start" if audit_focus else "External BOS / trend start"),
              ("#e8aa55", "External protection"),
              ("#6fffe0", "External bullish CHoCH"), ("#ff727c", "External bearish CHoCH"),
              ("#dce7f0", "Filled large marker = Strong"))
    if simplified:
        legend = legend[:2] + (legend[-1],) if grouped_events else legend[:2]
    for i, (color, description) in enumerate(legend):
        label(left + i * 191, 647, description, color)
    if internal_swings:
        label(left, 666, "Dotted gray: internal swing level", "#98adbf")
        if not audit_focus:
            label(left + 235, 666, "Square: internal BOS", "#a9c8ef")
        label(left + (235 if audit_focus else 400), 666, "Triangle: internal CHoCH", "#f4bcfd")
    if discoveries:
        label(left + 610 if internal_swings else left, 666, "Green ring: discovered swing break", "#9de292")
        label(left + 870 if internal_swings else left + 260, 666,
              "Orange diamond: discovered range break", "#ffb485")
    if grouped_events:
        row = (729 if audit_focus else 687) if discoveries else 666
        start = left + 460 if audit_focus else left
        label(start, row, "Square: grouped break" if audit_focus else "Triangles: grouped breaks", "#86e9ae")
        label(start + 260, row, "Gold diamond: grouped range", "#ffc56e")
    marks.append("</svg>")
    with path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(marks))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Replay output directory")
    parser.add_argument("--discovery", choices=("off", "overlay", "simplified", "both"), default="off")
    parser.add_argument("--grouped", type=Path, help="Grouped CSV for simplified and debug charts")
    args = parser.parse_args()
    directory = args.input
    metadata = json.loads((directory / "run_metadata.json").read_text(encoding="utf-8"))
    symbol = metadata["symbol"]
    bars, _ = load_candles(directory / "candles_4h.csv", symbol, "4h",
                           utc(metadata["evaluation_end_exclusive"]))
    swings = read_rows(directory / "confirmed_swings.csv")
    breaks = read_rows(directory / "structure_events.csv")
    candidates = read_rows(directory / "choch_candidates.csv")
    internal_swings = []
    internal_events = []
    if (directory / "internal_events.csv").exists():
        internal_swings = read_rows(directory / "internal_swings.csv")
        internal_events = read_rows(directory / "internal_events.csv")
    discoveries = []
    if args.discovery != "off":
        discoveries = read_rows(directory / "discovered_breaks.csv")
    indices = {bar.closed_at: index for index, bar in enumerate(bars)}
    charts = directory / "charts"
    charts.mkdir(exist_ok=False)
    if args.grouped:
        grouped = [row for row in read_rows(args.grouped) if row["symbol"] == symbol]
        major_pivots = {event[key] for event in breaks
                        for key in ("pivot_timestamp", "protected_pivot_timestamp")}
        major = [swing for swing in swings if swing["pivot_timestamp"] in major_pivots]
        strong = [event for event in candidates if event["strong_choch"] == "True"]
        strong_stamps = {utc(event["event_timestamp"]) for event in strong}
        strong_breaks = [event for event in breaks if utc(event["break_timestamp"]) in strong_stamps
                         and event["kind"] == "choch"]
        render(charts / "grouped_simplified.svg", bars, major, strong_breaks, strong,
               f"{symbol} grouped structural breaks", simplified=True, grouped_events=grouped)
        raw = read_rows(directory / "discovered_breaks.csv")
        render(charts / "grouped_debug.svg", bars, swings, breaks, candidates,
               f"{symbol} grouped breaks and raw evidence", internal_swings, internal_events,
               raw, grouped_events=grouped)
        print(f"Chart directory: {charts}")
        return
    if args.discovery != "simplified":
        render(charts / "overview.svg", bars, swings, breaks, candidates, f"{symbol} market structure",
               internal_swings, internal_events, discoveries)
        for event in candidates:
            if event["strong_choch"] != "True":
                continue
            stamp = utc(event["event_timestamp"])
            index = indices[stamp]
            context = bars[max(0, index - 45):index + 25]
            name = f"{stamp:%Y%m%dT%H%MZ}_{event['direction']}.svg"
            render(charts / name, context, swings, breaks, candidates,
                   f"{symbol} Strong {event['direction']} CHoCH | {stamp:%Y-%m-%d %H:%M} UTC",
                   internal_swings, internal_events, discoveries)
    if args.discovery in ("simplified", "both"):
        major_pivots = {event[key] for event in breaks
                        for key in ("pivot_timestamp", "protected_pivot_timestamp")}
        major = [swing for swing in swings if swing["pivot_timestamp"] in major_pivots]
        render(charts / "discovery_simplified.svg", bars, major, (), (),
               f"{symbol} structural break discovery", discoveries=discoveries, simplified=True)
    print(f"Chart directory: {charts}")


if __name__ == "__main__":
    main()
