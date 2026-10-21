"""Lifecycle coordination for the standalone EMA outcome repository."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

import pandas as pd

from ema_outcome import evaluate_outcome
from ema_repository import EmaRepository, EmaSignalRecord


@dataclass(frozen=True)
class ProcessingSummary:
    total_pending: int
    evaluated: int
    wins: int
    losses: int
    timeouts: int
    still_pending: int
    missing_market_data: int
    persistence_conflicts: int
    errors: int
    pending_remaining: int


def _pending_snapshot(repository: EmaRepository) -> list[EmaSignalRecord]:
    records: list[EmaSignalRecord] = []
    offset = 0
    page_size = 100
    while True:
        page = repository.list_pending_signals(limit=page_size, offset=offset)
        records.extend(page)
        if len(page) < page_size:
            return records
        offset += len(page)


def process_pending_outcomes(
    repository: EmaRepository,
    candles_by_symbol: Mapping[str, pd.DataFrame],
    evaluation_timestamp: datetime,
    expiry_duration: timedelta,
) -> ProcessingSummary:
    """Evaluate and persist outcomes for a deterministic pending snapshot."""
    if not isinstance(repository, EmaRepository):
        raise ValueError("repository must be an EmaRepository")
    if not isinstance(candles_by_symbol, Mapping):
        raise ValueError("candles_by_symbol must be a mapping")
    if not isinstance(expiry_duration, timedelta) or expiry_duration <= timedelta(0):
        raise ValueError("expiry_duration must be positive")

    pending = _pending_snapshot(repository)
    evaluated = wins = losses = timeouts = still_pending = 0
    missing_market_data = persistence_conflicts = errors = 0

    for record in pending:
        candles = candles_by_symbol.get(record.signal.symbol)
        if candles is None:
            missing_market_data += 1
            continue

        try:
            outcome = evaluate_outcome(
                record.signal,
                candles,
                evaluation_timestamp,
                record.signal.confirmation_close_at + expiry_duration,
            )
            evaluated += 1
            if outcome.status == "PENDING":
                still_pending += 1
                continue

            save_result = repository.save_outcome(record.signal.setup_key, outcome)
            if save_result.reason == "terminal_conflict":
                persistence_conflicts += 1
            elif save_result.reason in {"updated", "already_terminal_same"}:
                if outcome.status == "WIN":
                    wins += 1
                elif outcome.status == "LOSS":
                    losses += 1
                elif outcome.status == "TIMEOUT":
                    timeouts += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    pending_remaining = len(_pending_snapshot(repository))
    return ProcessingSummary(
        total_pending=len(pending),
        evaluated=evaluated,
        wins=wins,
        losses=losses,
        timeouts=timeouts,
        still_pending=still_pending,
        missing_market_data=missing_market_data,
        persistence_conflicts=persistence_conflicts,
        errors=errors,
        pending_remaining=pending_remaining,
    )
