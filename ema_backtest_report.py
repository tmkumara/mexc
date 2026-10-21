"""Deterministic performance aggregation for offline EMA replay outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from statistics import median
from typing import Any

from ema_backtest_outcomes import EmaBacktestOutcomes
from ema_replay import EmaReplayResult


_STATUSES = ("WIN", "LOSS", "TIMEOUT", "PENDING")
_TERMINAL_STATUSES = {"WIN", "LOSS", "TIMEOUT"}


@dataclass(frozen=True)
class EmaPerformanceReport:
    """Immutable aggregate of one replay and its simulated outcomes."""

    symbol: str
    total_evaluations: int
    accepted_signals: int
    rejected_evaluations: int
    duplicate_setups: int
    win_count: int
    loss_count: int
    timeout_count: int
    pending_count: int
    resolved_trade_count: int
    win_rate: float | None
    win_rate_denominator: int
    loss_rate: float | None
    loss_rate_denominator: int
    average_gross_roi_pct: float | None
    median_gross_roi_pct: float | None
    total_gross_roi_pct: float | None
    average_reward_to_risk: float | None
    best_trade_gross_roi_pct: float | None
    worst_trade_gross_roi_pct: float | None
    maximum_consecutive_wins: int
    maximum_consecutive_losses: int

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible scalar report values."""
        return {
            "symbol": self.symbol,
            "total_evaluations": self.total_evaluations,
            "accepted_signals": self.accepted_signals,
            "rejected_evaluations": self.rejected_evaluations,
            "duplicate_setups": self.duplicate_setups,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "timeout_count": self.timeout_count,
            "pending_count": self.pending_count,
            "resolved_trade_count": self.resolved_trade_count,
            "win_rate": self.win_rate,
            "win_rate_denominator": self.win_rate_denominator,
            "loss_rate": self.loss_rate,
            "loss_rate_denominator": self.loss_rate_denominator,
            "average_gross_roi_pct": self.average_gross_roi_pct,
            "median_gross_roi_pct": self.median_gross_roi_pct,
            "total_gross_roi_pct": self.total_gross_roi_pct,
            "average_reward_to_risk": self.average_reward_to_risk,
            "best_trade_gross_roi_pct": self.best_trade_gross_roi_pct,
            "worst_trade_gross_roi_pct": self.worst_trade_gross_roi_pct,
            "maximum_consecutive_wins": self.maximum_consecutive_wins,
            "maximum_consecutive_losses": self.maximum_consecutive_losses,
        }


def _utc(value: object, field_name: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _finite_roi(value: object, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _streaks(statuses: list[str]) -> tuple[int, int]:
    maximum_wins = maximum_losses = current_wins = current_losses = 0
    for status in statuses:
        if status == "WIN":
            current_wins += 1
            current_losses = 0
            maximum_wins = max(maximum_wins, current_wins)
        elif status == "LOSS":
            current_losses += 1
            current_wins = 0
            maximum_losses = max(maximum_losses, current_losses)
        else:
            current_wins = current_losses = 0
    return maximum_wins, maximum_losses


def build_performance_report(
    replay_result: EmaReplayResult,
    backtest_outcomes: EmaBacktestOutcomes,
) -> EmaPerformanceReport:
    """Aggregate one replay result and its simulated outcomes.

    ROI statistics use only terminal records with a realized finite gross ROI,
    including TIMEOUT when its evaluator supplied one.  The total ROI is an
    arithmetic sum of trade ROI percentages; it is not portfolio return.
    """
    if not isinstance(replay_result, EmaReplayResult):
        raise ValueError("replay_result must be an EmaReplayResult")
    if not isinstance(backtest_outcomes, EmaBacktestOutcomes):
        raise ValueError("backtest_outcomes must be an EmaBacktestOutcomes")
    if replay_result.symbol != backtest_outcomes.symbol:
        raise ValueError("replay and outcome symbol must match")
    if replay_result.total_evaluations < 0 or replay_result.rejected_evaluation_count < 0 or replay_result.duplicate_count < 0:
        raise ValueError("replay counts must be non-negative")

    accepted = tuple(replay_result.accepted_signals)
    records = tuple(backtest_outcomes.outcomes)
    record_keys = [record.setup_key for record in records]
    if len(record_keys) != len(set(record_keys)):
        raise ValueError("duplicate setup_key in outcomes")
    if backtest_outcomes.total_accepted_signals != len(records) or len(accepted) != len(records):
        raise ValueError("replay and outcome counts do not match")

    signals_by_key = {}
    for item in accepted:
        if item.signal.setup_key in signals_by_key:
            raise ValueError("duplicate setup_key in replay result")
        signals_by_key[item.signal.setup_key] = item.signal

    seen_keys: set[str] = set()
    ordered = []
    for record in records:
        if record.setup_key not in signals_by_key:
            raise ValueError("outcome setup_key is not present in replay result")
        seen_keys.add(record.setup_key)
        ordered.append((
            _utc(record.evaluation_timestamp, "outcome evaluation_timestamp"),
            record,
            signals_by_key[record.setup_key],
        ))
    ordered.sort(key=lambda item: item[0])

    counts = {status: 0 for status in _STATUSES}
    realized_rois: list[float] = []
    realized_rrs: list[float] = []
    status_sequence: list[str] = []
    for _timestamp, record, signal in ordered:
        status = record.outcome.status
        if status not in counts:
            raise ValueError(f"unsupported outcome status: {status}")
        counts[status] += 1
        status_sequence.append(status)
        if status in _TERMINAL_STATUSES and record.outcome.gross_roi_pct is not None:
            realized_rois.append(_finite_roi(record.outcome.gross_roi_pct, "gross_roi_pct"))
            realized_rrs.append(_finite_roi(signal.rr, "signal rr"))

    expected_counts = dict(backtest_outcomes.counts)
    if any(expected_counts.get(status) != counts[status] for status in _STATUSES):
        raise ValueError("outcome counts do not match outcome records")

    win_loss_denominator = counts["WIN"] + counts["LOSS"]
    averages = None if not realized_rois else sum(realized_rois) / len(realized_rois)
    median_roi = None if not realized_rois else float(median(realized_rois))
    total_roi = None if not realized_rois else sum(realized_rois)
    average_rr = None if not realized_rrs else sum(realized_rrs) / len(realized_rrs)
    maximum_wins, maximum_losses = _streaks(status_sequence)

    return EmaPerformanceReport(
        symbol=backtest_outcomes.symbol,
        total_evaluations=replay_result.total_evaluations,
        accepted_signals=len(accepted),
        rejected_evaluations=replay_result.rejected_evaluation_count,
        duplicate_setups=replay_result.duplicate_count,
        win_count=counts["WIN"],
        loss_count=counts["LOSS"],
        timeout_count=counts["TIMEOUT"],
        pending_count=counts["PENDING"],
        resolved_trade_count=counts["WIN"] + counts["LOSS"] + counts["TIMEOUT"],
        win_rate=None if not win_loss_denominator else counts["WIN"] / win_loss_denominator,
        win_rate_denominator=win_loss_denominator,
        loss_rate=None if not win_loss_denominator else counts["LOSS"] / win_loss_denominator,
        loss_rate_denominator=win_loss_denominator,
        average_gross_roi_pct=averages,
        median_gross_roi_pct=median_roi,
        total_gross_roi_pct=total_roi,
        average_reward_to_risk=average_rr,
        best_trade_gross_roi_pct=None if not realized_rois else max(realized_rois),
        worst_trade_gross_roi_pct=None if not realized_rois else min(realized_rois),
        maximum_consecutive_wins=maximum_wins,
        maximum_consecutive_losses=maximum_losses,
    )
