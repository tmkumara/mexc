from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ema_outcome import OutcomeResult, evaluate_outcome
from ema_signal import EmaSignal


UTC = timezone.utc
CONFIRMATION_OPEN = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
CONFIRMATION_CLOSE = datetime(2026, 1, 2, 12, 15, tzinfo=UTC)


def _signal(direction="LONG"):
    if direction == "LONG":
        tp, sl = 101.5, 99.0
    else:
        tp, sl = 98.5, 101.0
    return EmaSignal(
        symbol="TEST_USDT",
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
        setup_key=f"outcome-{direction}",
        confirmation_candle_open=CONFIRMATION_OPEN,
        confirmation_close_at=CONFIRMATION_CLOSE,
        pivot_timestamp=CONFIRMATION_OPEN - timedelta(minutes=30),
        rsi_reset_timestamp=CONFIRMATION_OPEN - timedelta(minutes=15),
        generated_at=CONFIRMATION_CLOSE + timedelta(minutes=1),
    )


def _candles(rows):
    index = [CONFIRMATION_CLOSE + timedelta(minutes=15 * offset) for offset, *_ in rows]
    return pd.DataFrame(
        {
            "open": [opening for _, opening, _, _, _ in rows],
            "high": [high for _, _, high, _, _ in rows],
            "low": [low for _, _, _, low, _ in rows],
            "close": [closing for _, _, _, _, closing in rows],
        },
        index=pd.DatetimeIndex(index),
    )


def _evaluate(signal, candles, evaluation, expiry):
    return evaluate_outcome(signal, candles, evaluation, expiry)


def test_long_tp_hit():
    candles = _candles([(0, 100.0, 101.6, 99.8, 101.2)])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "WIN"
    assert result.exit_price == 101.5
    assert result.gross_roi_pct == 30.0
    assert result.reason == "tp_hit"


def test_long_sl_hit():
    candles = _candles([(0, 100.0, 100.2, 98.9, 99.2)])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "LOSS"
    assert result.exit_price == 99.0
    assert result.gross_roi_pct == -20.0


def test_short_tp_hit():
    candles = _candles([(0, 100.0, 100.2, 98.4, 99.0)])

    result = _evaluate(_signal("SHORT"), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "WIN"
    assert result.exit_price == 98.5
    assert result.gross_roi_pct == 30.0


def test_short_sl_hit():
    candles = _candles([(0, 100.0, 101.1, 99.8, 100.8)])

    result = _evaluate(_signal("SHORT"), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "LOSS"
    assert result.exit_price == 101.0
    assert result.gross_roi_pct == -20.0


def test_same_candle_tp_and_sl_is_conservative_loss():
    candles = _candles([(0, 100.0, 101.6, 98.9, 100.0)])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "LOSS"
    assert result.exit_price == 99.0
    assert result.reason == "both_hit_sl_first"


def test_tp_reached_after_several_candles():
    candles = _candles([
        (0, 100.0, 100.4, 99.8, 100.1),
        (1, 100.1, 100.5, 99.9, 100.3),
        (2, 100.3, 101.6, 100.1, 101.4),
    ])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(hours=1), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "WIN"
    assert result.closed_at == CONFIRMATION_CLOSE + timedelta(minutes=45)


def test_no_hit_before_expiry_remains_pending():
    candles = _candles([(0, 100.0, 100.4, 99.8, 100.1)])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "PENDING"
    assert result.reason == "awaiting_expiry"


def test_delayed_check_finds_hit_before_expiry_instead_of_timeout():
    candles = _candles([
        (0, 100.0, 101.6, 99.8, 101.2),
        (1, 101.2, 101.3, 100.8, 101.0),
        (2, 101.0, 101.2, 100.8, 101.0),
    ])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(days=1), CONFIRMATION_CLOSE + timedelta(hours=1))

    assert result.status == "WIN"
    assert result.reason == "tp_hit"


def test_forming_candle_is_ignored():
    candles = _candles([(0, 100.0, 101.6, 99.8, 101.2)])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(minutes=15), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert result.status == "PENDING"
    assert result.reason == "insufficient_history"


def test_missing_historical_candle_prevents_false_timeout():
    candles = _candles([
        (0, 100.0, 100.4, 99.8, 100.1),
        (2, 100.1, 100.4, 99.8, 100.1),
    ])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(hours=1), CONFIRMATION_CLOSE + timedelta(minutes=45))

    assert result.status == "PENDING"
    assert result.reason == "insufficient_history"


def test_candle_before_confirmation_close_cannot_trigger_outcome():
    candles = _candles([
        (-1, 100.0, 101.6, 99.8, 101.2),
        (0, 100.0, 100.4, 99.8, 100.1),
        (1, 100.1, 100.4, 99.8, 100.1),
        (2, 100.1, 100.4, 99.8, 100.1),
    ])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(hours=1), CONFIRMATION_CLOSE + timedelta(minutes=45))

    assert result.status == "TIMEOUT"
    assert result.reason == "expiry_reached"


def test_expiry_between_candle_boundaries_excludes_later_open():
    expiry = CONFIRMATION_CLOSE + timedelta(minutes=37)
    candles = _candles([
        (0, 100.0, 100.4, 99.8, 100.1),
        (1, 100.1, 100.4, 99.8, 100.1),
        (2, 100.1, 101.6, 99.8, 101.2),
    ])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(hours=1), expiry)

    assert result.status == "TIMEOUT"
    assert result.closed_at == CONFIRMATION_CLOSE + timedelta(minutes=30)
    assert result.exit_price == 100.1


def test_timeout_uses_final_close_and_gross_leveraged_roi():
    expiry = CONFIRMATION_CLOSE + timedelta(minutes=45)
    candles = _candles([
        (0, 100.0, 100.4, 99.8, 100.2),
        (1, 100.2, 100.4, 99.8, 100.5),
        (2, 100.5, 100.8, 100.2, 100.75),
    ])

    result = _evaluate(_signal("LONG"), candles, CONFIRMATION_CLOSE + timedelta(hours=1), expiry)

    assert result.status == "TIMEOUT"
    assert result.exit_price == 100.75
    assert result.gross_roi_pct == pytest.approx(15.0)


def test_empty_history_is_pending():
    candles = pd.DataFrame(columns=["open", "high", "low", "close"], index=pd.DatetimeIndex([]))

    result = _evaluate(_signal(), candles, CONFIRMATION_CLOSE + timedelta(hours=1), CONFIRMATION_CLOSE + timedelta(hours=2))

    assert isinstance(result, OutcomeResult)
    assert result.status == "PENDING"
    assert result.reason == "insufficient_history"


def test_input_candles_are_not_mutated():
    candles = _candles([(0, 100.0, 101.6, 99.8, 101.2)])
    original = candles.copy(deep=True)

    evaluate_outcome(_signal(), candles, CONFIRMATION_CLOSE + timedelta(minutes=30), CONFIRMATION_CLOSE + timedelta(hours=2))

    pd.testing.assert_frame_equal(candles, original)
