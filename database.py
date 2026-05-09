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
  placed        INTEGER  (always 1 — all signals auto-tracked)
  generated_at  TEXT  (ISO UTC)
  placed_at     TEXT  (ISO UTC — same as generated_at for auto-placed)
  closed_at     TEXT  (ISO UTC, nullable)
  pnl_roi       REAL  (nullable — +tp_roi or -sl_roi when closed)
"""

import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from config import DB_PATH, ZONE_EXPIRE_HOURS, ZONE_EXPIRE_ACCEPTED_HOURS

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
        # ── sweep zones ───────────────────────────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS sweep_zones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,
                zone_low    REAL NOT NULL,
                zone_high   REAL NOT NULL,
                status      TEXT NOT NULL DEFAULT 'accepted',
                detected_at TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                signal_id   INTEGER,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_zones_symbol_status
            ON sweep_zones(symbol, status)
        """)
        # ── signals ───────────────────────────────────────────────
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
                placed       INTEGER NOT NULL DEFAULT 1,
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
            ("placed",    "INTEGER NOT NULL DEFAULT 1"),
            ("placed_at", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass
    logger.info("Database initialised")


def save_signal(symbol: str, direction: str, entry_price: float,
                tp_price: float, sl_price: float, leverage: int,
                generated_at: datetime) -> int:
    ts = generated_at.isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO signals
              (symbol, direction, entry_price, tp_price, sl_price,
               leverage, status, placed, generated_at, placed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
        """, (symbol, direction, entry_price, tp_price, sl_price,
              leverage, ts, ts))
        return cur.lastrowid


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
    """Count all pending signals."""
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status = 'pending'"
        ).fetchone()
        return row[0]


def get_pending_signals() -> list[dict]:
    """Return all pending signals for outcome monitoring."""
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


# ── sweep zone tracking ───────────────────────────────────────────

def upsert_zone(symbol: str, direction: str,
                zone_low: float, zone_high: float,
                detected_at: datetime) -> int:
    """
    Return the id of an existing active zone with matching anchor level
    (zone_high for LONG, zone_low for SHORT, within 0.1%), or insert a new one.
    Updates updated_at on every call so we know the zone is still live.
    """
    now    = datetime.now(timezone.utc).isoformat()
    ts     = detected_at.isoformat()
    anchor = zone_high if direction == "LONG" else zone_low

    with _conn() as con:
        rows = con.execute("""
            SELECT id, zone_low, zone_high FROM sweep_zones
            WHERE symbol = ? AND direction = ?
              AND status IN ('accepted', 'waiting_retest')
            ORDER BY detected_at DESC LIMIT 20
        """, (symbol, direction)).fetchall()

        for row in rows:
            existing = row["zone_high"] if direction == "LONG" else row["zone_low"]
            if abs(existing - anchor) / max(anchor, 1e-12) < 0.001:
                con.execute(
                    "UPDATE sweep_zones SET updated_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return row["id"]

        cur = con.execute("""
            INSERT INTO sweep_zones
              (symbol, direction, zone_low, zone_high, status, detected_at, updated_at)
            VALUES (?, ?, ?, ?, 'accepted', ?, ?)
        """, (symbol, direction, zone_low, zone_high, ts, now))
        logger.info(
            f"[ZONE] New #{cur.lastrowid} {direction} {symbol} "
            f"[{zone_low:.6g}, {zone_high:.6g}]"
        )
        return cur.lastrowid


def update_zone_status(zone_id: int, status: str, signal_id: int = None):
    """Update zone status. Optionally link a signal_id when status='signal_generated'."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        if signal_id is not None:
            con.execute(
                "UPDATE sweep_zones SET status=?, signal_id=?, updated_at=? WHERE id=?",
                (status, signal_id, now, zone_id),
            )
        else:
            con.execute(
                "UPDATE sweep_zones SET status=?, updated_at=? WHERE id=?",
                (status, now, zone_id),
            )
    logger.info(f"[ZONE] #{zone_id} → {status}" + (f" (signal #{signal_id})" if signal_id else ""))


def get_active_zones() -> list[dict]:
    """Return all zones with status accepted or waiting_retest, newest first."""
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM sweep_zones
            WHERE status IN ('accepted', 'waiting_retest')
            ORDER BY detected_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_all_zones(limit: int = 200) -> list[dict]:
    """Return all zones including closed ones, newest first."""
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM sweep_zones
            ORDER BY detected_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def expire_old_zones():
    """
    Tiered zone expiry:
      accepted       → expire after ZONE_EXPIRE_ACCEPTED_HOURS (24h default)
      waiting_retest → expire after ZONE_EXPIRE_HOURS (48h default)
    """
    now               = datetime.now(timezone.utc)
    now_s             = now.isoformat()
    cutoff_accepted   = (now - timedelta(hours=ZONE_EXPIRE_ACCEPTED_HOURS)).isoformat()
    cutoff_retest     = (now - timedelta(hours=ZONE_EXPIRE_HOURS)).isoformat()
    with _conn() as con:
        c1 = con.execute("""
            UPDATE sweep_zones SET status = 'expired', updated_at = ?
            WHERE status = 'accepted' AND detected_at < ?
        """, (now_s, cutoff_accepted))
        c2 = con.execute("""
            UPDATE sweep_zones SET status = 'expired', updated_at = ?
            WHERE status = 'waiting_retest' AND detected_at < ?
        """, (now_s, cutoff_retest))
    total = c1.rowcount + c2.rowcount
    if total:
        logger.info(f"[ZONE] Expired {total} zone(s) ({c1.rowcount} accepted, {c2.rowcount} waiting_retest)")
