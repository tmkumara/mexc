"""
SQLite database for signal tracking and armed setups.
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
        # ── signals table ──────────────────────────────────────────
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
            CREATE INDEX IF NOT EXISTS idx_signals_generated_at
            ON signals (generated_at)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_symbol_status
            ON signals (symbol, status)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_direction_generated
            ON signals (direction, generated_at)
        """)

        for col, definition in [
            ("placed",    "INTEGER NOT NULL DEFAULT 1"),
            ("placed_at", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass

        # ── armed_setups table ─────────────────────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS armed_setups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'armed',
                trigger_price   REAL    NOT NULL,
                entry_low       REAL    NOT NULL,
                entry_high      REAL    NOT NULL,
                sl_price        REAL    NOT NULL,
                tp_price        REAL    NOT NULL,
                rr              REAL    NOT NULL,
                score           REAL    NOT NULL,
                setup_reason    TEXT,
                trend_summary   TEXT,
                created_at      TEXT    NOT NULL,
                expires_at      TEXT    NOT NULL,
                fired_signal_id INTEGER,
                fired_at        TEXT,
                updated_at      TEXT,
                miss_reason     TEXT
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_armed_setups_status
            ON armed_setups (status)
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_armed_setups_symbol_status
            ON armed_setups (symbol, status)
        """)

    logger.info("Database initialised")


# ── signals table ─────────────────────────────────────────────────

def save_signal(
    symbol: str,
    direction: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    leverage: int,
    generated_at: datetime,
) -> int:
    ts = generated_at.isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO signals
              (symbol, direction, entry_price, tp_price, sl_price,
               leverage, status, placed, generated_at, placed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
        """, (symbol, direction, entry_price, tp_price, sl_price, leverage, ts, ts))
        return cur.lastrowid


def update_signal_outcome(signal_id: int, status: str, pnl_roi: float):
    closed_at = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE signals
            SET status = ?, pnl_roi = ?, closed_at = ?
            WHERE id = ? AND status = 'pending'
        """, (status, pnl_roi, closed_at, signal_id))


def count_active_signals() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM signals WHERE status = 'pending'").fetchone()
        return row[0]


def get_pending_signals() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM signals WHERE status = 'pending' ORDER BY generated_at ASC
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
        rows = con.execute("SELECT * FROM signals ORDER BY generated_at ASC").fetchall()
        return [dict(r) for r in rows]


def count_signals_since(start: datetime) -> int:
    with _conn() as con:
        row = con.execute("""
            SELECT COUNT(*) AS cnt FROM signals WHERE generated_at >= ?
        """, (start.isoformat(),)).fetchone()
        return int(row["cnt"] or 0)


def count_signals_since_by_direction(start: datetime, direction: str) -> int:
    with _conn() as con:
        row = con.execute("""
            SELECT COUNT(*) AS cnt FROM signals
            WHERE generated_at >= ? AND direction = ?
        """, (start.isoformat(), direction)).fetchone()
        return int(row["cnt"] or 0)


def latest_signal_time() -> datetime | None:
    with _conn() as con:
        row = con.execute("""
            SELECT generated_at FROM signals ORDER BY generated_at DESC LIMIT 1
        """).fetchone()
        if not row:
            return None
        dt = datetime.fromisoformat(row["generated_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt


def signal_exists_for_coin(symbol: str, since: datetime) -> bool:
    with _conn() as con:
        row = con.execute("""
            SELECT id FROM signals
            WHERE symbol = ? AND generated_at >= ?
            LIMIT 1
        """, (symbol, since.isoformat())).fetchone()
        return row is not None


def count_losses_since(symbol: str, direction: str | None, since: datetime) -> int:
    with _conn() as con:
        if direction:
            row = con.execute("""
                SELECT COUNT(*) AS cnt FROM signals
                WHERE symbol = ? AND direction = ? AND status = 'loss' AND generated_at >= ?
            """, (symbol, direction, since.isoformat())).fetchone()
        else:
            row = con.execute("""
                SELECT COUNT(*) AS cnt FROM signals
                WHERE symbol = ? AND status = 'loss' AND generated_at >= ?
            """, (symbol, since.isoformat())).fetchone()
        return int(row["cnt"] or 0)


# ── armed_setups table ────────────────────────────────────────────

def save_armed_setup(setup: dict) -> int | None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO armed_setups (
                symbol, direction, status,
                trigger_price, entry_low, entry_high,
                sl_price, tp_price, rr, score,
                setup_reason, trend_summary,
                created_at, expires_at, updated_at
            ) VALUES (?, ?, 'armed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            setup["symbol"],
            setup["direction"],
            setup["trigger_price"],
            setup["entry_low"],
            setup["entry_high"],
            setup["sl_price"],
            setup["tp_price"],
            setup["rr"],
            setup["score"],
            setup.get("setup_reason", ""),
            setup.get("trend_summary", ""),
            now,
            setup["expires_at"],
            now,
        ))
        return cur.lastrowid


def get_armed_setups(limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM armed_setups
            WHERE status = 'armed'
            ORDER BY score DESC, created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_armed_setup_by_symbol(symbol: str) -> dict | None:
    with _conn() as con:
        row = con.execute("""
            SELECT * FROM armed_setups
            WHERE symbol = ? AND status = 'armed'
            ORDER BY score DESC LIMIT 1
        """, (symbol,)).fetchone()
        return dict(row) if row else None


def armed_setup_exists(symbol: str) -> bool:
    with _conn() as con:
        row = con.execute("""
            SELECT id FROM armed_setups WHERE symbol = ? AND status = 'armed' LIMIT 1
        """, (symbol,)).fetchone()
        return row is not None


def mark_armed_setup_fired(setup_id: int, signal_id: int):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE armed_setups
            SET status = 'fired', fired_signal_id = ?, fired_at = ?, updated_at = ?
            WHERE id = ? AND status = 'armed'
        """, (signal_id, now, now, setup_id))


def mark_armed_setup_missed(setup_id: int, reason: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE armed_setups
            SET status = 'missed', miss_reason = ?, updated_at = ?
            WHERE id = ? AND status = 'armed'
        """, (reason, now, setup_id))


def mark_armed_setup_expired(setup_id: int):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE armed_setups
            SET status = 'expired', updated_at = ?
            WHERE id = ? AND status = 'armed'
        """, (now, setup_id))


def mark_armed_setup_invalidated(setup_id: int, reason: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE armed_setups
            SET status = 'invalidated', miss_reason = ?, updated_at = ?
            WHERE id = ? AND status = 'armed'
        """, (reason, now, setup_id))


def expire_old_armed_setups(now: datetime):
    with _conn() as con:
        con.execute("""
            UPDATE armed_setups
            SET status = 'expired', updated_at = ?
            WHERE status = 'armed' AND expires_at <= ?
        """, (now.isoformat(), now.isoformat()))


def count_armed_setups() -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM armed_setups WHERE status = 'armed'"
        ).fetchone()
        return row[0]
