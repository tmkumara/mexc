from datetime import datetime, timedelta, timezone

import pytest

from ema_backtest_outcomes import EmaBacktestOutcome, EmaBacktestOutcomes
from ema_backtest_report import build_performance_report
from ema_outcome import OutcomeResult
from ema_replay import EmaReplayResult, EmaReplaySignal
from ema_signal import EmaSignal


UTC = timezone.utc
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _signal(setup_key: str, direction: str = "LONG", generated_at: datetime | None = None) -> EmaSignal:
    return EmaSignal(
        symbol="SOL_USDT",
        direction=direction,
        entry_price=100.0,
        tp_price=101.5 if direction == "LONG" else 98.5,
        sl_price=99.0 if direction == "LONG" else 101.0,
        leverage=20,
        tp_roi_pct=30.0,
        sl_roi_pct=20.0,
        rr=1.5,
        strategy_id="ema_trend_pullback",
        strategy_version="v1",
        setup_key=setup_key,
        confirmation_candle_open=START,
        confirmation_close_at=START + timedelta(minutes=15),
        pivot_timestamp=START - timedelta(minutes=30),
        rsi_reset_timestamp=START - timedelta(minutes=15),
        generated_at=generated_at or START + timedelta(minutes=16, seconds=30),
    )


def _replay(signals: list[EmaSignal], symbol: str = "SOL_USDT", total_evaluations: int = 10) -> EmaReplayResult:
    return EmaReplayResult(
        symbol=symbol,
        evaluation_start_at=START,
        evaluation_end_at=START + timedelta(days=1),
        total_evaluations=total_evaluations,
        accepted_signals=tuple(EmaReplaySignal(signal, signal.generated_at) for signal in signals),
        rejected_evaluation_count=3,
        rejection_reason_counts={"no_setup": 3},
        duplicate_count=1,
    )


def _outcomes(signals: list[EmaSignal], results: list[OutcomeResult]) -> EmaBacktestOutcomes:
    records = tuple(
        EmaBacktestOutcome(signal.setup_key, signal.generated_at, outcome)
        for signal, outcome in zip(signals, results)
    )
    pending = tuple(record for record in records if record.outcome.status == "PENDING")
    completed = tuple(record for record in records if record.outcome.status != "PENDING")
    counts = {status: sum(record.outcome.status == status for record in records) for status in ("WIN", "LOSS", "TIMEOUT", "PENDING")}
    return EmaBacktestOutcomes("SOL_USDT", len(records), records, pending, completed, counts)


def _result(status: str, roi: float | None, reason: str = "test") -> OutcomeResult:
    if status == "PENDING":
        return OutcomeResult(status, None, None, None, reason)
    return OutcomeResult(status, START + timedelta(hours=1), 100.0, roi, reason)


def _build(signals: list[EmaSignal], results: list[OutcomeResult]):
    return build_performance_report(_replay(signals), _outcomes(signals, results))


def test_win_only_metrics_and_json_output():
    report = _build([_signal("win")], [_result("WIN", 30.0)])

    assert report.win_count == 1
    assert report.loss_count == 0
    assert report.resolved_trade_count == 1
    assert report.win_rate == 1.0
    assert report.win_rate_denominator == 1
    assert report.average_gross_roi_pct == 30.0
    assert report.median_gross_roi_pct == 30.0
    assert report.total_gross_roi_pct == 30.0
    assert report.to_dict()["symbol"] == "SOL_USDT"


def test_loss_only_metrics():
    report = _build([_signal("loss")], [_result("LOSS", -20.0)])

    assert report.loss_rate == 1.0
    assert report.loss_rate_denominator == 1
    assert report.best_trade_gross_roi_pct == -20.0
    assert report.worst_trade_gross_roi_pct == -20.0


def test_mixed_rates_median_best_worst_and_average_rr():
    signals = [_signal("a"), _signal("b"), _signal("c", direction="SHORT")]
    report = _build(signals, [_result("WIN", 30.0), _result("LOSS", -20.0), _result("TIMEOUT", 5.0)])

    assert report.win_count == 1
    assert report.loss_count == 1
    assert report.timeout_count == 1
    assert report.resolved_trade_count == 3
    assert report.win_rate == 0.5
    assert report.loss_rate == 0.5
    assert report.win_rate_denominator == 2
    assert report.average_gross_roi_pct == pytest.approx(5.0)
    assert report.median_gross_roi_pct == 5.0
    assert report.total_gross_roi_pct == 15.0
    assert report.average_reward_to_risk == 1.5
    assert report.best_trade_gross_roi_pct == 30.0
    assert report.worst_trade_gross_roi_pct == -20.0


def test_pending_and_empty_results_use_null_undefined_metrics():
    signal = _signal("pending")
    report = _build([signal], [_result("PENDING", None, "awaiting_expiry")])

    assert report.pending_count == 1
    assert report.resolved_trade_count == 0
    assert report.win_rate is None
    assert report.loss_rate is None
    assert report.average_gross_roi_pct is None
    assert report.total_gross_roi_pct is None
    assert report.average_reward_to_risk is None

    empty = _build([], [])
    assert empty.accepted_signals == 0
    assert empty.to_dict()["average_gross_roi_pct"] is None


def test_streaks_use_chronological_order_not_input_order():
    first = _signal("first", generated_at=START + timedelta(minutes=16, seconds=30))
    second = _signal("second", generated_at=START + timedelta(minutes=31, seconds=30))
    third = _signal("third", generated_at=START + timedelta(minutes=46, seconds=30))
    report = _build([third, first, second], [_result("LOSS", -20.0), _result("WIN", 30.0), _result("WIN", 30.0)])

    assert report.maximum_consecutive_wins == 2
    assert report.maximum_consecutive_losses == 1


def test_mismatched_symbols_are_rejected():
    signals = [_signal("one")]
    with pytest.raises(ValueError, match="symbol"):
        build_performance_report(_replay(signals, symbol="XRP_USDT"), _outcomes(signals, [_result("WIN", 30.0)]))


def test_mismatched_counts_are_rejected():
    signals = [_signal("one"), _signal("two")]
    with pytest.raises(ValueError, match="count"):
        build_performance_report(_replay(signals), _outcomes(signals[:1], [_result("WIN", 30.0)]))


def test_duplicate_setup_keys_are_rejected():
    signal = _signal("same")
    duplicate = EmaBacktestOutcomes(
        "SOL_USDT",
        2,
        (
            EmaBacktestOutcome("same", signal.generated_at, _result("WIN", 30.0)),
            EmaBacktestOutcome("same", signal.generated_at + timedelta(minutes=15), _result("LOSS", -20.0)),
        ),
        (),
        (),
        {"WIN": 1, "LOSS": 1, "TIMEOUT": 0, "PENDING": 0},
    )
    with pytest.raises(ValueError, match="duplicate"):
        build_performance_report(_replay([signal]), duplicate)


def test_input_results_are_not_mutated_and_repeated_reports_are_equal():
    signals = [_signal("one"), _signal("two")]
    outcomes = _outcomes(signals, [_result("WIN", 30.0), _result("TIMEOUT", 5.0)])
    replay = _replay(signals)
    before = outcomes.outcomes

    first = build_performance_report(replay, outcomes)
    second = build_performance_report(replay, outcomes)

    assert first == second
    assert outcomes.outcomes == before


def test_boundary_regression_preserves_timeout_as_terminal_trade():
    report = _build([_signal("timeout")], [_result("TIMEOUT", 2.5, "expiry_reached")])

    assert report.timeout_count == 1
    assert report.resolved_trade_count == 1
    assert report.win_rate_denominator == 0
    assert report.total_gross_roi_pct == 2.5


def test_missing_realized_roi_is_excluded_from_roi_statistics():
    report = _build([_signal("timeout")], [_result("TIMEOUT", None)])

    assert report.timeout_count == 1
    assert report.resolved_trade_count == 1
    assert report.average_gross_roi_pct is None
    assert report.total_gross_roi_pct is None
