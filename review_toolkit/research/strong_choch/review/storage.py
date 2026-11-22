"""Append-only, per-reviewer SQLite history and navigation."""

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3


class StaleReview(ValueError):
    pass


class Storage:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    case_id TEXT NOT NULL, event_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL, event_timestamp TEXT NOT NULL, reviewer_id INTEGER NOT NULL,
                    review_label TEXT, reviewed_at TEXT NOT NULL, review_version INTEGER NOT NULL,
                    notes TEXT NOT NULL, action TEXT NOT NULL, undo_target INTEGER UNIQUE,
                    prior_label TEXT, prior_notes TEXT NOT NULL,
                    UNIQUE (case_id, reviewer_id, review_version)
                );
                CREATE TABLE IF NOT EXISTS navigation (
                    reviewer_id INTEGER PRIMARY KEY, case_id TEXT NOT NULL
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
        return db.execute("""SELECT * FROM history WHERE reviewer_id=? AND case_id=?
                          ORDER BY review_version DESC LIMIT 1""", (reviewer, case_id)).fetchone()

    def effective(self, reviewer, case_id):
        with self.connection() as db:
            item = self.latest(db, reviewer, case_id)
            return dict(item) if item else None

    def append(self, db, case, reviewer, request, latest, label, notes, action, target=None):
        db.execute("""INSERT INTO history (request_id, case_id, event_id, symbol, timeframe,
                   event_timestamp, reviewer_id, review_label, reviewed_at, review_version,
                   notes, action, undo_target, prior_label, prior_notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (request, case.case_id, case.event_id, case.symbol, "4h", case.timestamp,
                    reviewer, label, datetime.now(timezone.utc).isoformat(),
                    1 + (latest["review_version"] if latest else 0), notes, action, target,
                    latest["review_label"] if latest else None, latest["notes"] if latest else ""))

    def write(self, case, reviewer, request, *, label=None, notes=None, expected=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM history WHERE request_id=?", (request,)).fetchone():
                return False
            latest = self.latest(db, reviewer, case.case_id)
            previous_label = latest["review_label"] if latest else None
            previous_notes = latest["notes"] if latest else ""
            version = latest["review_version"] if latest else 0
            new_label = previous_label if label is None else label
            new_notes = previous_notes if notes is None else notes
            if (new_label, new_notes) == (previous_label, previous_notes):
                return False
            if expected is not None and expected != version:
                raise StaleReview("Review changed. Reopen the case before correcting it.")
            self.append(db, case, reviewer, request, latest, new_label, new_notes,
                        "note" if notes is not None else "decision")
            return True

    def undo(self, cases, reviewer, request):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM history WHERE request_id=?", (request,)).fetchone():
                return None
            undone = db.execute("""SELECT h.* FROM history h
                WHERE reviewer_id=? AND action='decision' AND NOT EXISTS
                (SELECT 1 FROM history u WHERE u.undo_target=h.id)
                ORDER BY h.id DESC LIMIT 1""", (reviewer,)).fetchone()
            if undone is None or undone["case_id"] not in cases:
                return None
            case = cases[undone["case_id"]]
            latest = self.latest(db, reviewer, case.case_id)
            self.append(db, case, reviewer, request, latest, undone["prior_label"],
                        latest["notes"], "undo", undone["id"])
            return case.case_id

    def history(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM history ORDER BY id")]

    def cursor(self, reviewer, case_id=None):
        with self.connection() as db:
            if case_id is not None:
                db.execute("""INSERT INTO navigation VALUES (?,?)
                           ON CONFLICT(reviewer_id) DO UPDATE SET case_id=excluded.case_id""",
                           (reviewer, case_id))
            row = db.execute("SELECT case_id FROM navigation WHERE reviewer_id=?", (reviewer,)).fetchone()
            return row[0] if row else None
