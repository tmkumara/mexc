from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import ema_signal
from ema_signal import EmaSignal, to_ema_signal


def _evaluation(direction="LONG"):
    if direction == "LONG":
        entry, tp, sl = 100.0, 101.5, 99.0
    else:
        entry, tp, sl = 100.0, 98.5, 101.0
    return {
        "accepted": True,
        "symbol": "TEST_USDT",
        "direction": direction,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "leverage": 20,
        "gross_tp_roi_pct": 30.0,
        "gross_sl_roi_pct": 20.0,
        "rr": 1.5,
        "strategy_id": "ema_trend_pullback",
        "strategy_version": "v1",
        "confirmation_candle_open": pd.Timestamp("2026-01-02 12:00:00"),
        "confirmation_close_at": pd.Timestamp("2026-01-02 12:15:00"),
        "pivot_timestamp": pd.Timestamp("2026-01-02 11:15:00"),
        "rsi_reset_timestamp": pd.Timestamp("2026-01-02 11:30:00"),
        "setup_key": "ema_trend_pullback|v1|TEST_USDT|LONG|2026-01-02T12:15:00+00:00|2026-01-02T11:15:00+00:00|2026-01-02T11:30:00+00:00",
    }


def test_valid_long_conversion_returns_frozen_ema_signal():
    generated_at = datetime(2026, 1, 2, 12, 2, tzinfo=timezone.utc)

    signal = to_ema_signal(_evaluation("LONG"), generated_at)

    assert isinstance(signal, EmaSignal)
    assert signal.direction == "LONG"
    assert signal.entry_price == 100.0
    assert signal.tp_price == 101.5
    assert signal.sl_price == 99.0
    with pytest.raises(Exception):
        signal.entry_price = 101.0


def test_valid_short_conversion_is_symmetric():
    signal = to_ema_signal(_evaluation("SHORT"), datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert signal.direction == "SHORT"
    assert signal.tp_price < signal.entry_price < signal.sl_price
    assert signal.strategy_id == "ema_trend_pullback"
    assert signal.strategy_version == "v1"


def test_rejected_evaluation_is_not_convertible():
    result = _evaluation()
    result["accepted"] = False

    with pytest.raises(ValueError, match="accepted"):
        to_ema_signal(result, datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_missing_required_field_is_rejected():
    result = _evaluation()
    del result["setup_key"]

    with pytest.raises(ValueError, match="setup_key"):
        to_ema_signal(result, datetime(2026, 1, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "direction,entry,tp,sl",
    [
        ("LONG", 100.0, 99.0, 98.0),
        ("LONG", 100.0, 101.0, 101.0),
        ("SHORT", 100.0, 101.0, 99.0),
        ("SHORT", 100.0, 99.0, 99.0),
    ],
)
def test_invalid_price_geometry_is_rejected(direction, entry, tp, sl):
    result = _evaluation(direction)
    result.update(entry=entry, tp=tp, sl=sl)

    with pytest.raises(ValueError, match="geometry"):
        to_ema_signal(result, datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_timestamps_are_normalized_to_utc():
    result = _evaluation()
    result["confirmation_candle_open"] = pd.Timestamp("2026-01-02 12:00:00+05:30")
    result["confirmation_close_at"] = pd.Timestamp("2026-01-02 12:15:00+05:30")
    result["pivot_timestamp"] = pd.Timestamp("2026-01-02 11:15:00+05:30")
    result["rsi_reset_timestamp"] = pd.Timestamp("2026-01-02 11:30:00+05:30")

    signal = to_ema_signal(result, datetime(2026, 1, 2, 12, tzinfo=timezone(timedelta(hours=5, minutes=30))))

    assert signal.confirmation_candle_open == datetime(2026, 1, 2, 6, 30, tzinfo=timezone.utc)
    assert signal.confirmation_close_at == datetime(2026, 1, 2, 6, 45, tzinfo=timezone.utc)
    assert signal.pivot_timestamp.tzinfo == timezone.utc
    assert signal.generated_at == datetime(2026, 1, 2, 6, 30, tzinfo=timezone.utc)


def test_confirmation_close_is_fifteen_minutes_after_open():
    signal = to_ema_signal(_evaluation(), datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert signal.confirmation_close_at == signal.confirmation_candle_open + timedelta(minutes=15)


def test_converter_does_not_double_add_confirmation_duration():
    result = _evaluation()
    result["confirmation_close_at"] = pd.Timestamp("2026-01-02 12:15:00")

    signal = to_ema_signal(result, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert signal.confirmation_close_at == datetime(2026, 1, 2, 12, 15, tzinfo=timezone.utc)


def test_explicit_generated_at_is_preserved_not_current_clock():
    generated_at = datetime(2020, 5, 6, 7, 8, 9, tzinfo=timezone.utc)

    signal = to_ema_signal(_evaluation(), generated_at)

    assert signal.generated_at == generated_at


def test_setup_key_and_roi_values_are_preserved_exactly():
    result = _evaluation()
    result["gross_tp_roi_pct"] = 30.125
    result["gross_sl_roi_pct"] = 20.875
    result["rr"] = 1.503

    signal = to_ema_signal(result, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert signal.setup_key == result["setup_key"]
    assert signal.tp_roi_pct == 30.125
    assert signal.sl_roi_pct == 20.875
    assert signal.rr == 1.503


def test_model_has_no_dependency_on_existing_smc_signal():
    assert "strategy" not in ema_signal.__dict__
    assert not hasattr(EmaSignal, "score")
