from datetime import datetime, timedelta, timezone

import pytest

from ema_outcome import OutcomeResult
from ema_signal import EmaSignal
from ema_trading_costs import EmaNetOutcome, EmaTradingCosts, calculate_net_outcome


UTC = timezone.utc
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _signal(direction="LONG", setup_key="setup"):
    return EmaSignal(
        symbol="SOL_USDT",
        direction=direction,
        entry_price=100.0,
        tp_price=110.0 if direction == "LONG" else 90.0,
        sl_price=90.0 if direction == "LONG" else 110.0,
        leverage=20,
        tp_roi_pct=200.0,
        sl_roi_pct=200.0,
        rr=1.5,
        strategy_id="ema_trend_pullback",
        strategy_version="v1",
        setup_key=setup_key,
        confirmation_candle_open=START,
        confirmation_close_at=START + timedelta(minutes=15),
        pivot_timestamp=START - timedelta(minutes=30),
        rsi_reset_timestamp=START - timedelta(minutes=15),
        generated_at=START + timedelta(minutes=16, seconds=30),
    )


def _outcome(status, exit_price=None, gross_roi=None):
    return OutcomeResult(status, START + timedelta(hours=1) if exit_price is not None else None, exit_price, gross_roi, "test")


def test_zero_cost_reproduces_long_gross_roi():
    result = calculate_net_outcome(_signal(), _outcome("WIN", 105.0, 100.0), EmaTradingCosts(0, 0, 0, 0))

    assert result == EmaNetOutcome("setup", "LONG", "WIN", 100.0, 0.0, 0.0, 0.0, 0.0, 0.0, 100.0)


def test_long_costs_use_exit_notional_and_adverse_slippage():
    result = calculate_net_outcome(
        _signal(),
        _outcome("WIN", 105.0, 100.0),
        EmaTradingCosts(0.0005, 0.0007, 0.001, 0.002),
    )

    assert result.entry_fee_pct == pytest.approx(1.001)
    assert result.exit_fee_pct == pytest.approx(1.46706)
    assert result.entry_slippage_pct == pytest.approx(2.0)
    assert result.exit_slippage_pct == pytest.approx(4.2)
    assert result.total_cost_pct == pytest.approx(8.66806)
    assert result.net_roi_pct == pytest.approx(91.33194)


def test_short_costs_use_adverse_opposite_execution_direction():
    result = calculate_net_outcome(
        _signal("SHORT", "short"),
        _outcome("WIN", 95.0, 100.0),
        EmaTradingCosts(0.0005, 0.0007, 0.001, 0.002),
    )

    assert result.entry_slippage_pct == pytest.approx(2.0)
    assert result.exit_slippage_pct == pytest.approx(3.8)
    assert result.net_roi_pct == pytest.approx(100.0 - result.total_cost_pct)


def test_loss_and_timeout_use_actual_exit_price_not_target():
    costs = EmaTradingCosts(0.001, 0.001, 0, 0)
    loss = calculate_net_outcome(_signal("LONG", "loss"), _outcome("LOSS", 94.0, -120.0), costs)
    timeout = calculate_net_outcome(_signal("LONG", "timeout"), _outcome("TIMEOUT", 103.0, 60.0), costs)

    assert loss.gross_roi_pct == -120.0
    assert timeout.gross_roi_pct == 60.0
    assert loss.net_roi_pct != timeout.net_roi_pct


def test_pending_preserves_status_without_realized_values():
    result = calculate_net_outcome(_signal(), _outcome("PENDING"), EmaTradingCosts(0.1, 0.1, 0.1, 0.1))

    assert result.outcome_status == "PENDING"
    assert result.net_roi_pct is None
    assert result.total_cost_pct is None


@pytest.mark.parametrize(
    "field_values",
    [(-0.1, 0, 0, 0), (float("nan"), 0, 0, 0), (0, float("inf"), 0, 0)],
)
def test_invalid_cost_rates_are_rejected(field_values):
    with pytest.raises(ValueError, match="rate"):
        EmaTradingCosts(*field_values)


def test_invalid_prices_and_leverage_are_rejected():
    with pytest.raises(ValueError, match="exit_price"):
        calculate_net_outcome(_signal(), _outcome("WIN", 0, 100.0), EmaTradingCosts(0, 0, 0, 0))

    invalid = object.__new__(EmaSignal)
    object.__setattr__(invalid, "setup_key", "bad")
    object.__setattr__(invalid, "direction", "LONG")
    object.__setattr__(invalid, "entry_price", 100.0)
    object.__setattr__(invalid, "leverage", 0)
    with pytest.raises(ValueError, match="leverage"):
        calculate_net_outcome(invalid, _outcome("WIN", 105.0, 100.0), EmaTradingCosts(0, 0, 0, 0))


def test_gross_roi_inconsistency_is_rejected():
    with pytest.raises(ValueError, match="gross ROI"):
        calculate_net_outcome(_signal(), _outcome("WIN", 105.0, 99.0), EmaTradingCosts(0, 0, 0, 0))


def test_costs_are_not_double_counted():
    result = calculate_net_outcome(_signal(), _outcome("WIN", 105.0, 100.0), EmaTradingCosts(0.001, 0.001, 0.001, 0.001))

    assert result.net_roi_pct == pytest.approx(result.gross_roi_pct - result.total_cost_pct)


def test_result_is_stable_and_input_objects_unchanged():
    signal = _signal()
    outcome = _outcome("WIN", 105.0, 100.0)
    costs = EmaTradingCosts(0.001, 0.001, 0.001, 0.001)

    first = calculate_net_outcome(signal, outcome, costs)
    second = calculate_net_outcome(signal, outcome, costs)

    assert first == second
    assert signal.entry_price == 100.0
    assert outcome.exit_price == 105.0
