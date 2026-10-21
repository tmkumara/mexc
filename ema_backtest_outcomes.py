"""Deterministic offline outcome simulation for EMA replay results.

The live outcome evaluator treats ``signal.confirmation_close_at`` as the
first eligible candle open.  A replay evaluation occurs 90 seconds after the
confirmation candle closes, so the candle opening exactly at that close is
still ambiguous: its first 90 seconds overlap the settlement boundary.  This
module therefore excludes that candle and invokes the evaluator with an
internal signal whose confirmation boundary is advanced by one 15-minute
candle.  The original signal, timestamps, and setup key remain unchanged in
the returned records.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

import pandas as pd

from ema_candles import validate_candles
from ema_dataset_loader import EmaHistoricalDataset
from ema_expiry import calculate_expiry_at
from ema_outcome import OutcomeResult, evaluate_outcome
from ema_replay import EmaReplayResult
from ema_signal import EmaSignal


_UTC = timezone.utc
_TIMEFRAME = "15m"
_CANDLE_DURATION = timedelta(minutes=15)
_SETTLEMENT_SECONDS = 90
_STATUSES = ("WIN", "LOSS", "TIMEOUT", "PENDING")


@dataclass(frozen=True)
class EmaBacktestOutcome:
    """Outcome for one replay-accepted signal."""

    setup_key: str
    evaluation_timestamp: datetime
    outcome: OutcomeResult


@dataclass(frozen=True)
class EmaBacktestOutcomes:
    """Immutable aggregate returned by :func:`simulate_replay_outcomes`."""

    symbol: str
    total_accepted_signals: int
    outcomes: tuple[EmaBacktestOutcome, ...]
    pending_outcomes: tuple[EmaBacktestOutcome, ...]
    completed_outcomes: tuple[EmaBacktestOutcome, ...]
    counts: Mapping[str, int]


def _utc_datetime(value: object, field_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be UTC-aware")
    return value.astimezone(_UTC)


def _pending(reason: str) -> OutcomeResult:
    return OutcomeResult("PENDING", None, None, None, reason)


def _simulate_replay_outcomes(
    dataset: EmaHistoricalDataset,
    replay_result: EmaReplayResult,
    expiry_at_for_signal: Callable[[EmaSignal], datetime],
) -> EmaBacktestOutcomes:
    """Evaluate all accepted replay signals against one immutable dataset.

    Signals are processed in evaluation order.  Only 15m candles opening
    strictly after the original confirmation close are passed to the outcome
    evaluator, preventing the confirmation/settlement-overlap candle from
    producing a result.  No persistence, network access, or system clock is
    used.
    """
    if not isinstance(dataset, EmaHistoricalDataset):
        raise ValueError("dataset must be an EmaHistoricalDataset")
    if not isinstance(replay_result, EmaReplayResult):
        raise ValueError("replay_result must be an EmaReplayResult")
    if dataset.symbol != replay_result.symbol:
        raise ValueError("dataset and replay_result symbol must match")

    candles = validate_candles(dataset.candles_15m.copy(deep=True), _TIMEFRAME)
    entries = sorted(
        replay_result.accepted_signals,
        key=lambda item: _utc_datetime(item.evaluation_timestamp, "evaluation_timestamp"),
    )
    seen_keys: set[str] = set()
    for item in entries:
        signal = item.signal
        if signal.symbol != dataset.symbol:
            raise ValueError("signal symbol does not match dataset symbol")
        if not signal.setup_key:
            raise ValueError("signal setup_key is required")
        if signal.setup_key in seen_keys:
            raise ValueError("duplicate setup_key")
        seen_keys.add(signal.setup_key)
        _utc_datetime(signal.generated_at, "signal.generated_at")

    outcomes: list[EmaBacktestOutcome] = []
    counts = Counter({status: 0 for status in _STATUSES})
    last_available_at = None
    if not candles.empty:
        last_available_at = candles.index[-1].to_pydatetime() + _CANDLE_DURATION + timedelta(seconds=_SETTLEMENT_SECONDS)

    for item in entries:
        signal = item.signal
        evaluation_timestamp = _utc_datetime(item.evaluation_timestamp, "evaluation_timestamp")
        confirmation_close = _utc_datetime(signal.confirmation_close_at, "confirmation_close_at")
        expiry_at = _utc_datetime(
            expiry_at_for_signal(signal),
            "expiry_at",
        )

        if last_available_at is None or last_available_at < _utc_datetime(signal.generated_at, "signal.generated_at"):
            outcome = _pending("insufficient_history")
        else:
            available = candles.loc[candles.index > pd.Timestamp(confirmation_close)].copy()
            evaluator_signal = replace(
                signal,
                confirmation_candle_open=signal.confirmation_candle_open + _CANDLE_DURATION,
                confirmation_close_at=confirmation_close + _CANDLE_DURATION,
            )
            if expiry_at <= evaluator_signal.confirmation_close_at:
                outcome = _pending("insufficient_history")
            else:
                outcome = evaluate_outcome(
                    evaluator_signal,
                    available,
                    last_available_at,
                    expiry_at,
                )

        if outcome.status not in counts:
            raise ValueError(f"unexpected outcome status: {outcome.status}")
        counts[outcome.status] += 1
        outcomes.append(EmaBacktestOutcome(signal.setup_key, evaluation_timestamp, outcome))

    immutable_outcomes = tuple(outcomes)
    pending = tuple(item for item in immutable_outcomes if item.outcome.status == "PENDING")
    completed = tuple(item for item in immutable_outcomes if item.outcome.status != "PENDING")
    return EmaBacktestOutcomes(
        symbol=dataset.symbol,
        total_accepted_signals=len(immutable_outcomes),
        outcomes=immutable_outcomes,
        pending_outcomes=pending,
        completed_outcomes=completed,
        counts=dict(counts),
    )


def simulate_replay_outcomes(
    dataset: EmaHistoricalDataset,
    replay_result: EmaReplayResult,
    expiry_duration: timedelta,
) -> EmaBacktestOutcomes:
    """Evaluate with the legacy generic confirmation-close duration contract."""
    if not isinstance(expiry_duration, timedelta) or expiry_duration <= timedelta(0):
        raise ValueError("expiry_duration must be a positive timedelta")
    return _simulate_replay_outcomes(
        dataset,
        replay_result,
        lambda signal: signal.confirmation_close_at + expiry_duration,
    )


def simulate_ema_outcomes(
    dataset: EmaHistoricalDataset,
    replay_result: EmaReplayResult,
) -> EmaBacktestOutcomes:
    """Evaluate with the approved availability-based four-hour expiry."""
    return _simulate_replay_outcomes(dataset, replay_result, lambda signal: calculate_expiry_at(signal.confirmation_close_at))
