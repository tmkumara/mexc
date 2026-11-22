"""Phase 2.11a: clearer second-opinion charts and neutral explanations.

Everything here is derived by replaying the frozen and shadow detectors over
the candles that closed at or before the reviewed close (the same prefix
replay used in Phase 2.10). No later candle is read, drawn, or described.
The detectors are imported and executed, never modified.
"""

from collections import defaultdict
import csv
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from strategy.market_structure.models import FOUR_HOURS
from strategy.market_structure.structure import Parameters

from ..evaluate import read_rows
from ..shadow_protection import PairedReplay, event_at
from ..structure_audit import SavedRun
from ..validation import fingerprint
from .io import utc
from .queue import load_queue
from .second_opinion import ROUND, SELECTION, detector_answers


ENGINES = ("frozen", "shadow")
MIN_CONTEXT_BARS = 60
MAX_CONTEXT_BARS = 330
NAMES = {
    "EXTERNAL_CHOCH": "CHoCH",
    "STRONG_EXTERNAL_CHOCH": "Strong CHoCH",
    "EXTERNAL_BOS": "BOS",
    "EXTERNAL_TREND_ESTABLISHED": "New trend (no protected level broken)",
    "": "No external event",
}
STRONG_REASONS = {
    "insufficient_trend_history": "the prior trend was counted as too young",
    "weak_displacement": "the close was not far enough beyond the level",
    "poor_body_quality": "the candle body was small relative to its range",
    "weak_close_position": "the candle did not close near its extreme",
    "weak_break": "the break distance was small relative to ATR",
}


def stamp_text(value):
    return value.strftime("%m-%d %H:%M") if value else "n/a"


def price_text(value):
    if value is None:
        return "n/a"
    return f"{value:.4f}".rstrip("0").rstrip(".") if value < 10 else f"{value:.2f}"


def trend_of(state):
    return state.split("_")[0].upper() if state and state != "unknown" else "UNKNOWN"


# ---------------------------------------------------------------------------
# Replay and context extraction


def replay_prefix(case, saved, parameters):
    """Replay only candles that closed at or before the reviewed close."""
    stamp = utc(case.timestamp)
    end = saved.indices[stamp]
    prefix = saved.bars[:end + 1]
    if prefix[-1].closed_at != stamp:
        raise ValueError(f"{case.case_id}: reviewed close not found in dataset")
    paired = PairedReplay(parameters)
    for candle in prefix:
        paired.update(candle)
    return prefix, paired


def swing_labels(active):
    """HH/HL/LH/LL for the ATR-filtered alternating swing sequence."""
    labels, last = {}, {}
    for swing in active:
        previous = last.get(swing.kind)
        if previous is None:
            labels[swing] = "SH" if swing.kind == "high" else "SL"
        elif swing.kind == "high":
            labels[swing] = "HH" if swing.price > previous.price else "LH"
        else:
            labels[swing] = "HL" if swing.price > previous.price else "LL"
        last[swing.kind] = swing
    return labels


def engine_context(engine, prior, stamp, swings_by_time):
    event = event_at(engine, stamp)
    classification = event["classification"] or ""
    trend = trend_of(prior["state"])
    protected_kind = None
    if prior["protected_level"] is not None:
        protected_kind = "low" if trend == "BULLISH" else "high"
    started = prior["trend_started_at"]
    establishing = next((row for row in engine.structure_events
                         if row.kind == "trend_established" and row.break_timestamp == started), None)
    bos = [row for row in engine.structure_events
           if row.kind == "bos" and started and started < row.break_timestamp < stamp]
    reasons = [item for item in (event.get("strong_rejection_reasons") or "").split("|") if item]
    earlier = [row for row in engine.structure_events if row.break_timestamp < stamp]
    last_event = earlier[-1] if earlier else None
    boundary = prior["external_high"] if trend == "BULLISH" else prior["external_low"] if trend == "BEARISH" else None
    return dict(
        classification=classification,
        prior_state=prior["state"], trend=trend,
        protected_level=prior["protected_level"], protected_kind=protected_kind,
        protected_pivot=prior["protected_pivot"], protected_since=prior["protected_since"],
        trend_started_at=started, bos_count=prior["continuation_count"],
        external_high=prior["external_high"], external_low=prior["external_low"],
        boundary=boundary,
        broken_level=event["broken_level"], broken_pivot=event["pivot_timestamp"],
        resulting_state=event["resulting_state"].value if event["resulting_state"] else None,
        strong_reasons=reasons,
        establishing=dict(level=establishing.broken_level, pivot=establishing.pivot_timestamp,
                          direction=establishing.direction.value.upper()) if establishing else None,
        bos_events=[dict(level=row.broken_level, at=row.break_timestamp) for row in bos],
        boundary_pivot=swings_by_time.get(boundary),
        last_event=dict(kind=last_event.kind, direction=last_event.direction.value.upper(),
                        at=last_event.break_timestamp, level=last_event.broken_level) if last_event else None,
    )


def window_start(case_index, engines, index_of):
    """First chart bar: enough history to show how the prior trend formed."""
    anchors = []
    for data in engines.values():
        for key, back in (("trend_started_at", 14), ("protected_pivot", 8)):
            stamp = data.get(key)
            if stamp is not None and stamp in index_of:
                anchors.append(index_of[stamp] - back)
        if data["trend"] == "UNKNOWN" and data["last_event"] and data["last_event"]["at"] in index_of:
            anchors.append(index_of[data["last_event"]["at"]] - 6)
        if data["establishing"] and data["establishing"]["pivot"] in index_of:
            anchors.append(index_of[data["establishing"]["pivot"]] - 12)
    desired = min(anchors + [case_index - MIN_CONTEXT_BARS + 1])
    return desired, max(desired, case_index - MAX_CONTEXT_BARS + 1)


def build_context(case, saved, parameters, first_pass):
    prefix, paired = replay_prefix(case, saved, parameters)
    stamp = utc(case.timestamp)
    last = len(prefix) - 1
    index_of = {}
    for i, bar in enumerate(prefix):
        index_of[bar.timestamp] = i
        index_of[bar.closed_at] = i
    frozen_engine, shadow_engine = paired.frozen.layers.external, paired.shadow
    if [swing.pivot_timestamp for swing in frozen_engine.confirmed_swings] != \
            [swing.pivot_timestamp for swing in shadow_engine.confirmed_swings]:
        raise ValueError(f"{case.case_id}: detectors disagree on confirmed pivots")
    swings_by_time = {swing.price: swing.pivot_timestamp for swing in frozen_engine.confirmed_swings}
    engines = {}
    for label, engine, prior in (("frozen", frozen_engine, paired.prior_states[stamp][0]),
                                 ("shadow", shadow_engine, paired.prior_states[stamp][1])):
        data = engine_context(engine, prior, stamp, swings_by_time)
        if data["classification"] != case.evidence[f"{label}_classification"]:
            raise ValueError(f"{case.case_id}: {label} replay does not reproduce the queued classification")
        engines[label] = data
    desired, start = window_start(last, engines, index_of)
    first = max(0, start)
    active = list(frozen_engine.active_swings)
    labels = swing_labels(active)
    active_set = set(active)
    swings = [dict(kind=swing.kind, price=swing.price, pivot=swing.pivot_timestamp,
                   confirmed=swing.confirmation_timestamp, index=index_of[swing.pivot_timestamp],
                   significant=swing in active_set, label=labels.get(swing))
              for swing in frozen_engine.confirmed_swings
              if swing.confirmation_timestamp <= stamp and index_of.get(swing.pivot_timestamp, -1) >= first]
    bar = prefix[-1]
    grouped = [float(value) for value in (case.evidence.get("grouped_broken_levels") or "").split("|") if value]
    return dict(
        case_id=case.case_id, symbol=case.symbol, close_time=stamp, direction=case.direction,
        open=bar.open, high=bar.high, low=bar.low, close=bar.close,
        bars=prefix[first:], window_first=first, event_index=last - first,
        dataset_start=saved.bars[0].timestamp,
        context_incomplete=desired < 0, missing_bars=max(0, -desired),
        truncated=start > desired and desired >= 0,
        engines=engines, swings=swings, grouped=grouped,
        pivot_right=parameters.pivot_right,
        selection=SELECTION[case.case_id], first_pass=first_pass.get(case.case_id),
    )


# ---------------------------------------------------------------------------
# Neutral plain-English explanation


def against_trend(direction, trend):
    if trend == "UNKNOWN":
        return "no established trend to compare against"
    if (direction == "up") == (trend == "BEARISH"):
        return f"AGAINST the prior {trend.lower()} trend"
    return f"in the SAME direction as the prior {trend.lower()} trend"


def detector_reason(name, data, ctx):
    cls = data["classification"]
    side = "below" if ctx["direction"] == "down" else "above"
    if cls in ("EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH"):
        text = (f"{name} reports {NAMES[cls]} because the close ({price_text(ctx['close'])}) went {side} "
                f"the protected {data['protected_kind']} {price_text(data['protected_level'])} that it was "
                f"using to guard the {data['trend'].lower()} trend.")
        if cls == "EXTERNAL_CHOCH" and data["strong_reasons"]:
            missing = "; ".join(STRONG_REASONS.get(item, item.replace("_", " ")) for item in data["strong_reasons"])
            text += f" It is not labelled Strong because {missing}."
        elif cls == "STRONG_EXTERNAL_CHOCH":
            text += (f" It is labelled Strong: it dates the trend from {stamp_text(data['trend_started_at'])} "
                     f"with {data['bos_count']} BOS before this break, and the break candle passed its quality checks.")
        return text
    if cls == "EXTERNAL_BOS":
        return (f"{name} reports BOS because the close went {side} its trend boundary "
                f"{price_text(data['broken_level'])} in the direction of its {data['trend'].lower()} trend.")
    if cls == "EXTERNAL_TREND_ESTABLISHED":
        before = ("it had no established trend" if data["trend"] == "UNKNOWN"
                  else f"its {data['trend'].lower()} trend had no protected level")
        return (f"{name} reports a new trend start, not a CHoCH: {before}, so nothing protected could be "
                f"broken. A fresh swing sequence plus the close {side} {price_text(data['broken_level'])} "
                f"set a new direction.")
    if data["trend"] == "UNKNOWN":
        return (f"{name} reports no external event: it had no established trend at this time, so it "
                f"cannot raise a CHoCH or BOS, and no new trend setup qualified on this close.")
    guard = (f"the protected {data['protected_kind']} {price_text(data['protected_level'])}"
             if data["protected_level"] is not None else "a protected level (none was assigned)")
    return (f"{name} reports no external event: the close did not cross {guard} or its trend boundary "
            f"{price_text(data['boundary'])}.")


def differences(ctx):
    frozen, shadow = ctx["engines"]["frozen"], ctx["engines"]["shadow"]
    items = []
    if frozen["prior_state"] != shadow["prior_state"]:
        items.append(f"prior state ({frozen['prior_state']} vs {shadow['prior_state']})")
    if frozen["protected_level"] != shadow["protected_level"]:
        items.append(f"protected level ({price_text(frozen['protected_level'])} vs "
                     f"{price_text(shadow['protected_level'])})")
    if frozen["trend_started_at"] != shadow["trend_started_at"]:
        items.append(f"trend start ({stamp_text(frozen['trend_started_at'])} vs "
                     f"{stamp_text(shadow['trend_started_at'])})")
    if frozen["bos_count"] != shadow["bos_count"]:
        items.append(f"BOS count ({frozen['bos_count']} vs {shadow['bos_count']})")
    if frozen["classification"] != shadow["classification"]:
        items.insert(0, f"classification ({NAMES[frozen['classification']]} vs "
                        f"{NAMES[shadow['classification']]})")
    return items


def relevant_level(ctx, data):
    """The level a reviewer should check the close against, per detector."""
    if data["broken_level"] is not None:
        return data["broken_level"], "the level it reports as broken"
    if data["trend"] != "UNKNOWN":
        against = (ctx["direction"] == "up") == (data["trend"] == "BEARISH")
        if against and data["protected_level"] is not None:
            return data["protected_level"], f"its protected {data['protected_kind']}"
        if not against and data["boundary"] is not None:
            return data["boundary"], "its trend boundary"
    if ctx["grouped"]:
        level = max(ctx["grouped"]) if ctx["direction"] == "up" else min(ctx["grouped"])
        return level, "the furthest confirmed swing crossed by this close"
    return None, None


def explanation(ctx):
    frozen, shadow = ctx["engines"]["frozen"], ctx["engines"]["shadow"]
    side = "below" if ctx["direction"] == "down" else "above"
    lines = [f"{ctx['case_id']} — {ctx['symbol']} 4H, candle closing {ctx['close_time']:%Y-%m-%d %H:%M} UTC",
             "", "BEFORE THE BREAK"]
    for name, data in (("Frozen", frozen), ("Shadow", shadow)):
        if data["trend"] == "UNKNOWN":
            reset = data["last_event"]
            lines.append(f"- {name}: trend UNKNOWN (no established direction)." + (
                f" Its last external event was a {EVENT_NAMES[reset['kind']]} ({reset['direction'].lower()}) "
                f"at {stamp_text(reset['at'])} through {price_text(reset['level'])}; after a CHoCH the detector "
                f"waits for a new trend setup before it can label CHoCH or BOS again." if reset else ""))
            continue
        evidence = ""
        if data["establishing"]:
            evidence = (f" It became {data['trend'].lower()} at the close of {stamp_text(data['trend_started_at'])} "
                        f"when price closed {'above' if data['trend'] == 'BULLISH' else 'below'} the swing "
                        f"{price_text(data['establishing']['level'])}.")
        bos = f" {data['bos_count']} BOS since then." if data["bos_count"] else " No BOS since then."
        guard = (f" Protected {data['protected_kind']}: {price_text(data['protected_level'])} "
                 f"(swing at {stamp_text(data['protected_pivot'])}, protecting since {stamp_text(data['protected_since'])})."
                 if data["protected_level"] is not None else " No protected level was assigned.")
        lines.append(f"- {name}: {data['trend']} trend.{evidence}{bos}{guard}")
    significant = [swing for swing in ctx["swings"] if swing["significant"]][-6:]
    if significant:
        sequence = ", ".join(f"{swing['label']} {price_text(swing['price'])}" for swing in significant)
        lines.append(f"- Last significant confirmed swings (oldest first): {sequence}.")
    lines += ["", "AT THE BREAK",
              f"- This 4H candle closed at {price_text(ctx['close'])} (high {price_text(ctx['high'])}, "
              f"low {price_text(ctx['low'])}), breaking {'up' if ctx['direction'] == 'up' else 'down'}."]
    if ctx["grouped"]:
        lines.append(f"- Confirmed swing levels its close crossed: "
                     f"{', '.join(price_text(value) for value in ctx['grouped'][:8])}"
                     f"{' …' if len(ctx['grouped']) > 8 else ''}.")
    for name, data in (("Frozen", frozen), ("Shadow", shadow)):
        level, what = relevant_level(ctx, data)
        relation = against_trend(ctx["direction"], data["trend"])
        relation = (f"{name}: no established trend to compare the break direction against" if data["trend"] == "UNKNOWN"
                    else f"{name}: the break is {relation}")
        if level is None:
            lines.append(f"- {relation}; no reference level is available.")
            continue
        beyond = ctx["close"] < level if ctx["direction"] == "down" else ctx["close"] > level
        lines.append(f"- {relation}. Relevant level: {price_text(level)} ({what}); "
                     f"the close is {'beyond' if beyond else 'NOT beyond'} it ({side} = beyond).")
    lines += ["", "DETECTOR INTERPRETATION",
              f"- {detector_reason('Frozen', frozen, ctx)}",
              f"- {detector_reason('Shadow', shadow, ctx)}"]
    diff = differences(ctx)
    lines.append("- The detectors " + ("agree on every recorded field." if not diff
                                       else "differ on: " + "; ".join(diff) + "."))
    lines += ["", "MISSING OR WEAK EVIDENCE"]
    missing = []
    if ctx["context_incomplete"]:
        missing.append(f"The dataset starts {stamp_text(ctx['dataset_start'])} UTC; about {ctx['missing_bars']} "
                       f"earlier candles would be needed to show the full trend setup.")
    for name, data in (("Frozen", frozen), ("Shadow", shadow)):
        if data["trend"] == "UNKNOWN":
            missing.append(f"{name} has no established trend, so it cannot judge CHoCH vs BOS here.")
        elif data["protected_level"] is None:
            missing.append(f"{name} has a {data['trend'].lower()} trend but no protected level.")
        elif data["trend_started_at"] and not data["bos_count"]:
            missing.append(f"{name}'s trend has no BOS yet (single leg since {stamp_text(data['trend_started_at'])}).")
    if not missing:
        missing.append("No missing structural evidence recorded by either detector.")
    lines += [f"- {item}" for item in missing]
    lines += ["", "Neither detector's answer is assumed correct. The chart and this text stop at this close."]
    return "\n".join(lines)


def caption(ctx, position, total):
    frozen, shadow = ctx["engines"]["frozen"], ctx["engines"]["shadow"]

    def line(name, data):
        guard = (f", protected {data['protected_kind']} {price_text(data['protected_level'])}"
                 if data["protected_level"] is not None else ", no protected level")
        return f"{name}: {NAMES[data['classification']]} · prior {data['trend']}{guard if data['trend'] != 'UNKNOWN' else ''}"

    diff = differences(ctx)
    return "\n".join([
        f"Second opinion {position}/{total} · {ctx['case_id']}",
        f"{ctx['symbol']} · 4H close {ctx['close_time']:%Y-%m-%d %H:%M} UTC · break "
        f"{'UP' if ctx['direction'] == 'up' else 'DOWN'} · close {price_text(ctx['close'])}",
        line("Frozen", frozen), line("Shadow", shadow),
        "Detectors agree" if not diff else "Detectors differ on: " + diff[0],
        "Tap Explain for a plain-English walkthrough. Which answer fits this close?",
    ])[:1000]


# ---------------------------------------------------------------------------
# Chart rendering (direct raster, mobile sized)

WIDTH = 1280
AXIS = 240
LABELLED_SWINGS = 8
LEFT = 16
TITLE = 96
PANEL_HEAD = 78
PLOT = 470
PANEL_GAP = 26
LEGEND = 132
EVENT_NAMES = {"choch": "CHoCH", "bos": "BOS", "trend_established": "new trend"}
COLORS = dict(bg="#0f1720", panel="#131d28", grid="#1f2a36", text="#e6edf3", muted="#8b98a5",
              up="#26a69a", down="#ef5350", protected="#ffb020", external="#4fa3ff",
              broken_up="#2ee59d", broken_down="#ff4d6d", band="#2b3a4c", minor="#9aa7b4",
              unconfirmed="#1c2633", trend="#c084fc", crossed="#b8c2cc")


def font(size, bold=False):
    names = (("C:/Windows/Fonts/arialbd.ttf", "DejaVuSans-Bold.ttf") if bold
             else ("C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf"))
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Scale:
    def __init__(self, low, high, top, count):
        pad = (high - low) * 0.06 or high * 0.01
        self.low, self.high = low - pad, high + pad
        self.top, self.count = top, count
        self.step = (WIDTH - AXIS - LEFT) / count

    def y(self, price):
        return self.top + PLOT * (self.high - price) / (self.high - self.low)

    def x(self, index):
        return LEFT + self.step * (index + 0.5)

    def inside(self, price):
        return self.low <= price <= self.high


def shared_range(ctx):
    """Candle range plus levels that sit close to it; distant levels are clipped."""
    bars = ctx["bars"]
    low = min(bar.low for bar in bars)
    high = max(bar.high for bar in bars)
    span = high - low
    for data in ctx["engines"].values():
        for key in ("protected_level", "broken_level", "boundary"):
            value = data[key]
            if value is not None and low - 0.12 * span <= value <= high + 0.12 * span:
                low, high = min(low, value), max(high, value)
    return low, high


def text_box(draw, xy, text, fill, size=18, bold=False, anchor="la", bg=None):
    face = font(size, bold)
    box = draw.textbbox(xy, text, font=face, anchor=anchor)
    if bg:
        draw.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=bg)
    draw.text(xy, text, fill=fill, font=face, anchor=anchor)
    return box


def overlaps(box, placed):
    return any(not (box[2] < other[0] or box[0] > other[2] or box[3] < other[1] or box[1] > other[3])
               for other in placed)


def draw_panel(draw, ctx, name, data, top, scale):
    bars, event = ctx["bars"], ctx["event_index"]
    plot_top = top + PANEL_HEAD
    draw.rectangle((0, top, WIDTH, plot_top + PLOT), fill=COLORS["panel"])
    headline = f"{name.upper()}: {NAMES[data['classification']]}"
    text_box(draw, (LEFT, top + 10), headline, COLORS["text"], 28, True)
    if data["trend"] == "UNKNOWN":
        reset = data["last_event"]
        prior = ("Previous trend: UNKNOWN (no established direction)" if not reset else
                 f"Previous trend: UNKNOWN since {EVENT_NAMES[reset['kind']]} {reset['direction'].lower()} "
                 f"at {stamp_text(reset['at'])} (state resets after a CHoCH)")
    else:
        guard = (f" · protected {data['protected_kind']} {price_text(data['protected_level'])}"
                 if data["protected_level"] is not None else " · no protected level")
        prior = (f"Previous trend: {data['trend']} since {stamp_text(data['trend_started_at'])}"
                 f" · {data['bos_count']} BOS{guard}")
    text_box(draw, (LEFT, top + 48), prior, COLORS["muted"], 20)

    # grid and axis prices
    grid = []
    for step in range(5):
        price = scale.low + (scale.high - scale.low) * (step + 0.5) / 5
        y = scale.y(price)
        draw.line((LEFT, y, WIDTH - AXIS, y), fill=COLORS["grid"], width=1)
        grid.append((y, price))

    # unconfirmable zone and break candle band
    zone = scale.x(event - ctx["pivot_right"] + 1) - scale.step / 2
    draw.rectangle((zone, plot_top, scale.x(event) + scale.step / 2, plot_top + PLOT), fill=COLORS["unconfirmed"])
    bx = scale.x(event)
    draw.rectangle((bx - max(5, scale.step), plot_top, bx + max(5, scale.step), plot_top + PLOT), fill=COLORS["band"])

    # trend start marker
    started = data["trend_started_at"]
    trend_label = None
    if data["trend"] == "UNKNOWN" and data["last_event"]:
        index = next((i for i, bar in enumerate(bars) if bar.closed_at == data["last_event"]["at"]), None)
        if index is not None:
            x = scale.x(index)
            for y in range(int(plot_top), int(plot_top + PLOT), 10):
                draw.line((x, y, x, y + 5), fill=COLORS["trend"], width=2)
            trend_label = (x + 6, f"trend reset here: {EVENT_NAMES[data['last_event']['kind']]} "
                                  f"{stamp_text(data['last_event']['at'])}")
    if started:
        index = next((i for i, bar in enumerate(bars) if bar.closed_at == started), None)
        if index is not None:
            x = scale.x(index)
            for y in range(int(plot_top), int(plot_top + PLOT), 10):
                draw.line((x, y, x, y + 5), fill=COLORS["trend"], width=2)
            trend_label = (x + 6, f"{data['trend']} trend set {stamp_text(started)}")
        else:
            trend_label = (LEFT + 4, f"← {data['trend']} trend set {stamp_text(started)} (before chart)")

    # candles
    body = max(1.0, scale.step * 0.62)
    for i, bar in enumerate(bars):
        x = scale.x(i)
        color = COLORS["up"] if bar.close >= bar.open else COLORS["down"]
        draw.line((x, scale.y(bar.high), x, scale.y(bar.low)), fill=color, width=1)
        top_y, bottom_y = sorted((scale.y(bar.open), scale.y(bar.close)))
        draw.rectangle((x - body / 2, top_y, x + body / 2, max(bottom_y, top_y + 1)), fill=color)

    # horizontal levels (shared y-scale; distant levels clipped with an edge note)
    tags = []
    reserved = [(bx - 160, plot_top + 4, bx + 6, plot_top + 28)]
    if trend_label:
        width = draw.textlength(trend_label[1], font=font(16, True))
        reserved.append((trend_label[0] - 4, plot_top + PLOT - 30, trend_label[0] + width + 4, plot_top + PLOT))
    index_of = {bar.timestamp: i for i, bar in enumerate(bars)}

    def level(price, pivot, color, label, width=2, dash=False):
        if price is None:
            return
        if not scale.inside(price):
            edge = plot_top + 4 if price > scale.high else plot_top + PLOT - 26
            arrow = "↑" if price > scale.high else "↓"
            tags.append((edge + 11, f"{arrow} {label} {price_text(price)} (off chart)", color, True))
            return
        y = scale.y(price)
        start = scale.x(index_of[pivot]) if pivot in index_of else LEFT
        x = start
        while x < WIDTH - AXIS:
            end = min(WIDTH - AXIS, x + (9 if dash else WIDTH))
            draw.line((x, y, end, y), fill=color, width=width)
            x = end + (6 if dash else 0)
        tags.append((y, f"{label} {price_text(price)}", color, False))

    if data["boundary"] is not None and data["boundary"] != data["broken_level"]:
        level(data["boundary"], data["boundary_pivot"], COLORS["external"],
              "Trend boundary", 2, True)
    if data["protected_level"] is not None:
        level(data["protected_level"], data["protected_pivot"], COLORS["protected"],
              f"PROTECTED {data['protected_kind'].upper()}", 3)
    broken_color = COLORS["broken_up"] if ctx["direction"] == "up" else COLORS["broken_down"]
    if data["broken_level"] is not None:
        same = data["broken_level"] == data["protected_level"]
        level(data["broken_level"], data["broken_pivot"], broken_color,
              "BROKEN = PROTECTED" if same else "BROKEN", 3)
    elif ctx["grouped"]:
        crossed = max(ctx["grouped"]) if ctx["direction"] == "up" else min(ctx["grouped"])
        pivot = next((s["pivot"] for s in ctx["swings"] if s["price"] == crossed), None)
        level(crossed, pivot, COLORS["crossed"], "Minor swing crossed", 2, True)

    # swings: the last few significant swings get HH/HL/LH/LL labels; older ones a plain dot
    placed = [box for box in reserved]
    face = font(16, True)
    significant = [swing for swing in ctx["swings"] if swing["significant"]]
    labelled = {id(swing) for swing in significant[-LABELLED_SWINGS:]}
    plot_right = WIDTH - AXIS - 4
    for swing in ctx["swings"]:
        i = swing["index"] - ctx["window_first"]
        if i < 0 or i >= len(bars):
            continue
        x, y = scale.x(i), scale.y(swing["price"])
        high = swing["kind"] == "high"
        if not swing["significant"]:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), outline=COLORS["minor"], width=1)
            continue
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=COLORS["text"])
        if id(swing) not in labelled:
            continue
        text = f"{swing['label']} {price_text(swing['price'])}"
        width = draw.textlength(text, font=face)
        cx = min(max(x, LEFT + width / 2 + 4), plot_right - width / 2 - 4)
        lo = int(max(0, (cx - width / 2 - LEFT) / scale.step - 1))
        hi = int(min(len(bars) - 1, (cx + width / 2 - LEFT) / scale.step + 1))
        neighbourhood = bars[lo:hi + 1]
        above = scale.y(max(bar.high for bar in neighbourhood)) - 8
        below = scale.y(min(bar.low for bar in neighbourhood)) + 8
        order = ([above - k * 21 for k in range(5)] + [below + k * 21 for k in range(5)] if high
                 else [below + k * 21 for k in range(5)] + [above - k * 21 for k in range(5)])
        choice = None
        for yy in order:
            top_anchor = yy >= below
            if yy < plot_top + 18 or yy > plot_top + PLOT - 22:
                continue
            box = draw.textbbox((cx, yy), text, font=face, anchor="mt" if top_anchor else "ms")
            box = (box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2)
            if not overlaps(box, placed):
                choice = (yy, top_anchor, box)
                break
        if choice is None:
            continue  # no free space without covering candles or other labels; dot stays
        yy, top_anchor, box = choice
        placed.append(box)
        draw.line((x, y, cx, box[3] if not top_anchor else box[1]), fill=COLORS["muted"], width=1)
        draw.rectangle(box, fill=COLORS["panel"])
        draw.text((cx, yy), text, fill=COLORS["text"], font=face, anchor="mt" if top_anchor else "ms")

    if trend_label:
        text_box(draw, (trend_label[0], plot_top + PLOT - 10), trend_label[1], COLORS["trend"], 16, True,
                 anchor="ls", bg=COLORS["panel"])

    # close tag on the axis and right-side level tags without collisions
    tags.append((scale.y(ctx["close"]), f"Close {price_text(ctx['close'])}", broken_color, False))
    used = []
    edge_used = []
    for y, label, color, edge in sorted(tags, key=lambda item: item[0]):
        if edge:
            while any(abs(y - other) < 22 for other in edge_used):
                y += 22 if y < plot_top + PLOT / 2 else -22
            edge_used.append(y)
            text_box(draw, (LEFT + 4, y), label, color, 17, True, anchor="lm", bg=COLORS["panel"])
            continue
        target = max(plot_top + 10, min(plot_top + PLOT - 10, y))
        while any(abs(target - other) < 21 for other in used):
            target += 21
        used.append(target)
        if abs(target - y) > 2:
            draw.line((WIDTH - AXIS - 2, y, WIDTH - AXIS + 4, target), fill=color, width=1)
        text_box(draw, (WIDTH - AXIS + 8, target), label, color, 15, True, anchor="lm", bg=COLORS["bg"])
    for y, price in grid:
        if all(abs(y - other) > 16 for other in used):
            text_box(draw, (WIDTH - AXIS + 8, y), price_text(price), COLORS["muted"], 16, anchor="lm")
    text_box(draw, (bx - 10, plot_top + 22), "BREAK CANDLE →", COLORS["text"], 15, True,
             anchor="rs", bg=COLORS["panel"])


def swatch(draw, x, y, kind, color):
    mid = y + 11
    if kind == "dot":
        draw.ellipse((x + 4, mid - 5, x + 14, mid + 5), fill=color)
    elif kind == "ring":
        draw.ellipse((x + 5, mid - 4, x + 13, mid + 4), outline=color, width=1)
    elif kind == "zone":
        draw.rectangle((x + 2, y + 2, x + 16, y + 20), fill=color, outline=COLORS["muted"])
    elif kind == "solid":
        draw.line((x, mid, x + 18, mid), fill=color, width=3)
    elif kind == "dash":
        for start in (0, 7, 14):
            draw.line((x + start, mid, x + start + 4, mid), fill=color, width=2)
    else:  # vertical dash
        for start in (2, 9, 16):
            draw.line((x + 9, y + start, x + 9, y + start + 4), fill=color, width=2)


def render(ctx, destination):
    height = TITLE + 2 * (PANEL_HEAD + PLOT) + PANEL_GAP + LEGEND
    image = Image.new("RGB", (WIDTH, height), COLORS["bg"])
    draw = ImageDraw.Draw(image)
    direction = "UP" if ctx["direction"] == "up" else "DOWN"
    text_box(draw, (LEFT, 14), f"{ctx['symbol']} · 4H · close {ctx['close_time']:%Y-%m-%d %H:%M} UTC · "
             f"break {direction}", COLORS["text"], 30, True)
    first = ctx["bars"][0].timestamp
    note = f"Chart starts {first:%m-%d %H:%M} and ENDS at the reviewed close — no later candles."
    if ctx["context_incomplete"]:
        note += f" History incomplete: data begins {stamp_text(ctx['dataset_start'])}."
    text_box(draw, (LEFT, 58), note, COLORS["protected"] if ctx["context_incomplete"] else COLORS["muted"], 19)
    low, high = shared_range(ctx)
    tops = (TITLE, TITLE + PANEL_HEAD + PLOT + PANEL_GAP)
    for (name, data), top in zip(ctx["engines"].items(), tops):
        draw_panel(draw, ctx, name, data, top, Scale(low, high, top + PANEL_HEAD, len(ctx["bars"])))
    y = height - LEGEND + 14
    items = [("dot", COLORS["text"], "HH/HL/LH/LL: significant confirmed swing"),
             ("ring", COLORS["minor"], "minor confirmed pivot"),
             ("zone", COLORS["unconfirmed"], f"last {ctx['pivot_right']} candles: swings not yet confirmable")]
    present = {key for data in ctx["engines"].values() for key in ("protected_level", "boundary", "broken_level")
               if data[key] is not None}
    if "protected_level" in present:
        items.append(("solid", COLORS["protected"], "protected level"))
    if "boundary" in present:
        items.append(("dash", COLORS["external"], "trend boundary"))
    if "broken_level" in present:
        items.append(("solid", COLORS["broken_up"] if ctx["direction"] == "up" else COLORS["broken_down"],
                      "broken level"))
    elif ctx["grouped"]:
        items.append(("dash", COLORS["crossed"], "minor swing crossed"))
    if any(data["trend_started_at"] for data in ctx["engines"].values()):
        items.append(("vdash", COLORS["trend"], "prior trend set"))
    if any(data["trend"] == "UNKNOWN" and data["last_event"] for data in ctx["engines"].values()):
        items.append(("vdash", COLORS["trend"], "event that reset the trend"))
    x = LEFT
    for kind, color, label in items:
        width = draw.textlength(label, font=font(18)) + 44
        if x + width > WIDTH - LEFT:
            x, y = LEFT, y + 30
        swatch(draw, x, y, kind, color)
        text_box(draw, (x + 24, y), label, COLORS["muted"], 18)
        x += width
    text_box(draw, (LEFT, height - 24), "Frozen and shadow panels share the same time and price scale.",
             COLORS["muted"], 16)
    with destination.open("xb") as handle:
        image.save(handle, format="PNG", optimize=True)
    return image.size


# ---------------------------------------------------------------------------
# Artifact generation


def iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def serialisable(ctx):
    engines = {name: {key: ([{k: iso(v) for k, v in item.items()} for item in value] if key == "bos_events"
                            else {k: iso(v) for k, v in value.items()} if isinstance(value, dict)
                            else iso(value))
                      for key, value in data.items()}
               for name, data in ctx["engines"].items()}
    return dict(
        case_id=ctx["case_id"], symbol=ctx["symbol"], close_time=iso(ctx["close_time"]),
        direction=ctx["direction"], close=ctx["close"], high=ctx["high"], low=ctx["low"],
        chart=f"charts/{ctx['case_id']}.png", explanation=f"explanations/{ctx['case_id']}.md",
        first_candle_open=iso(ctx["bars"][0].timestamp), last_candle_close=iso(ctx["bars"][-1].closed_at),
        candle_count=len(ctx["bars"]), context_incomplete=ctx["context_incomplete"],
        dataset_start=iso(ctx["dataset_start"]), engines=engines,
        detector_answers=sorted(detector_answers(ctx["engines"]["frozen"]["classification"],
                                                 ctx["engines"]["shadow"]["classification"])),
        differences=differences(ctx), selection_reason=ctx["selection"],
        first_pass_label=ctx["first_pass"],
        swings=[{k: iso(v) for k, v in swing.items()} for swing in ctx["swings"] if swing["significant"]],
    )


def prepare_second(source, queue, freeze, first_pass_csv, output):
    lock = json.loads(freeze.read_text())
    if lock["fingerprint"] != fingerprint():
        raise ValueError("Frozen replay fingerprint mismatch")
    parameters = Parameters(**lock["parameters"])
    first_pass = {row["case_id"]: row["review_label"] for row in read_rows(first_pass_csv)} if first_pass_csv else {}
    cases = {case.case_id: case for case in load_queue(queue)}
    missing = set(SELECTION) - set(cases)
    if missing:
        raise ValueError(f"Second-opinion cases missing from queue: {sorted(missing)}")
    groups = defaultdict(list)
    for group in read_rows(source / "grouped_events.csv"):
        groups[group["symbol"]].append(group)
    runs = {}
    output.mkdir(parents=True, exist_ok=False)
    (output / "charts").mkdir()
    (output / "explanations").mkdir()
    manifest = []
    for position, case_id in enumerate(SELECTION, 1):
        case = cases[case_id]
        if case.symbol not in runs:
            runs[case.symbol] = SavedRun(source / case.symbol, case.symbol, groups[case.symbol])
        ctx = build_context(case, runs[case.symbol], parameters, first_pass)
        render(ctx, output / "charts" / f"{case_id}.png")
        text = explanation(ctx)
        (output / "explanations" / f"{case_id}.md").write_text(text + "\n", encoding="utf-8")
        row = serialisable(ctx)
        row["position"] = position
        row["caption"] = caption(ctx, position, len(SELECTION))
        row["explanation_text"] = text
        manifest.append(row)
    (output / "second_opinion_queue.json").write_text(
        json.dumps(dict(round=ROUND, fingerprint=lock["fingerprint"]["sha256"],
                        source_queue=str(queue.as_posix()), cases=manifest), indent=2) + "\n",
        encoding="utf-8")
    with (output / "first_vs_second_template.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("case_id", "selection_reason", "frozen_classification", "shadow_classification",
                         "first_pass_label", "second_pass_label", "second_pass_reason", "second_pass_note",
                         "changed", "follow_up"))
        for row in manifest:
            writer.writerow((row["case_id"], row["selection_reason"],
                             row["engines"]["frozen"]["classification"] or "NONE",
                             row["engines"]["shadow"]["classification"] or "NONE",
                             row["first_pass_label"] or "", "", "", "", "", ""))
    return len(manifest)
