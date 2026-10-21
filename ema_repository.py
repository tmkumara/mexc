"""Fresh SQLite persistence for standalone EMA signals."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from ema_outcome import OutcomeResult
from ema_signal import EmaSignal


PathLike = Union[str, Path]
_COLUMNS = (
    "id",
    "strategy_id",
    "strategy_version",
    "setup_key",
    "symbol",
    "direction",
    "entry_price",
    "tp_price",
    "sl_price",
    "leverage",
    "tp_roi_pct",
    "sl_roi_pct",
    "rr",
    "confirmation_candle_open",
    "confirmation_close_at",
    "pivot_timestamp",
    "rsi_reset_timestamp",
    "generated_at",
    "created_at",
    "status",
    "closed_at",
    "exit_price",
    "gross_roi_pct",
    "outcome_reason",
    "updated_at",
)
_STATUSES = {"PENDING", "WIN", "LOSS", "TIMEOUT"}


@dataclass(frozen=True)
class SaveResult:
    inserted: bool
    signal_id: int
    setup_key: str


@dataclass(frozen=True)
class OutcomeSaveResult:
    updated: bool
    status: str | None
    setup_key: str
    reason: str


@dataclass(frozen=True)
class EmaSignalRecord:
    signal: EmaSignal
    status: str
    closed_at: datetime | None
    exit_price: float | None
    gross_roi_pct: float | None
    outcome_reason: str | None
    updated_at: datetime


def _timestamp_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("signal timestamps must be UTC-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is not UTC-aware")
    return parsed.astimezone(timezone.utc)


class EmaRepository:
    """Small repository for one standalone EMA SQLite database."""

    def __init__(self, database_path: PathLike):
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'signals'"
            ).fetchone()
            if existing:
                columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(signals)"))
                if columns != _COLUMNS:
                    raise RuntimeError("signals table is not the standalone EMA schema")
                return

            connection.execute(
                """
                CREATE TABLE signals (
                    id INTEGER PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    setup_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    tp_price REAL NOT NULL,
                    sl_price REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    tp_roi_pct REAL NOT NULL,
                    sl_roi_pct REAL NOT NULL,
                    rr REAL NOT NULL,
                    confirmation_candle_open TEXT NOT NULL,
                    confirmation_close_at TEXT NOT NULL,
                    pivot_timestamp TEXT NOT NULL,
                    rsi_reset_timestamp TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'WIN', 'LOSS', 'TIMEOUT')),
                    closed_at TEXT NULL,
                    exit_price REAL NULL,
                    gross_roi_pct REAL NULL,
                    outcome_reason TEXT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (status = 'PENDING' AND closed_at IS NULL AND exit_price IS NULL AND gross_roi_pct IS NULL AND outcome_reason IS NULL)
                        OR
                        (status IN ('WIN', 'LOSS', 'TIMEOUT') AND closed_at IS NOT NULL AND exit_price IS NOT NULL AND gross_roi_pct IS NOT NULL AND outcome_reason IS NOT NULL)
                    )
                )
                """
            )

    def save_signal(self, signal: EmaSignal) -> SaveResult:
        if not isinstance(signal, EmaSignal):
            raise ValueError("signal must be an EmaSignal")
        self.initialize()

        now = _timestamp_text(datetime.now(timezone.utc))
        values = (
            signal.strategy_id,
            signal.strategy_version,
            signal.setup_key,
            signal.symbol,
            signal.direction,
            signal.entry_price,
            signal.tp_price,
            signal.sl_price,
            signal.leverage,
            signal.tp_roi_pct,
            signal.sl_roi_pct,
            signal.rr,
            _timestamp_text(signal.confirmation_candle_open),
            _timestamp_text(signal.confirmation_close_at),
            _timestamp_text(signal.pivot_timestamp),
            _timestamp_text(signal.rsi_reset_timestamp),
            _timestamp_text(signal.generated_at),
            now,
            "PENDING",
            None,
            None,
            None,
            None,
            now,
        )
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO signals (
                        strategy_id, strategy_version, setup_key, symbol, direction,
                        entry_price, tp_price, sl_price, leverage, tp_roi_pct,
                        sl_roi_pct, rr, confirmation_candle_open, confirmation_close_at,
                        pivot_timestamp, rsi_reset_timestamp, generated_at, created_at,
                        status, closed_at, exit_price, gross_roi_pct, outcome_reason, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                return SaveResult(True, int(cursor.lastrowid), signal.setup_key)
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed: signals.setup_key" not in str(exc):
                    raise
                row = connection.execute(
                    "SELECT id FROM signals WHERE setup_key = ?",
                    (signal.setup_key,),
                ).fetchone()
                if row is None:
                    raise
                return SaveResult(False, int(row["id"]), signal.setup_key)

    def get_by_setup_key(self, setup_key: str) -> EmaSignal | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE setup_key = ?",
                (setup_key,),
            ).fetchone()
        return self._row_to_signal(row) if row else None

    def get_signal_record(self, setup_key: str) -> EmaSignalRecord | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM signals WHERE setup_key = ?",
                (setup_key,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_pending_signals(self, limit: int = 100, offset: int = 0) -> list[EmaSignalRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signals WHERE status = 'PENDING' ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def save_outcome(self, setup_key: str, outcome: OutcomeResult) -> OutcomeSaveResult:
        if not isinstance(outcome, OutcomeResult):
            raise ValueError("outcome must be an OutcomeResult")
        if outcome.status not in _STATUSES:
            raise ValueError("outcome status must be PENDING, WIN, LOSS, or TIMEOUT")
        if outcome.status == "PENDING":
            if any(value is not None for value in (outcome.closed_at, outcome.exit_price, outcome.gross_roi_pct)):
                raise ValueError("PENDING outcome cannot contain terminal values")
        else:
            if outcome.closed_at is None or outcome.exit_price is None or outcome.gross_roi_pct is None:
                raise ValueError("terminal outcome requires closed_at, exit_price, and gross_roi_pct")
            if not outcome.reason:
                raise ValueError("terminal outcome requires a reason")
            _timestamp_text(outcome.closed_at)
            for field_name, value in (("exit_price", outcome.exit_price), ("gross_roi_pct", outcome.gross_roi_pct)):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{field_name} must be numeric")

        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM signals WHERE setup_key = ?",
                (setup_key,),
            ).fetchone()
            if existing is None:
                return OutcomeSaveResult(False, None, setup_key, "not_found")
            if outcome.status == "PENDING":
                return OutcomeSaveResult(False, existing["status"], setup_key, "still_pending")

            updated_at = _timestamp_text(datetime.now(timezone.utc))
            cursor = connection.execute(
                """
                UPDATE signals
                SET status = ?, closed_at = ?, exit_price = ?, gross_roi_pct = ?,
                    outcome_reason = ?, updated_at = ?
                WHERE setup_key = ? AND status = 'PENDING'
                """,
                (
                    outcome.status,
                    _timestamp_text(outcome.closed_at),
                    float(outcome.exit_price),
                    float(outcome.gross_roi_pct),
                    outcome.reason,
                    updated_at,
                    setup_key,
                ),
            )
            if cursor.rowcount == 1:
                return OutcomeSaveResult(True, outcome.status, setup_key, "updated")

            current = connection.execute(
                "SELECT * FROM signals WHERE setup_key = ?",
                (setup_key,),
            ).fetchone()
            same_terminal = (
                current is not None
                and current["status"] == outcome.status
                and current["closed_at"] == _timestamp_text(outcome.closed_at)
                and current["exit_price"] == float(outcome.exit_price)
                and current["gross_roi_pct"] == float(outcome.gross_roi_pct)
                and current["outcome_reason"] == outcome.reason
            )
            reason = "already_terminal_same" if same_terminal else "terminal_conflict"
            return OutcomeSaveResult(False, current["status"], setup_key, reason)

    def list_signals(self, limit: int = 100) -> list[EmaSignal]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM signals ORDER BY created_at ASC, id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_signal(row) for row in rows]

    def setup_exists(self, setup_key: str) -> bool:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM signals WHERE setup_key = ? LIMIT 1",
                (setup_key,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _row_to_signal(row: sqlite3.Row) -> EmaSignal:
        return EmaSignal(
            symbol=row["symbol"],
            direction=row["direction"],
            entry_price=row["entry_price"],
            tp_price=row["tp_price"],
            sl_price=row["sl_price"],
            leverage=row["leverage"],
            tp_roi_pct=row["tp_roi_pct"],
            sl_roi_pct=row["sl_roi_pct"],
            rr=row["rr"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            setup_key=row["setup_key"],
            confirmation_candle_open=_parse_timestamp(row["confirmation_candle_open"]),
            confirmation_close_at=_parse_timestamp(row["confirmation_close_at"]),
            pivot_timestamp=_parse_timestamp(row["pivot_timestamp"]),
            rsi_reset_timestamp=_parse_timestamp(row["rsi_reset_timestamp"]),
            generated_at=_parse_timestamp(row["generated_at"]),
        )

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row) -> EmaSignalRecord:
        return EmaSignalRecord(
            signal=cls._row_to_signal(row),
            status=row["status"],
            closed_at=_parse_timestamp(row["closed_at"]) if row["closed_at"] else None,
            exit_price=row["exit_price"],
            gross_roi_pct=row["gross_roi_pct"],
            outcome_reason=row["outcome_reason"],
            updated_at=_parse_timestamp(row["updated_at"]),
        )
