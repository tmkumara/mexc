from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ema_backtest_outcomes import simulate_ema_outcomes, simulate_replay_outcomes
from ema_expiry import calculate_expiry_at
from ema_dataset_loader import EmaHistoricalDataset
from ema_outcome import OutcomeResult
from ema_replay import EmaReplayResult, EmaReplaySignal
from ema_signal import EmaSignal


UTC = timezone.utc
CONFIRMATION_OPEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CONFIRMATION_CLOSE = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
ACTIONABLE_OPEN = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
HORIZON_END = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)


def _signal(direction="LONG", setup_key="setup-1", generated_at=None):
    if direction == "LONG":
        tp, sl = 101.5, 99.0
    else:
        tp, sl = 98.5, 101.0
    return EmaSignal(
        symbol="SOL_USDT",
        direction=direction,
        entry_price=100.0,
        tp_price=tp,
        sl_price=sl,
        leverage=20,
        tp_roi_pct=30.0,
        sl_roi_pct=20.0,
        rr=1.5,
        strategy_id="ema_trend_pullback",
        strategy_version="v1",
        setup_key=setup_key,
        confirmation_candle_open=CONFIRMATION_OPEN,
        confirmation_close_at=CONFIRMATION_CLOSE,
        pivot_timestamp=CONFIRMATION_OPEN - timedelta(minutes=30),
        rsi_reset_timestamp=CONFIRMATION_OPEN - timedelta(minutes=15),
        generated_at=generated_at or CONFIRMATION_CLOSE + timedelta(seconds=90),
    )


def _replay(*signals, symbol="SOL_USDT"):
    entries = tuple(
        EmaReplaySignal(signal=signal, evaluation_timestamp=signal.generated_at)
        for signal in signals
    )
    return EmaReplayResult(
        symbol=symbol,
        evaluation_start_at=CONFIRMATION_OPEN,
        evaluation_end_at=HORIZON_END,
        total_evaluations=1,
        accepted_signals=entries,
        rejected_evaluation_count=0,
        rejection_reason_counts={},
        duplicate_count=0,
    )


def _candles(rows):
    index = [CONFIRMATION_CLOSE + timedelta(minutes=15 * offset) for offset, *_ in rows]
    return pd.DataFrame(
        {
            "open": [opening for _, opening, _, _, _ in rows],
            "high": [high for _, _, high, _, _ in rows],
            "low": [low for _, _, _, low, _ in rows],
            "close": [closing for _, _, _, _, closing in rows],
            "volume": [10.0] * len(rows),
        },
        index=pd.DatetimeIndex(index),
    )


def _dataset(candles):
    hourly = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [10.0]},
        index=pd.DatetimeIndex([CONFIRMATION_OPEN]),
    )
    return EmaHistoricalDataset(
        symbol="SOL_USDT",
        candles_15m=candles,
        candles_1h=hourly,
        evaluation_start_at=CONFIRMATION_OPEN,
        evaluation_end_at=HORIZON_END,
        warmup_start_at=CONFIRMATION_OPEN,
    )


def _simulate(direction="LONG", rows=None, expiry=timedelta(hours=1), setup_key="setup-1"):
    if rows is None:
        rows = [
            (1, 100.0, 100.4, 99.8, 100.1),
            (2, 100.1, 100.4, 99.8, 100.2),
            (3, 100.2, 100.4, 99.8, 100.3),
        ]
    signal = _signal(direction, setup_key)
    return simulate_replay_outcomes(_dataset(_candles(rows)), _replay(signal), expiry)


def test_long_take_profit():
    result = _simulate(rows=[
        (-1, 100.0, 101.8, 99.8, 101.0),
        (0, 100.0, 100.4, 99.8, 100.1),
        (1, 100.1, 101.6, 100.0, 101.4),
        (2, 101.4, 101.5, 101.0, 101.2),
    ])

    assert result.counts["WIN"] == 1
    assert result.outcomes[0].outcome.status == "WIN"


def test_long_stop_loss():
    result = _simulate(rows=[
        (1, 100.0, 100.4, 99.8, 100.1),
        (2, 100.1, 100.2, 98.9, 99.1),
        (3, 99.1, 99.4, 99.0, 99.2),
    ])

    assert result.counts["LOSS"] == 1
    assert result.outcomes[0].outcome.reason == "sl_hit"


def test_short_take_profit_and_stop_loss():
    win = _simulate("SHORT", rows=[(1, 100.0, 100.2, 98.4, 99.0), (2, 99.0, 99.3, 98.8, 99.1)])
    loss = _simulate("SHORT", rows=[(1, 100.0, 101.1, 100.0, 100.8), (2, 100.8, 100.9, 100.5, 100.7)], setup_key="short-loss")

    assert win.counts["WIN"] == 1
    assert loss.counts["LOSS"] == 1


def test_same_candle_tp_sl_collision_is_loss():
    result = _simulate(rows=[(1, 100.0, 101.6, 98.9, 100.0), (2, 100.0, 100.4, 99.8, 100.1), (3, 100.1, 100.4, 99.8, 100.2)])

    assert result.outcomes[0].outcome.status == "LOSS"
    assert result.outcomes[0].outcome.reason == "both_hit_sl_first"


def test_complete_history_produces_timeout():
    result = _simulate()

    assert result.counts["TIMEOUT"] == 1
    assert result.outcomes[0].outcome.reason == "expiry_reached"


def test_pending_before_expiry_is_preserved():
    result = _simulate(expiry=timedelta(days=2))

    assert result.counts["PENDING"] == 1
    assert result.outcomes[0].outcome.reason in {"awaiting_expiry", "insufficient_history"}


def test_missing_candle_prevents_false_timeout():
    result = _simulate(rows=[(1, 100.0, 100.4, 99.8, 100.1), (3, 100.1, 100.4, 99.8, 100.1)])

    assert result.counts["PENDING"] == 1
    assert result.outcomes[0].outcome.reason == "insufficient_history"


def test_signal_near_dataset_end_remains_pending_when_expiry_is_unavailable():
    result = _simulate(expiry=timedelta(hours=4))

    assert result.counts["PENDING"] == 1


def test_confirmation_and_settlement_overlap_candle_cannot_trigger_outcome():
    result = _simulate(rows=[
        (-1, 100.0, 101.8, 98.0, 100.0),
        (1, 100.0, 100.4, 99.8, 100.1),
        (2, 100.1, 100.4, 99.8, 100.1),
        (3, 100.1, 100.4, 99.8, 100.1),
    ])

    assert result.outcomes[0].outcome.status == "TIMEOUT"


def test_no_candle_before_signal_availability_is_used():
    result = _simulate(rows=[
        (-1, 100.0, 101.8, 98.0, 100.0),
        (0, 100.0, 100.4, 99.8, 100.1),
        (1, 100.1, 100.4, 99.8, 100.1),
        (2, 100.1, 100.4, 99.8, 100.1),
    ])

    assert result.outcomes[0].outcome.status != "WIN"


def test_multiple_signals_are_chronological():
    first = _signal(setup_key="first")
    second = _signal(setup_key="second", generated_at=CONFIRMATION_CLOSE + timedelta(minutes=30))
    result = simulate_replay_outcomes(_dataset(_candles([
        (1, 100.0, 100.4, 99.8, 100.1),
        (2, 100.1, 100.4, 99.8, 100.2),
        (3, 100.2, 100.4, 99.8, 100.3),
    ])), _replay(second, first), timedelta(hours=1))

    assert [item.setup_key for item in result.outcomes] == ["first", "second"]


def test_duplicate_setup_key_is_rejected():
    signal = _signal()

    with pytest.raises(ValueError, match="duplicate"):
        simulate_replay_outcomes(_dataset(_candles([])), _replay(signal, signal), timedelta(hours=1))


def test_symbol_mismatch_is_rejected():
    with pytest.raises(ValueError, match="symbol"):
        simulate_replay_outcomes(_dataset(_candles([])), _replay(_signal(), symbol="XRP_USDT"), timedelta(hours=1))


@pytest.mark.parametrize("expiry", [timedelta(0), timedelta(seconds=-1), "1h"])
def test_invalid_expiry_duration_is_rejected(expiry):
    with pytest.raises(ValueError, match="expiry"):
        simulate_replay_outcomes(_dataset(_candles([])), _replay(_signal()), expiry)


def test_input_dataset_is_not_mutated_and_repeated_runs_are_stable():
    candles = _candles([(0, 100.0, 100.4, 99.8, 100.1), (1, 100.1, 100.4, 99.8, 100.2), (2, 100.2, 100.4, 99.8, 100.3)])
    dataset = _dataset(candles)
    original = dataset.candles_15m.copy(deep=True)
    replay = _replay(_signal())

    first = simulate_replay_outcomes(dataset, replay, timedelta(hours=1))
    second = simulate_replay_outcomes(dataset, replay, timedelta(hours=1))

    assert first == second
    pd.testing.assert_frame_equal(dataset.candles_15m, original)


def test_result_counts_include_all_statuses_and_preserve_setup_key():
    result = _simulate()

    assert result.total_accepted_signals == 1
    assert set(result.counts) == {"WIN", "LOSS", "TIMEOUT", "PENDING"}
    assert result.outcomes[0].setup_key == "setup-1"
    assert isinstance(result.outcomes[0].outcome, OutcomeResult)


def test_approved_entry_point_uses_exact_four_hour_expiry():
    signal = _signal()
    assert calculate_expiry_at(signal.confirmation_close_at) == CONFIRMATION_CLOSE + timedelta(hours=4, seconds=90)

    rows = [(offset, 100.0, 100.4, 99.8, 100.1) for offset in range(1, 16)]
    rows.append((16, 100.0, 101.8, 99.8, 101.0))
    result = simulate_ema_outcomes(_dataset(_candles(rows)), _replay(signal))

    assert result.outcomes[0].outcome.status == "TIMEOUT"
    assert result.outcomes[0].outcome.reason == "expiry_reached"
    assert result.outcomes[0].outcome.exit_price == 100.1


def test_approved_entry_point_preserves_signal_metadata_and_setup_key():
    signal = _signal(setup_key="approved-key")
    result = simulate_ema_outcomes(_dataset(_candles([(1, 100.0, 101.6, 99.8, 101.2)])), _replay(signal))

    assert result.outcomes[0].setup_key == signal.setup_key
    assert signal.confirmation_close_at == CONFIRMATION_CLOSE
    assert signal.generated_at == CONFIRMATION_CLOSE + timedelta(seconds=90)


def test_generic_duration_interface_remains_available():
    signal = _signal()
    result = simulate_replay_outcomes(
        _dataset(_candles([(1, 100.0, 101.6, 99.8, 101.2)])),
        _replay(signal),
        timedelta(hours=1),
    )

    assert result.outcomes[0].outcome.status == "WIN"
