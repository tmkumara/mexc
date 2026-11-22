"""Phase 2.11a second-opinion review: labels, selection, storage and service.

Second-opinion decisions live in their own SQLite file and tables. The
first-pass database (Phase 2.10) is only ever read, never written.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .queue import CASE_PATTERN


ROUND = "phase_2_11a"

# Source of truth: strong_choch_results/phase_2_11/PHASE_2_11_REVIEW_ANALYSIS.md, section 7.
SELECTION = {
    "ADA_20260919_0400_U": "First-pass Valid CHoCH conflicts with the recorded trend: upward break inside an external uptrend; prior trend start was off the Phase 2.10 chart.",
    "ETH_20261008_1200_D": "Both detectors report CHoCH (shadow Strong) but first pass said Not CHoCH; only Strong case; range breakdown vs reversal question.",
    "BNB_20261020_2000_U": "First-pass Valid CHoCH while both detectors were in an unknown state; possible missed CHoCH.",
    "ETH_20260819_1600_U": "Frozen/shadow CHoCH disagreement (shadow CHoCH, frozen new trend); range breakout vs reversal.",
    "ETH_20261024_1200_D": "First-pass label was Ambiguous; internal break in the trend direction.",
    "ETH_20260821_0400_U": "Three-way disagreement: frozen BOS, shadow new trend, first pass Ordinary Break.",
}

LABELS = {
    "ch": "CHOCH_REVERSAL",
    "bo": "BOS_CONTINUATION",
    "mi": "MINOR_BREAK",
    "nv": "NO_VALID_BREAK",
    "un": "UNSURE",
}
BUTTONS = {
    "ch": "CHoCH — Reversal",
    "bo": "BOS — Continuation",
    "mi": "Minor Break",
    "nv": "No Valid Break",
    "un": "Unsure",
}
DEFINITIONS = {
    "ch": "a trend was established before this candle AND this 4H close went beyond the protected "
          "level guarding it (e.g. closed below the protected low of an uptrend).",
    "bo": "a trend was established AND this close went beyond the trend boundary in the SAME "
          "direction (e.g. closed above the last significant high of an uptrend).",
    "mi": "the close crossed a swing, but only a small/internal one, not the protected level or the "
          "trend boundary.",
    "nv": "no meaningful level was closed beyond (wick only, or nothing structural was crossed).",
    "un": "the chart does not give enough evidence (trend unclear, history missing, levels unclear).",
}
REASONS = {
    "pt": ("PREVIOUS_TREND_UNCLEAR", "Previous trend unclear"),
    "pl": ("PROTECTED_LEVEL_INCORRECT", "Protected level incorrect"),
    "sd": ("SAME_DIRECTION_AS_TREND", "Break in same direction as trend"),
    "is": ("INTERNAL_SWING_ONLY", "Only an internal swing broken"),
    "nc": ("NO_CLOSE_BEYOND_LEVEL", "Candle did not close beyond level"),
    "ih": ("INSUFFICIENT_HISTORY", "Insufficient chart history"),
    "ot": ("OTHER", "Other / custom note"),
}
CALLBACK = re.compile(rf"(d|r):([a-z]{{2}}):({CASE_PATTERN}):(0|[1-9]\d{{0,6}})|(x|p|n):({CASE_PATTERN})")


def detector_answers(frozen, shadow):
    """Second-opinion answers each detector's classification is consistent with."""
    answers = set()
    for classification in (frozen, shadow):
        if classification in ("EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH"):
            answers.add("CHOCH_REVERSAL")
        elif classification == "EXTERNAL_BOS":
            answers.add("BOS_CONTINUATION")
        else:
            # No event, or a new trend without a protected level being broken.
            answers.update(("MINOR_BREAK", "NO_VALID_BREAK"))
    return answers


def needs_reason(label, answers):
    return label == "UNSURE" or label not in answers


def parse_callback(payload, prefix):
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 64 or not payload.startswith(prefix):
        raise ValueError("Invalid review button")
    match = CALLBACK.fullmatch(payload[len(prefix):])
    if not match:
        raise ValueError("Invalid review button")
    if match.group(1):
        kind, code, case_id, version = match.group(1, 2, 3, 4)
        table = LABELS if kind == "d" else REASONS
        if code not in table:
            raise ValueError("Invalid review button")
        return kind, code, case_id, int(version)
    return match.group(5), None, match.group(6), None


def load_manifest(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("round") != ROUND:
        raise ValueError("Second-opinion manifest round mismatch")
    cases = data["cases"]
    ids = [row["case_id"] for row in cases]
    if ids != list(SELECTION) or len(set(ids)) != len(ids):
        raise ValueError("Second-opinion manifest does not match the Phase 2.11 selection")
    return {row["case_id"]: row for row in cases}


class StaleDecision(ValueError):
    pass


class SecondOpinionStorage:
    """Append-only second-pass history in a database separate from the first pass."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS second_history (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    round TEXT NOT NULL, case_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    symbol TEXT NOT NULL, event_timestamp TEXT NOT NULL, reviewer_id INTEGER NOT NULL,
                    label TEXT, reason TEXT, note TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
                    version INTEGER NOT NULL, prior_label TEXT, prior_reason TEXT,
                    recorded_at TEXT NOT NULL,
                    UNIQUE (round, case_id, reviewer_id, version)
                );
                CREATE TABLE IF NOT EXISTS second_navigation (
                    round TEXT NOT NULL, reviewer_id INTEGER NOT NULL, case_id TEXT NOT NULL,
                    PRIMARY KEY (round, reviewer_id)
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def latest(db, reviewer, case_id):
        return db.execute("""SELECT * FROM second_history WHERE round=? AND reviewer_id=? AND case_id=?
                          ORDER BY version DESC LIMIT 1""", (ROUND, reviewer, case_id)).fetchone()

    def effective(self, reviewer, case_id):
        with self.connection() as db:
            row = self.latest(db, reviewer, case_id)
            return dict(row) if row else None

    def record(self, case, reviewer, request, action, expected, *, label=None, reason=None, note=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM second_history WHERE request_id=?", (request,)).fetchone():
                return None
            latest = self.latest(db, reviewer, case.case_id)
            version = latest["version"] if latest else 0
            if expected is not None and expected != version:
                raise StaleDecision("This case changed. Reopen it with /review_second_case before answering.")
            current_label = latest["label"] if latest else None
            current_reason = latest["reason"] if latest else None
            current_note = latest["note"] if latest else ""
            if action == "decision":
                if label == current_label:
                    return None
                new = (label, None, current_note)  # a new classification needs a new reason
            elif action == "reason":
                if current_label is None:
                    raise ValueError("Choose a classification before a reason.")
                if reason == current_reason:
                    return None
                new = (current_label, reason, current_note)
            elif action == "note":
                if current_label is None:
                    raise ValueError("Choose a classification before adding a note.")
                new = (current_label, current_reason, note)
            else:
                raise ValueError("Invalid second-opinion action")
            db.execute("""INSERT INTO second_history (request_id, round, case_id, event_id, symbol,
                       event_timestamp, reviewer_id, label, reason, note, action, version,
                       prior_label, prior_reason, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (request, ROUND, case.case_id, case.event_id, case.symbol, case.timestamp, reviewer,
                        *new, action, version + 1, current_label, current_reason,
                        datetime.now(timezone.utc).isoformat()))
            return version + 1

    def history(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM second_history WHERE round=? ORDER BY id", (ROUND,))]

    def cursor(self, reviewer, case_id=None):
        with self.connection() as db:
            if case_id is not None:
                db.execute("""INSERT INTO second_navigation VALUES (?,?,?)
                           ON CONFLICT(round, reviewer_id) DO UPDATE SET case_id=excluded.case_id""",
                           (ROUND, reviewer, case_id))
            row = db.execute("SELECT case_id FROM second_navigation WHERE round=? AND reviewer_id=?",
                             (ROUND, reviewer)).fetchone()
            return row[0] if row else None


def export_second(manifest, storage, output):
    """Write second-pass history and a first-vs-second comparison (read-only on storage contents)."""
    import csv

    history = storage.history()
    output.mkdir(parents=True, exist_ok=False)
    columns = ("id", "case_id", "reviewer_id", "label", "reason", "note", "action", "version",
               "prior_label", "prior_reason", "recorded_at")
    with (output / "second_opinion_history.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(history)
    latest = {}
    for row in history:
        latest[row["case_id"], row["reviewer_id"]] = row
    with (output / "first_vs_second.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(("case_id", "reviewer_id", "frozen_classification", "shadow_classification",
                         "first_pass_label", "second_pass_label", "second_pass_reason", "second_pass_note",
                         "corrections"))
        for case_id, row in manifest.items():
            for (key, reviewer), state in sorted(latest.items()):
                if key != case_id:
                    continue
                corrections = sum(1 for item in history if item["case_id"] == case_id
                                  and item["reviewer_id"] == reviewer and item["action"] == "decision") - 1
                writer.writerow((case_id, reviewer, row["engines"]["frozen"]["classification"] or "NONE",
                                 row["engines"]["shadow"]["classification"] or "NONE",
                                 row.get("first_pass_label") or "", state["label"] or "",
                                 state["reason"] or "", state["note"], max(0, corrections)))
    return len(history)


class SecondOpinionService:
    def __init__(self, manifest, cases, storage, allowed, first_pass=None):
        self.manifest = manifest
        self.cases = {key: cases[key] for key in manifest}
        self.order = tuple(manifest)
        self.storage = storage
        self.allowed = frozenset(allowed)
        self.first_pass = first_pass

    def authorized(self, reviewer, chat_type):
        return chat_type == "private" and type(reviewer) is int and reviewer in self.allowed

    def require(self, reviewer):
        if type(reviewer) is not int or reviewer not in self.allowed:
            raise PermissionError("Reviewer is not authorized")

    def case(self, reviewer, case_id):
        self.require(reviewer)
        if case_id not in self.cases:
            raise ValueError("Unknown second-opinion case. Use /review_second_progress to list them.")
        return self.cases[case_id]

    def open_case(self, reviewer, case_id):
        case = self.case(reviewer, case_id)
        self.storage.cursor(reviewer, case_id)
        return case

    def navigate(self, reviewer, action, anchor=None):
        self.require(reviewer)
        current = anchor or self.storage.cursor(reviewer)
        index = self.order.index(current) if current in self.cases else -1
        if action == "previous":
            return self.open_case(reviewer, self.order[max(0, index - 1)])
        if action == "first":
            current_state = self.storage.effective(reviewer, current) if current in self.cases else None
            if current in self.cases and not (current_state and current_state["label"]):
                return self.open_case(reviewer, current)
        cycle = self.order[index + 1:] + self.order[:index + 1]
        for key in cycle:
            state = self.storage.effective(reviewer, key)
            if not state or not state["label"]:
                return self.open_case(reviewer, key)
        return None

    def answers(self, case_id):
        row = self.manifest[case_id]
        return detector_answers(row["engines"]["frozen"]["classification"],
                                row["engines"]["shadow"]["classification"])

    def decide(self, reviewer, case_id, code, version, request):
        case = self.case(reviewer, case_id)
        if code not in LABELS:
            raise ValueError("Invalid second-opinion answer")
        return self.storage.record(case, reviewer, f"second:{request}", "decision", version, label=LABELS[code])

    def reason(self, reviewer, case_id, code, version, request):
        case = self.case(reviewer, case_id)
        if code not in REASONS:
            raise ValueError("Invalid reason")
        return self.storage.record(case, reviewer, f"second:{request}", "reason", version,
                                   reason=REASONS[code][0])

    def note(self, reviewer, case_id, text, request):
        case = self.case(reviewer, case_id)
        text = text.strip()
        if not text or len(text) > 1000:
            raise ValueError("Notes must be 1 to 1,000 characters.")
        return self.storage.record(case, reviewer, f"second:{request}:note", "note", None, note=text)

    def first_label(self, reviewer, case_id):
        if self.first_pass is None:
            return self.manifest[case_id].get("first_pass_label")
        state = self.first_pass.effective(reviewer, case_id)
        return state["review_label"] if state else None

    def progress(self, reviewer):
        self.require(reviewer)
        rows = []
        for key in self.order:
            state = self.storage.effective(reviewer, key) or {}
            label = state.get("label")
            pending = bool(label and needs_reason(label, self.answers(key)) and not state.get("reason"))
            rows.append((key, label, state.get("reason"), pending))
        return rows
