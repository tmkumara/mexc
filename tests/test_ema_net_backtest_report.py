from datetime import datetime, timedelta, timezone

import pytest

from ema_backtest_outcomes import EmaBacktestOutcome, EmaBacktestOutcomes
from ema_net_backtest_report import build_net_performance_report
from ema_outcome import OutcomeResult
from ema_replay import EmaReplayResult, EmaReplaySignal
from ema_signal import EmaSignal
from ema_trading_costs import EmaTradingCosts


UTC = timezone.utc
START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _signal(key, direction="LONG", generated_at=None):
    return EmaSignal(
        symbol="SOL_USDT", direction=direction, entry_price=100.0,
        tp_price=110.0 if direction == "LONG" else 90.0,
        sl_price=90.0 if direction == "LONG" else 110.0,
        leverage=20, tp_roi_pct=200.0, sl_roi_pct=200.0, rr=1.5,
        strategy_id="ema_trend_pullback", strategy_version="v1", setup_key=key,
        confirmation_candle_open=START, confirmation_close_at=START + timedelta(minutes=15),
        pivot_timestamp=START - timedelta(minutes=30), rsi_reset_timestamp=START - timedelta(minutes=15),
        generated_at=generated_at or START + timedelta(minutes=16, seconds=30),
    )


def _outcome(status, exit_price=None, gross_roi=None):
    return OutcomeResult(status, START + timedelta(hours=1) if exit_price is not None else None, exit_price, gross_roi, "test")


def _inputs(items):
    signals = [item[0] for item in items]
    replay = EmaReplayResult(
        "SOL_USDT", START, START + timedelta(days=1), 20,
        tuple(EmaReplaySignal(signal, signal.generated_at) for signal in signals),
        4, {"rejected": 4}, 2,
    )
    records = tuple(EmaBacktestOutcome(signal.setup_key, signal.generated_at, outcome) for signal, outcome in items)
    counts = {status: sum(record.outcome.status == status for record in records) for status in ("WIN", "LOSS", "TIMEOUT", "PENDING")}
    outcomes = EmaBacktestOutcomes("SOL_USDT", len(records), records, tuple(r for r in records if r.outcome.status == "PENDING"), tuple(r for r in records if r.outcome.status != "PENDING"), counts)
    return replay, outcomes


def test_mixed_net_report_preserves_gross_and_aggregates_costs():
    replay, outcomes = _inputs([
        (_signal("win"), _outcome("WIN", 105.0, 100.0)),
        (_signal("loss"), _outcome("LOSS", 99.0, -20.0)),
        (_signal("timeout"), _outcome("TIMEOUT", 102.0, 40.0)),
    ])
    report = build_net_performance_report(replay, outcomes, EmaTradingCosts(0.001, 0.001, 0.001, 0.001))

    assert report.gross_report.win_count == 1
    assert report.gross_report.timeout_count == 1
    assert report.average_net_roi_pct is not None
    assert report.median_net_roi_pct is not None
    assert report.best_net_roi_pct == max(result.net_roi_pct for result in report.net_outcomes if result.net_roi_pct is not None)
    assert report.worst_net_roi_pct == min(result.net_roi_pct for result in report.net_outcomes if result.net_roi_pct is not None)
    assert report.total_net_roi_pct == pytest.approx(sum(result.net_roi_pct for result in report.net_outcomes if result.net_roi_pct is not None))
    assert report.total_modeled_fees_pct > 0
    assert report.total_modeled_slippage_pct > 0
    assert report.funding_excluded is True


def test_zero_cost_net_report_matches_gross_report():
    replay, outcomes = _inputs([(_signal("win"), _outcome("WIN", 105.0, 100.0))])
    report = build_net_performance_report(replay, outcomes, EmaTradingCosts(0, 0, 0, 0))

    assert report.average_net_roi_pct == report.gross_report.average_gross_roi_pct
    assert report.total_net_roi_pct == report.gross_report.total_gross_roi_pct
    assert report.net_positive_trade_count == 1


def test_pending_and_empty_backtests_have_null_net_rates_and_metrics():
    replay, outcomes = _inputs([(_signal("pending"), _outcome("PENDING"))])
    pending = build_net_performance_report(replay, outcomes, EmaTradingCosts(0.1, 0.1, 0.1, 0.1))
    assert pending.net_positive_rate is None
    assert pending.average_net_roi_pct is None
    assert pending.total_modeled_fees_pct == 0.0

    empty_replay, empty_outcomes = _inputs([])
    empty = build_net_performance_report(empty_replay, empty_outcomes, EmaTradingCosts(0, 0, 0, 0))
    assert empty.to_dict()["average_net_roi_pct"] is None


def test_positive_negative_flat_counts_and_denominator():
    replay, outcomes = _inputs([
        (_signal("positive"), _outcome("WIN", 105.0, 100.0)),
        (_signal("negative"), _outcome("LOSS", 99.0, -20.0)),
        (_signal("flat"), _outcome("TIMEOUT", 100.0, 0.0)),
    ])
    report = build_net_performance_report(replay, outcomes, EmaTradingCosts(0, 0, 0, 0))
    assert report.net_positive_trade_count == 1
    assert report.net_negative_trade_count == 1
    assert report.net_flat_trade_count == 1
    assert report.net_positive_rate == pytest.approx(1 / 3)
    assert report.net_positive_rate_denominator == 3


def test_json_output_is_primitive_and_deterministic():
    replay, outcomes = _inputs([(_signal("one"), _outcome("WIN", 105.0, 100.0))])
    costs = EmaTradingCosts(0.001, 0.001, 0.001, 0.001)
    first = build_net_performance_report(replay, outcomes, costs)
    second = build_net_performance_report(replay, outcomes, costs)
    assert first == second
    assert isinstance(first.to_dict()["gross_performance"], dict)
    assert isinstance(first.to_dict()["funding_excluded"], bool)


def test_invalid_setup_or_duplicate_records_are_rejected():
    replay, outcomes = _inputs([(_signal("one"), _outcome("WIN", 105.0, 100.0))])
    bad = EmaBacktestOutcomes(
        "SOL_USDT", 1,
        (EmaBacktestOutcome("other", START, _outcome("WIN", 105.0, 100.0)),),
        (), (), {"WIN": 1, "LOSS": 0, "TIMEOUT": 0, "PENDING": 0},
    )
    with pytest.raises(ValueError, match="setup_key"):
        build_net_performance_report(replay, bad, EmaTradingCosts(0, 0, 0, 0))


def test_inputs_are_not_mutated():
    replay, outcomes = _inputs([(_signal("one"), _outcome("WIN", 105.0, 100.0))])
    before = outcomes.outcomes
    build_net_performance_report(replay, outcomes, EmaTradingCosts(0.001, 0.001, 0.001, 0.001))
    assert outcomes.outcomes == before
