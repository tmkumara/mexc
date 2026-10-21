"""Pure, explicitly configured trading-cost calculations for EMA backtests."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from ema_outcome import OutcomeResult
from ema_signal import EmaSignal


@dataclass(frozen=True)
class EmaTradingCosts:
    """Rates are non-negative fractions, not percentages.

    For example, ``0.0005`` represents 0.05%.  Funding is intentionally not
    represented and is excluded from every calculation in this module.
    """

    entry_fee_rate: float
    exit_fee_rate: float
    entry_slippage_rate: float
    exit_slippage_rate: float

    def __post_init__(self) -> None:
        for name in (
            "entry_fee_rate",
            "exit_fee_rate",
            "entry_slippage_rate",
            "exit_slippage_rate",
        ):
            value = getattr(self, name)
            if isinstance(value, bool):
                raise ValueError(f"{name} must be a finite non-negative rate")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a finite non-negative rate") from exc
            if not isfinite(number) or number < 0:
                raise ValueError(f"{name} must be a finite non-negative rate")
            object.__setattr__(self, name, number)


@dataclass(frozen=True)
class EmaNetOutcome:
    setup_key: str
    direction: str
    outcome_status: str
    gross_roi_pct: float | None
    entry_fee_pct: float | None
    exit_fee_pct: float | None
    entry_slippage_pct: float | None
    exit_slippage_pct: float | None
    total_cost_pct: float | None
    net_roi_pct: float | None


def _finite_price(value: object, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite positive price") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} must be a finite positive price")
    return number


def _gross_from_prices(signal: EmaSignal, exit_price: float) -> float:
    if signal.direction == "LONG":
        return (exit_price - signal.entry_price) / signal.entry_price * signal.leverage * 100.0
    return (signal.entry_price - exit_price) / signal.entry_price * signal.leverage * 100.0


def calculate_net_outcome(
    signal: EmaSignal,
    outcome: OutcomeResult,
    costs: EmaTradingCosts,
) -> EmaNetOutcome:
    """Calculate fee/slippage-adjusted ROI without changing outcome status."""
    if not isinstance(signal, EmaSignal):
        raise ValueError("signal must be an EmaSignal")
    if not isinstance(outcome, OutcomeResult):
        raise ValueError("outcome must be an OutcomeResult")
    if not isinstance(costs, EmaTradingCosts):
        raise ValueError("costs must be an EmaTradingCosts")
    if signal.leverage <= 0 or not isinstance(signal.leverage, int):
        raise ValueError("signal leverage must be a positive integer")
    if signal.entry_price <= 0 or not isfinite(float(signal.entry_price)):
        raise ValueError("signal entry_price must be positive and finite")
    if outcome.status not in {"WIN", "LOSS", "TIMEOUT", "PENDING"}:
        raise ValueError(f"unsupported outcome status: {outcome.status}")

    if outcome.status == "PENDING":
        if outcome.exit_price is not None or outcome.gross_roi_pct is not None:
            raise ValueError("PENDING outcome cannot contain exit price or gross ROI")
        return EmaNetOutcome(signal.setup_key, signal.direction, "PENDING", None, None, None, None, None, None, None)

    if outcome.exit_price is None or outcome.gross_roi_pct is None:
        raise ValueError("terminal outcome requires exit_price and gross_roi_pct")
    exit_price = _finite_price(outcome.exit_price, "outcome exit_price")
    gross_roi = float(outcome.gross_roi_pct)
    if not isfinite(gross_roi):
        raise ValueError("outcome gross_roi_pct must be finite")
    expected_gross = _gross_from_prices(signal, exit_price)
    if abs(gross_roi - expected_gross) > 1e-9 * max(1.0, abs(expected_gross)):
        raise ValueError("outcome gross ROI is inconsistent with signal and exit price")

    entry_price = float(signal.entry_price)
    if signal.direction == "LONG":
        effective_entry = entry_price * (1.0 + costs.entry_slippage_rate)
        effective_exit = exit_price * (1.0 - costs.exit_slippage_rate)
    else:
        effective_entry = entry_price * (1.0 - costs.entry_slippage_rate)
        effective_exit = exit_price * (1.0 + costs.exit_slippage_rate)

    quantity = signal.leverage / entry_price
    entry_notional = quantity * effective_entry
    exit_notional = quantity * effective_exit
    entry_fee_pct = entry_notional * costs.entry_fee_rate * 100.0
    exit_fee_pct = exit_notional * costs.exit_fee_rate * 100.0
    entry_slippage_pct = abs(effective_entry - entry_price) * quantity * 100.0
    exit_slippage_pct = abs(effective_exit - exit_price) * quantity * 100.0
    total_cost_pct = entry_fee_pct + exit_fee_pct + entry_slippage_pct + exit_slippage_pct
    net_roi_pct = gross_roi - total_cost_pct
    return EmaNetOutcome(
        setup_key=signal.setup_key,
        direction=signal.direction,
        outcome_status=outcome.status,
        gross_roi_pct=gross_roi,
        entry_fee_pct=entry_fee_pct,
        exit_fee_pct=exit_fee_pct,
        entry_slippage_pct=entry_slippage_pct,
        exit_slippage_pct=exit_slippage_pct,
        total_cost_pct=total_cost_pct,
        net_roi_pct=net_roi_pct,
    )
