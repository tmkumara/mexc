"""Net-performance aggregation for offline EMA backtests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from statistics import median
from typing import Any

from ema_backtest_outcomes import EmaBacktestOutcome, EmaBacktestOutcomes
from ema_backtest_report import EmaPerformanceReport, build_performance_report
from ema_replay import EmaReplayResult
from ema_trading_costs import EmaNetOutcome, EmaTradingCosts, calculate_net_outcome


@dataclass(frozen=True)
class EmaNetPerformanceReport:
    gross_report: EmaPerformanceReport
    net_outcomes: tuple[EmaNetOutcome, ...]
    average_net_roi_pct: float | None
    median_net_roi_pct: float | None
    total_net_roi_pct: float | None
    best_net_roi_pct: float | None
    worst_net_roi_pct: float | None
    net_positive_trade_count: int
    net_negative_trade_count: int
    net_flat_trade_count: int
    net_positive_rate: float | None
    net_positive_rate_denominator: int
    total_modeled_fees_pct: float
    total_modeled_slippage_pct: float
    funding_excluded: bool

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible gross and net report values."""
        return {
            "gross_performance": self.gross_report.to_dict(),
            "average_net_roi_pct": self.average_net_roi_pct,
            "median_net_roi_pct": self.median_net_roi_pct,
            "total_net_roi_pct": self.total_net_roi_pct,
            "best_net_roi_pct": self.best_net_roi_pct,
            "worst_net_roi_pct": self.worst_net_roi_pct,
            "net_positive_trade_count": self.net_positive_trade_count,
            "net_negative_trade_count": self.net_negative_trade_count,
            "net_flat_trade_count": self.net_flat_trade_count,
            "net_positive_rate": self.net_positive_rate,
            "net_positive_rate_denominator": self.net_positive_rate_denominator,
            "total_modeled_fees_pct": self.total_modeled_fees_pct,
            "total_modeled_slippage_pct": self.total_modeled_slippage_pct,
            "funding_excluded": self.funding_excluded,
        }


def _utc(value: object) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("outcome evaluation_timestamp must be UTC-aware")
    return value.astimezone(timezone.utc)


def build_net_performance_report(
    replay_result: EmaReplayResult,
    backtest_outcomes: EmaBacktestOutcomes,
    costs: EmaTradingCosts,
) -> EmaNetPerformanceReport:
    """Build gross and explicitly cost-adjusted offline performance metrics."""
    if not isinstance(costs, EmaTradingCosts):
        raise ValueError("costs must be an EmaTradingCosts")
    gross_report = build_performance_report(replay_result, backtest_outcomes)
    signals_by_key = {item.signal.setup_key: item.signal for item in replay_result.accepted_signals}
    records = sorted(
        backtest_outcomes.outcomes,
        key=lambda record: _utc(record.evaluation_timestamp),
    )

    net_results: list[EmaNetOutcome] = []
    realized: list[EmaNetOutcome] = []
    for record in records:
        signal = signals_by_key.get(record.setup_key)
        if signal is None:
            raise ValueError("outcome setup_key is not present in replay result")
        net_result = calculate_net_outcome(signal, record.outcome, costs)
        net_results.append(net_result)
        if net_result.net_roi_pct is not None:
            realized.append(net_result)

    net_rois = [float(result.net_roi_pct) for result in realized]
    if any(not isfinite(value) for value in net_rois):
        raise ValueError("net ROI must be finite")
    positive = sum(value > 0 for value in net_rois)
    negative = sum(value < 0 for value in net_rois)
    flat = sum(value == 0 for value in net_rois)
    denominator = len(net_rois)
    fees = sum(float(result.entry_fee_pct or 0.0) + float(result.exit_fee_pct or 0.0) for result in realized)
    slippage = sum(float(result.entry_slippage_pct or 0.0) + float(result.exit_slippage_pct or 0.0) for result in realized)

    return EmaNetPerformanceReport(
        gross_report=gross_report,
        net_outcomes=tuple(net_results),
        average_net_roi_pct=None if not net_rois else sum(net_rois) / len(net_rois),
        median_net_roi_pct=None if not net_rois else float(median(net_rois)),
        total_net_roi_pct=None if not net_rois else sum(net_rois),
        best_net_roi_pct=None if not net_rois else max(net_rois),
        worst_net_roi_pct=None if not net_rois else min(net_rois),
        net_positive_trade_count=positive,
        net_negative_trade_count=negative,
        net_flat_trade_count=flat,
        net_positive_rate=None if not denominator else positive / denominator,
        net_positive_rate_denominator=denominator,
        total_modeled_fees_pct=fees,
        total_modeled_slippage_pct=slippage,
        funding_excluded=True,
    )
