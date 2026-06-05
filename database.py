"""
SQLite database for signal tracking, reporting, and stateful SMC pending setups.
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

        for col, definition in [
            ("placed",    "INTEGER NOT NULL DEFAULT 1"),
            ("placed_at", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass

        con.execute("""
            CREATE TABLE IF NOT EXISTS pending_setups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'waiting',

                trend_tf        TEXT    NOT NULL,
                entry_tf        TEXT    NOT NULL,

                bias            TEXT    NOT NULL,
                bias_break      REAL,

                sweep_type      TEXT    NOT NULL,
                sweep_level     REAL    NOT NULL,
                sweep_extreme   REAL    NOT NULL,
                sweep_time      TEXT    NOT NULL,

                ob_type         TEXT    NOT NULL,
                ob_low          REAL    NOT NULL,
                ob_high         REAL    NOT NULL,
                ob_time         TEXT    NOT NULL,

                target_price    REAL    NOT NULL,
                sl_price        REAL    NOT NULL,

                rr_estimate     REAL    NOT NULL,
                score           REAL    NOT NULL,

                setup_time      TEXT    NOT NULL,
                expires_at      TEXT    NOT NULL,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT,

                fired_signal_id INTEGER,
                fired_at        TEXT
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_setups_status
            ON pending_setups (status)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_setups_symbol_status
            ON pending_setups (symbol, status)
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
        """, (
            symbol,
            direction,
            entry_price,
            tp_price,
            sl_price,
            leverage,
            ts,
            ts,
        ))

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
        row = con.execute(
            "SELECT COUNT(*) FROM signals WHERE status = 'pending'"
        ).fetchone()

        return row[0]


def get_pending_signals() -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM signals
            WHERE status = 'pending'
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
        rows = con.execute("""
            SELECT * FROM signals
            ORDER BY generated_at ASC
        """).fetchall()

        return [dict(r) for r in rows]


def signal_exists_for_coin(symbol: str, since: datetime) -> bool:
    with _conn() as con:
        row = con.execute("""
            SELECT id FROM signals
            WHERE symbol = ?
              AND status = 'pending'
              AND generated_at >= ?
            LIMIT 1
        """, (symbol, since.isoformat())).fetchone()

        return row is not None


# ── pending_setups table ──────────────────────────────────────────

def pending_setup_exists(symbol: str, direction: str | None = None) -> bool:
    with _conn() as con:
        if direction:
            row = con.execute("""
                SELECT id FROM pending_setups
                WHERE symbol = ?
                  AND direction = ?
                  AND status = 'waiting'
                LIMIT 1
            """, (symbol, direction)).fetchone()
        else:
            row = con.execute("""
                SELECT id FROM pending_setups
                WHERE symbol = ?
                  AND status = 'waiting'
                LIMIT 1
            """, (symbol,)).fetchone()

        return row is not None


def save_pending_setup(setup: dict) -> int | None:
    """
    Saves a pending SMC setup.

    Avoids duplicates for same symbol + direction while status='waiting'.
    """
    if pending_setup_exists(setup["symbol"], setup["direction"]):
        return None

    now = datetime.now(timezone.utc).isoformat()

    with _conn() as con:
        cur = con.execute("""
            INSERT INTO pending_setups (
                symbol, direction, status,
                trend_tf, entry_tf,
                bias, bias_break,
                sweep_type, sweep_level, sweep_extreme, sweep_time,
                ob_type, ob_low, ob_high, ob_time,
                target_price, sl_price,
                rr_estimate, score,
                setup_time, expires_at, created_at, updated_at
            )
            VALUES (
                ?, ?, 'waiting',
                ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?
            )
        """, (
            setup["symbol"],
            setup["direction"],
            setup["trend_tf"],
            setup["entry_tf"],
            setup["bias"],
            setup.get("bias_break"),
            setup["sweep_type"],
            setup["sweep_level"],
            setup["sweep_extreme"],
            setup["sweep_time"],
            setup["ob_type"],
            setup["ob_low"],
            setup["ob_high"],
            setup["ob_time"],
            setup["target_price"],
            setup["sl_price"],
            setup["rr_estimate"],
            setup["score"],
            setup["setup_time"],
            setup["expires_at"],
            now,
            now,
        ))

        return cur.lastrowid


def get_waiting_setups(limit: int = 200) -> list[dict]:
    """
    Return waiting setups prioritized by quality and freshness.

    Previous behavior returned the oldest rows first. That kept monitoring stale,
    far-away setups and delayed better fresh setups. For the optimized strategy,
    we monitor highest-score + newest setups first.
    """
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM pending_setups
            WHERE status = 'waiting'
            ORDER BY score DESC, created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()

        return [dict(r) for r in rows]


def count_waiting_setups() -> int:
    with _conn() as con:
        row = con.execute("""
            SELECT COUNT(*) FROM pending_setups
            WHERE status = 'waiting'
        """).fetchone()

        return row[0]


def count_waiting_setups_by_direction() -> dict[str, int]:
    """Return waiting setup counts grouped by direction."""
    with _conn() as con:
        rows = con.execute("""
            SELECT direction, COUNT(*) AS cnt
            FROM pending_setups
            WHERE status = 'waiting'
            GROUP BY direction
        """).fetchall()

        return {str(r["direction"]): int(r["cnt"]) for r in rows}


def mark_setup_fired(setup_id: int, signal_id: int):
    now = datetime.now(timezone.utc).isoformat()

    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'fired',
                fired_signal_id = ?,
                fired_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'waiting'
        """, (signal_id, now, now, setup_id))


def mark_setup_expired(setup_id: int):
    now = datetime.now(timezone.utc).isoformat()

    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'expired',
                updated_at = ?
            WHERE id = ? AND status = 'waiting'
        """, (now, setup_id))


def mark_setup_invalidated(setup_id: int):
    now = datetime.now(timezone.utc).isoformat()

    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'invalidated',
                updated_at = ?
            WHERE id = ? AND status = 'waiting'
        """, (now, setup_id))


def expire_old_waiting_setups(now: datetime):
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'expired',
                updated_at = ?
            WHERE status = 'waiting'
              AND expires_at <= ?
        """, (now.isoformat(), now.isoformat()))