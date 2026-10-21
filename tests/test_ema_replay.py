from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import ema_replay
from ema_dataset_loader import EmaHistoricalDataset


UTC = timezone.utc
EVAL_START = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
EVAL_END = datetime(2026, 1, 2, 0, 45, tzinfo=UTC)
WARMUP_START = datetime(2025, 12, 31, 0, 0, tzinfo=UTC)


def _frame(start, end, step):
    index = pd.date_range(start=start, end=end, freq=step, tz="UTC")
    values = list(range(len(index)))
    return pd.DataFrame(
        {
            "open": [100.0 + value for value in values],
            "high": [101.0 + value for value in values],
            "low": [99.0 + value for value in values],
            "close": [100.5 + value for value in values],
            "volume": [10.0 + value for value in values],
        },
        index=index,
    )


def _dataset():
    return EmaHistoricalDataset(
        symbol="SOL_USDT",
        candles_15m=_frame(WARMUP_START, EVAL_END, "15min"),
        candles_1h=_frame(WARMUP_START, EVAL_END + timedelta(hours=1), "1h"),
        evaluation_start_at=EVAL_START,
        evaluation_end_at=EVAL_END,
        warmup_start_at=WARMUP_START,
    )


def _accepted(evaluation_timestamp, setup_key):
    confirmation_open = evaluation_timestamp - timedelta(seconds=90, minutes=15)
    return {
        "accepted": True,
        "strategy_id": "ema_trend_pullback",
        "strategy_version": "v1",
        "symbol": "SOL_USDT",
        "direction": "LONG",
        "entry": 100.0,
        "tp": 101.5,
        "sl": 99.0,
        "leverage": 20,
        "gross_tp_roi_pct": 30.0,
        "gross_sl_roi_pct": 20.0,
        "rr": 1.5,
        "confirmation_candle_open": confirmation_open,
        "confirmation_close_at": confirmation_open + timedelta(minutes=15),
        "pivot_timestamp": confirmation_open - timedelta(minutes=15),
        "rsi_reset_timestamp": confirmation_open - timedelta(minutes=30),
        "setup_key": setup_key,
    }


def _run(monkeypatch, evaluator):
    monkeypatch.setattr(ema_replay, "evaluate_ema_pullback", evaluator)
    return ema_replay.replay_dataset(_dataset())


def test_inclusive_evaluation_period_and_one_evaluation_per_15m_candle(monkeypatch):
    evaluations = []

    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        evaluations.append((candles_15m.index[-1], evaluation_timestamp))
        return {"accepted": False, "reason": "test_rejected"}

    result = _run(monkeypatch, evaluator)

    assert result.total_evaluations == 4
    assert [item[0].to_pydatetime() for item in evaluations] == [
        datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 2, 0, 15, tzinfo=UTC),
        datetime(2026, 1, 2, 0, 30, tzinfo=UTC),
        datetime(2026, 1, 2, 0, 45, tzinfo=UTC),
    ]
    assert evaluations[-1][1].to_pydatetime() == datetime(2026, 1, 2, 1, 1, 30, tzinfo=UTC)


def test_warmup_is_not_evaluated_and_settlement_delay_is_90_seconds(monkeypatch):
    evaluations = []

    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        evaluations.append(evaluation_timestamp)
        return {"accepted": False, "reason": "forming_candle_excluded"}

    result = _run(monkeypatch, evaluator)

    assert result.total_evaluations == 4
    assert all(timestamp.tzinfo is not None for timestamp in evaluations)
    assert evaluations[0].to_pydatetime() == datetime(2026, 1, 2, 0, 16, 30, tzinfo=UTC)


def test_future_15m_and_forming_1h_candles_are_excluded(monkeypatch):
    captured = []

    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        captured.append((candles_15m.index[-1], candles_1h.index[-1], evaluation_timestamp))
        assert (candles_15m.index + pd.Timedelta(minutes=15) <= evaluation_timestamp).all()
        assert (candles_1h.index + pd.Timedelta(hours=1) <= evaluation_timestamp).all()
        return {"accepted": False, "reason": "test_rejected"}

    _run(monkeypatch, evaluator)

    assert captured[-1][0].to_pydatetime() == datetime(2026, 1, 2, 0, 45, tzinfo=UTC)
    assert captured[0][1].to_pydatetime() == datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    assert captured[-1][1].to_pydatetime() == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_rejection_counts_are_preserved(monkeypatch):
    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        return {"accepted": False, "reason": "trend_not_aligned"}

    result = _run(monkeypatch, evaluator)

    assert result.rejected_evaluation_count == 4
    assert result.rejection_reason_counts == {"trend_not_aligned": 4}
    assert result.accepted_signals == ()


def test_long_and_short_accepted_signals_retain_contract_fields(monkeypatch):
    directions = ["LONG", "SHORT", "LONG", "SHORT"]

    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        result = _accepted(evaluation_timestamp, f"setup-{len(calls)}")
        result["direction"] = directions[len(calls)]
        if result["direction"] == "SHORT":
            result.update({"entry": 100.0, "tp": 98.5, "sl": 101.0})
        calls.append(evaluation_timestamp)
        return result

    calls = []
    result = _run(monkeypatch, evaluator)

    assert [item.signal.direction for item in result.accepted_signals] == directions
    assert [item.signal.setup_key for item in result.accepted_signals] == ["setup-0", "setup-1", "setup-2", "setup-3"]
    assert all(item.signal.strategy_id == "ema_trend_pullback" for item in result.accepted_signals)
    assert all(item.evaluation_timestamp.tzinfo is not None for item in result.accepted_signals)
    assert [item.evaluation_timestamp for item in result.accepted_signals] == sorted(item.evaluation_timestamp for item in result.accepted_signals)


def test_duplicate_setup_keys_are_suppressed_and_counted(monkeypatch):
    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        return _accepted(evaluation_timestamp, "same-setup")

    result = _run(monkeypatch, evaluator)

    assert len(result.accepted_signals) == 1
    assert result.duplicate_count == 3


def test_stable_repeated_replay_results(monkeypatch):
    def evaluator(symbol, candles_15m, candles_1h, evaluation_timestamp):
        return _accepted(evaluation_timestamp, evaluation_timestamp.isoformat())

    first = _run(monkeypatch, evaluator)
    second = _run(monkeypatch, evaluator)

    assert first == second


def test_dataset_is_not_mutated(monkeypatch):
    dataset = _dataset()
    original_15m = dataset.candles_15m.copy(deep=True)
    original_1h = dataset.candles_1h.copy(deep=True)

    monkeypatch.setattr(ema_replay, "evaluate_ema_pullback", lambda *args: {"accepted": False, "reason": "x"})
    ema_replay.replay_dataset(dataset)

    pd.testing.assert_frame_equal(dataset.candles_15m, original_15m)
    pd.testing.assert_frame_equal(dataset.candles_1h, original_1h)


def test_invalid_dataset_type_is_rejected():
    with pytest.raises(ValueError, match="dataset"):
        ema_replay.replay_dataset(SimpleNamespace())


def test_real_evaluator_runs_through_replay_for_final_fixture_candle():
    fixture_path = Path(__file__).with_name("test_strategy_ema_pullback.py")
    spec = importlib.util.spec_from_file_location("strategy_fixture", fixture_path)
    fixture_module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(fixture_module)

    hourly, quarter, _ = fixture_module._fixture("long")
    hourly = hourly.copy()
    quarter = quarter.copy()
    hourly.index = hourly.index.tz_localize("UTC")
    quarter.index = quarter.index.tz_localize("UTC")
    dataset = EmaHistoricalDataset(
        symbol="TEST",
        candles_15m=quarter,
        candles_1h=hourly,
        evaluation_start_at=quarter.index[-2].to_pydatetime(),
        evaluation_end_at=quarter.index[-1].to_pydatetime(),
        warmup_start_at=quarter.index[0].to_pydatetime(),
    )

    result = ema_replay.replay_dataset(dataset)

    assert result.total_evaluations == 2
    assert result.accepted_signals[-1].signal.direction == "LONG"

