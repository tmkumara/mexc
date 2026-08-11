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
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
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
            ("breakeven_triggered_at", "TEXT"),
            ("strategy_name", "TEXT"),
            ("score", "REAL"),
            ("rr", "REAL"),
            ("entry_timeframe", "TEXT"),
            ("trend_timeframe", "TEXT"),
            ("setup_reason", "TEXT"),
            # ── Super Scalper v3 fields (nullable -- only populated for
            # signals fired by strategy_name = "Super Scalper v3") ──────
            ("tp1_price", "REAL"),
            ("tp2_price", "REAL"),
            ("tp1_hit_at", "TEXT"),
            ("trend", "TEXT"),
            ("strength", "INTEGER"),
            ("ao", "REAL"),
            ("kc_pos", "REAL"),
            ("regime", "TEXT"),
            ("regime_votes", "INTEGER"),
            ("adx", "REAL"),
            ("chop", "REAL"),
            ("signal_message_id", "INTEGER"),
            ("tp3_price", "REAL"),
            ("tp2_hit_at", "TEXT"),
            ("position_size", "REAL"),
        ]:
            try:
                con.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
            except Exception:
                pass

        # ── pending_setups table ───────────────────────────────────
        con.execute("""
            CREATE TABLE IF NOT EXISTS pending_setups (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol             TEXT    NOT NULL,
                direction          TEXT    NOT NULL,
                status             TEXT    NOT NULL DEFAULT 'pending_pullback',

                macro_tf           TEXT    NOT NULL,
                trend_tf           TEXT    NOT NULL,
                pullback_tf        TEXT    NOT NULL,
                entry_tf           TEXT    NOT NULL,

                macro_trend        INTEGER NOT NULL,
                trend_state        INTEGER NOT NULL,

                zlema_1h           REAL    NOT NULL,
                zlema_15m          REAL    NOT NULL,

                pullback_price     REAL    NOT NULL,
                pullback_time      TEXT    NOT NULL,

                confirmation_high  REAL,
                confirmation_low   REAL,
                confirmation_close REAL,
                confirmation_time  TEXT,
                trigger_price      REAL,

                score              REAL    NOT NULL,

                setup_time         TEXT    NOT NULL,
                expires_at         TEXT    NOT NULL,
                created_at         TEXT    NOT NULL,

                fired_signal_id    INTEGER,
                fired_at           TEXT,
                updated_at         TEXT,
                miss_reason        TEXT
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
    strategy_name: str = "",
    score: float = 0.0,
    rr: float = 0.0,
    entry_timeframe: str = "",
    trend_timeframe: str = "",
    setup_reason: str = "",
    tp2_price: float | None = None,
    tp3_price: float | None = None,
    position_size: float | None = None,
) -> int:
    ts = generated_at.isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO signals
              (symbol, direction, entry_price, tp_price, sl_price,
               leverage, status, placed, generated_at, placed_at,
               strategy_name, score, rr, entry_timeframe, trend_timeframe, setup_reason,
               tp2_price, tp3_price, position_size)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol, direction, entry_price, tp_price, sl_price, leverage, ts, ts,
            strategy_name, score, rr, entry_timeframe, trend_timeframe, setup_reason,
            tp2_price, tp3_price, position_size,
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


def mark_signal_breakeven_triggered(signal_id: int, triggered_at: datetime) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE signals
            SET breakeven_triggered_at = ?
            WHERE id = ? AND breakeven_triggered_at IS NULL
        """, (triggered_at.isoformat(), signal_id))


def count_active_signals() -> int:
    with _conn() as con:
        row = con.execute("SELECT COUNT(*) FROM signals WHERE status = 'pending'").fetchone()
        return row[0]


def count_active_signals_by_direction(direction: str) -> int:
    with _conn() as con:
        row = con.execute("""
            SELECT COUNT(*)
            FROM signals
            WHERE status = 'pending'
              AND direction = ?
        """, (direction,)).fetchone()
        return int(row[0])


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


# ── pending_setups table ─────────────────────────────────────────

def save_pending_setup(setup: dict) -> int | None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        cur = con.execute("""
            INSERT INTO pending_setups (
                symbol, direction, status,
                macro_tf, trend_tf, pullback_tf, entry_tf,
                macro_trend, trend_state,
                zlema_1h, zlema_15m,
                pullback_price, pullback_time,
                score, setup_time, expires_at, created_at, updated_at
            ) VALUES (?, ?, 'pending_pullback', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            setup["symbol"], setup["direction"],
            setup["macro_tf"], setup["trend_tf"], setup["pullback_tf"], setup["entry_tf"],
            setup["macro_trend"], setup["trend_state"],
            setup["zlema_1h"], setup["zlema_15m"],
            setup["pullback_price"], setup["pullback_time"],
            setup["score"], setup["setup_time"], setup["expires_at"], setup["created_at"], now,
        ))
        return cur.lastrowid


def get_pending_setups(status: str, limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute("""
            SELECT * FROM pending_setups
            WHERE status = ?
            ORDER BY score DESC, created_at DESC
            LIMIT ?
        """, (status, limit)).fetchall()
        return [dict(r) for r in rows]


def get_pending_setup_by_symbol(symbol: str) -> dict | None:
    with _conn() as con:
        row = con.execute("""
            SELECT * FROM pending_setups
            WHERE symbol = ? AND status IN ('pending_pullback', 'pending_breakout')
            ORDER BY created_at DESC LIMIT 1
        """, (symbol,)).fetchone()
        return dict(row) if row else None


def pending_setup_exists(symbol: str) -> bool:
    with _conn() as con:
        row = con.execute("""
            SELECT id FROM pending_setups
            WHERE symbol = ? AND status IN ('pending_pullback', 'pending_breakout')
            LIMIT 1
        """, (symbol,)).fetchone()
        return row is not None


def update_pending_setup_breakout(
    setup_id: int, confirmation_high: float, confirmation_low: float,
    confirmation_close: float, confirmation_time: str, trigger_price: float,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'pending_breakout',
                confirmation_high = ?, confirmation_low = ?, confirmation_close = ?,
                confirmation_time = ?, trigger_price = ?, updated_at = ?
            WHERE id = ? AND status = 'pending_pullback'
        """, (confirmation_high, confirmation_low, confirmation_close, confirmation_time, trigger_price, now, setup_id))


def mark_pending_setup_fired(setup_id: int, signal_id: int, final_score: float) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'fired', fired_signal_id = ?, fired_at = ?, score = ?, updated_at = ?
            WHERE id = ?
        """, (signal_id, now, final_score, now, setup_id))


def mark_pending_setup_missed(setup_id: int, reason: str = "", final_score: float | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'missed', miss_reason = ?, score = COALESCE(?, score), updated_at = ?
            WHERE id = ?
        """, (reason, final_score, now, setup_id))


def mark_pending_setup_expired(setup_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'expired', updated_at = ?
            WHERE id = ?
        """, (now, setup_id))


def expire_old_pending_setups(now: datetime) -> None:
    with _conn() as con:
        con.execute("""
            UPDATE pending_setups
            SET status = 'expired', updated_at = ?
            WHERE status IN ('pending_pullback', 'pending_breakout') AND expires_at <= ?
        """, (now.isoformat(), now.isoformat()))


def count_pending_setups() -> int:
    with _conn() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM pending_setups WHERE status IN ('pending_pullback', 'pending_breakout')"
        ).fetchone()
        return row[0]
