"""
SQLite database for signal tracking and reporting.

Table: signals
  id            INTEGER PK
  symbol        TEXT
  direction     TEXT  (LONG / SHORT)
  entry_price   REAL
  tp_price      REAL
  sl_price      REAL
  leverage      INTEGER
  status        TEXT  (pending / win / loss / expired)
  placed        INTEGER  (0 = not placed, 1 = placed by admin)
  generated_at  TEXT  (ISO UTC)
  placed_at     TEXT  (ISO UTC, nullable)
  closed_at     TEXT  (ISO UTC, nullable)
  pnl_roi       REAL  (nullable — +tp_roi or -sl_roi when closed)
"""

import sqlite3
import logging
from datetime import datetime, timezone
from contextlib import contextmanager
from config import DB_PATH

logger = logging.getLogger(__name__)


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,
                entry_price  REAL    NOT NULL,
                tp_price     REAL    NOT NULL,
                sl_price     REAL    NOT NULL,
                leverage     INTEGER NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending',
                placed       INTEGER NOT NULL DEFAULT 0,
                generated_at TEXT    NOT NULL,
                placed_at    TEXT,
                closed_at    TEXT,
                pnl_roi      REAL
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_generated_at
            ON signals (generated_at)
        """)
        # Migrate existing DB: add columns if missing
        for col, definition in [
            ("placed",    "INTEGER NOT NULL DEFAULT 0"),
            ("placed_at", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass  # column already exists
    logger.info("Database initialised")


def save_signal(symbol: str, direction: str, entry_price: float,
                tp_price: float, sl_price: float, leverage: int,
                generated_at: datetime) -> int:
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO signals
              (symbol, direction, entry_price, tp_price, sl_price,
               leverage, status, placed, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)
        """, (symbol, direction, entry_price, tp_price, sl_price,
              leverage, generated_at.isoformat()))
        return cur.lastrowid


def mark_placed(signal_id: int) -> bool:
    """Mark a signal as placed. Returns True if updated."""
    placed_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute("""
            UPDATE signals SET placed = 1, placed_at = ?
            WHERE id = ? AND status = 'pending'
        """, (placed_at, signal_id))
        return cur.rowcount > 0


def unmark_placed(signal_id: int) -> bool:
    """Remove placed mark from a signal. Returns True if updated."""
    with _conn() as con:
        cur = con.execute("""
            UPDATE signals SET placed = 0, placed_at = NULL
            WHERE id = ? AND status = 'pending'
        """, (signal_id,))
        return cur.rowcount > 0


def update_signal_outcome(signal_id: int, status: str, pnl_roi: float):
    """status: 'win' | 'loss' | 'expired'"""
    closed_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE signals
            SET status = ?, pnl_roi = ?, closed_at = ?
            WHERE id = ? AND status = 'pending'
        """, (status, pnl_roi, closed_at, signal_id))


def count_active_signals() -> int:
    """Count all pending signals (regardless of placed status)."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status = 'pending'"
        ).fetchone()
        return row[0]


def get_pending_signals() -> list[dict]:
    """Return pending signals that have been placed (for outcome monitoring)."""
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM signals
            WHERE status = 'pending' AND placed = 1
            ORDER BY generated_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_all_pending_signals() -> list[dict]:
    """Return all pending signals regardless of placed status."""
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM signals WHERE status = 'pending'
            ORDER BY generated_at ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_signals_in_range(start: datetime, end: datetime) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM signals
            WHERE generated_at >= ? AND generated_at < ?
            ORDER BY generated_at ASC
        """, (start.isoformat(), end.isoformat())).fetchall()
        return [dict(r) for r in rows]


def get_all_signals() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM signals ORDER BY generated_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def signal_exists_for_coin(symbol: str, since: datetime) -> bool:
    """Check if a pending signal already exists for this coin."""
    with _conn() as con:
        row = con.execute("""
            SELECT id FROM signals
            WHERE symbol = ?
              AND status = 'pending'
              AND generated_at >= ?
            LIMIT 1
        """, (symbol, since.isoformat())).fetchone()
        return row is not None
