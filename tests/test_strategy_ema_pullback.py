import pandas as pd
import strategy_ema_pullback as ema_strategy
import pytest

from ema_indicators import calculate_atr, calculate_ema, calculate_rsi, find_confirmed_pivots
from strategy_ema_pullback import evaluate_ema_pullback


def _candles(closes, timeframe):
    index = pd.date_range("2026-01-01", periods=len(closes), freq=timeframe)
    close = pd.Series(closes, index=index, dtype=float)
    previous = close.shift(1).fillna(close)
    return pd.DataFrame(
        {
            "open": previous,
            "high": close.combine(previous, max) + 0.05,
            "low": close.combine(previous, min) - 0.05,
            "close": close,
            "volume": 1000.0,
        },
        index=index,
    )


def _fixture(direction="long"):
    if direction == "long":
        hourly_close = [100 + i * 0.2 for i in range(260)]
        quarter_close = [100 + i * 0.05 for i in range(1000)]
        quarter_close[-10:] = [149.50, 149.20, 148.90, 148.60, 148.30, 148.45, 148.60, 148.75, 148.75, 149.40]
    else:
        hourly_close = [200 - i * 0.2 for i in range(260)]
        quarter_close = [200 - i * 0.05 for i in range(1000)]
        quarter_close[-10:] = [150.50, 150.80, 151.10, 151.40, 151.70, 151.55, 151.40, 151.25, 151.30, 150.60]

    hourly = _candles(hourly_close, "1h")
    quarter = _candles(quarter_close, "15min")
    if direction == "long":
        quarter.loc[quarter.index[-6], "low"] = 148.10
    else:
        quarter.loc[quarter.index[-6], "high"] = 151.80
    evaluation = quarter.index[-1] + pd.Timedelta(minutes=15, seconds=90)
    return hourly, quarter, evaluation


def _assert_long_preconditions(hourly, quarter, evaluation):
    usable_hourly = hourly.loc[hourly.index + pd.Timedelta(hours=1) <= quarter.index[-1] + pd.Timedelta(minutes=15)]
    ema50 = calculate_ema(usable_hourly["close"], 50)
    ema200 = calculate_ema(usable_hourly["close"], 200)
    ema20_15 = calculate_ema(quarter["close"], 20)
    ema50_15 = calculate_ema(quarter["close"], 50)
    rsi = calculate_rsi(quarter["close"], 14)
    atr = calculate_atr(quarter, 14)
    pivots = find_confirmed_pivots(quarter, cutoff=quarter.index[-1])

    assert ema50.iloc[-1] > ema200.iloc[-1]
    assert usable_hourly["close"].iloc[-1] > ema50.iloc[-1]
    assert usable_hourly["close"].iloc[-2] > ema50.iloc[-2]
    assert ema20_15.iloc[-1] > ema50_15.iloc[-1]
    assert rsi.iloc[-10:-1].min() <= 45
    assert rsi.iloc[-2] <= 50 < rsi.iloc[-1]
    assert atr.iloc[-1] > 0
    assert not pivots[pivots["type"] == "low"].empty


def test_valid_long_candidate_contains_contract_fields():
    hourly, quarter, evaluation = _fixture("long")
    _assert_long_preconditions(hourly, quarter, evaluation)

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result["accepted"] is True
    assert result["direction"] == "LONG"
    assert result["entry"] == quarter["close"].iloc[-1]
    assert result["tp"] == result["entry"] * 1.015
    assert result["leverage"] == 20
    assert result["gross_tp_roi_pct"] == pytest.approx(30.0)
    assert result["rr"] >= 1.5
    assert result["confirmation_candle_open"].tzinfo is not None
    assert result["confirmation_close_at"] == result["confirmation_candle_open"] + pd.Timedelta(minutes=15)
    assert result["pivot_timestamp"].tzinfo is not None
    assert result["rsi_reset_timestamp"].tzinfo is not None
    assert result["pivot_timestamp"] < result["confirmation_candle_open"]
    assert result["rsi_reset_timestamp"] < result["confirmation_candle_open"]
    assert result["setup_key"].startswith("ema_trend_pullback|v1|TEST|LONG|")


def test_valid_short_candidate_is_symmetric():
    hourly, quarter, evaluation = _fixture("short")

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result["accepted"] is True
    assert result["direction"] == "SHORT"
    assert result["tp"] == pytest.approx(result["entry"] * 0.985)
    assert result["gross_tp_roi_pct"] == pytest.approx(30.0)
    assert result["rr"] >= 1.5


def test_invalid_one_hour_trend_is_rejected():
    hourly, quarter, evaluation = _fixture("long")
    hourly.loc[hourly.index[-50:-10], "close"] = list(reversed([90 + i * 0.01 for i in range(40)]))

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "trend_not_aligned"}


def test_missing_ema_pullback_is_rejected():
    hourly, quarter, evaluation = _fixture("long")
    quarter.loc[quarter.index[-9:-1], "low"] = 160.0

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "no_valid_ema_pullback"}


def test_missing_rsi_reset_is_rejected():
    hourly, quarter, evaluation = _fixture("long")
    quarter.loc[quarter.index[-9:-1], "close"] = [149.50, 149.47, 149.44, 149.41, 149.38, 149.40, 149.43, 149.46]

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "rsi_reset_missing"}


def test_invalid_confirmation_candle_is_rejected():
    hourly, quarter, evaluation = _fixture("long")
    quarter.loc[quarter.index[-1], "open"] = 150.0

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "confirmation_invalid"}


def test_structural_risk_too_wide_is_rejected():
    hourly, quarter, evaluation = _fixture("long")
    for offset in (-10, -9, -8, -7, -6, -4, -3):
        quarter.loc[quarter.index[offset], "low"] = 149.5
    quarter.loc[quarter.index[-10], "low"] = 148.8
    quarter.loc[quarter.index[-9], "low"] = 148.8
    quarter.loc[quarter.index[-5], "low"] = 140.0

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "risk_too_wide"}


def test_structural_risk_too_tight_is_rejected():
    hourly, quarter, evaluation = _fixture("long")
    for offset in (-10, -9, -8, -7, -6, -4, -3):
        quarter.loc[quarter.index[offset], "low"] = 149.5
    quarter.loc[quarter.index[-10], "low"] = 148.8
    quarter.loc[quarter.index[-9], "low"] = 148.8
    quarter.loc[quarter.index[-5], "low"] = 149.35

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "risk_too_tight"}


def test_forming_final_candle_is_ignored_explicitly():
    hourly, quarter, evaluation = _fixture("long")
    before_settlement = quarter.index[-1] + pd.Timedelta(minutes=15, seconds=89)

    result = evaluate_ema_pullback("TEST", quarter, hourly, before_settlement)

    assert result == {"accepted": False, "reason": "forming_candle_excluded"}


def test_unconfirmed_pivot_is_not_used():
    hourly, quarter, evaluation = _fixture("long")
    quarter.loc[quarter.index[-10:-1], "low"] = 149.2
    quarter.loc[quarter.index[-2], "low"] = 148.8

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "pivot_missing"}


def test_future_one_hour_candle_cannot_supply_trend_alignment():
    hourly, quarter, evaluation = _fixture("long")
    hourly.loc[hourly.index[248:250], "close"] = [90.0, 90.1]
    hourly.loc[hourly.index[250], "close"] = 1000.0

    result = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert result == {"accepted": False, "reason": "trend_not_aligned"}


def test_setup_key_is_deterministic_across_identical_evaluations():
    hourly, quarter, evaluation = _fixture("long")

    first = evaluate_ema_pullback("TEST", quarter, hourly, evaluation)
    second = evaluate_ema_pullback("TEST", quarter.copy(), hourly.copy(), evaluation)

    assert first["setup_key"] == second["setup_key"]


def test_different_confirmation_candle_changes_setup_key():
    hourly, quarter, evaluation = _fixture("long")
    first = ema_strategy.evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    shifted = quarter.copy()
    shifted.index = shifted.index + pd.Timedelta(minutes=15)
    second = ema_strategy.evaluate_ema_pullback(
        "TEST", shifted, hourly, evaluation + pd.Timedelta(minutes=15)
    )

    assert first["accepted"] is True
    assert second["accepted"] is True
    assert first["setup_key"] != second["setup_key"]


def test_different_strategy_version_changes_setup_key(monkeypatch):
    hourly, quarter, evaluation = _fixture("long")
    first = ema_strategy.evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    monkeypatch.setattr(ema_strategy, "STRATEGY_VERSION", "v2")
    second = ema_strategy.evaluate_ema_pullback("TEST", quarter, hourly, evaluation)

    assert first["setup_key"] != second["setup_key"]


def test_long_and_short_setup_identities_differ():
    long_hourly, long_quarter, long_eval = _fixture("long")
    short_hourly, short_quarter, short_eval = _fixture("short")

    long_result = ema_strategy.evaluate_ema_pullback("TEST", long_quarter, long_hourly, long_eval)
    short_result = ema_strategy.evaluate_ema_pullback("TEST", short_quarter, short_hourly, short_eval)

    assert long_result["setup_key"] != short_result["setup_key"]
