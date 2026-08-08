from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import config
import strategy
from tests.strategy_fixtures import make_trend_df


def test_calculate_volume_ma_simple_rolling_mean():
    df = pd.DataFrame({"volume": [10.0, 20.0, 30.0, 40.0, 50.0]})
    vol_ma = strategy.calculate_volume_ma(df, period=3)
    assert round(float(vol_ma.iloc[-1]), 4) == round((30.0 + 40.0 + 50.0) / 3, 4)


def test_ema_trend_slope_up_true_for_uptrend():
    df = make_trend_df("LONG", bars=260, freq="15min").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    assert strategy._ema_trend_slope_up(ema_trend, config.EMA_TREND_SLOPE_LOOKBACK) is True


def test_ema_trend_slope_up_false_for_downtrend():
    df = make_trend_df("SHORT", bars=260, freq="15min").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    assert strategy._ema_trend_slope_up(ema_trend, config.EMA_TREND_SLOPE_LOOKBACK) is False


def test_rsi_reset_long_true_when_in_zone_and_turning_up():
    rsi = pd.Series([70, 60, 50, 48, 45, 47])
    assert strategy._rsi_reset_ok("LONG", rsi, lookback=5) is True


def test_rsi_reset_long_false_when_never_in_zone():
    rsi = pd.Series([70, 68, 65, 63, 62, 64])
    assert strategy._rsi_reset_ok("LONG", rsi, lookback=5) is False


def test_rsi_reset_long_false_when_still_falling():
    rsi = pd.Series([70, 60, 50, 48, 45, 43])
    assert strategy._rsi_reset_ok("LONG", rsi, lookback=5) is False


def test_rsi_reset_short_true_when_in_zone_and_turning_down():
    rsi = pd.Series([30, 40, 50, 52, 55, 53])
    assert strategy._rsi_reset_ok("SHORT", rsi, lookback=5) is True


def _two_candle_df(prev: dict, last: dict) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=2, freq="5min")
    return pd.DataFrame({
        "open":   [prev["open"],   last["open"]],
        "high":   [prev["high"],   last["high"]],
        "low":    [prev["low"],    last["low"]],
        "close":  [prev["close"],  last["close"]],
        "volume": [prev["volume"], last["volume"]],
    }, index=idx)


def test_confirmation_candle_long_passes():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 101.2, "low": 100.1, "close": 101.0, "volume": 1300.0},
    )
    ema20 = pd.Series([100.0, 100.3])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("LONG", df, ema20, vol_ma) is True


def test_confirmation_candle_long_fails_low_volume():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 101.2, "low": 100.1, "close": 101.0, "volume": 1050.0},
    )
    ema20 = pd.Series([100.0, 100.3])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("LONG", df, ema20, vol_ma) is False


def test_confirmation_candle_long_fails_does_not_close_above_prior_high():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 101.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 101.2, "low": 100.1, "close": 101.0, "volume": 1300.0},
    )
    ema20 = pd.Series([100.0, 100.3])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("LONG", df, ema20, vol_ma) is False


def test_confirmation_candle_short_passes():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 99.8, "volume": 1000.0},
        last={"open": 99.8, "high": 99.9, "low": 98.8, "close": 99.0, "volume": 1300.0},
    )
    ema20 = pd.Series([100.0, 99.7])
    vol_ma = pd.Series([1000.0, 1000.0])
    assert strategy._confirmation_candle_ok("SHORT", df, ema20, vol_ma) is True


def test_abnormal_candle_true_when_body_too_large():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.0, "high": 101.5, "low": 99.0, "close": 101.0, "volume": 1300.0},
    )
    assert strategy._abnormal_candle(df) is True


def test_abnormal_candle_false_when_body_normal():
    df = _two_candle_df(
        prev={"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 1000.0},
        last={"open": 100.2, "high": 100.6, "low": 100.0, "close": 100.4, "volume": 1300.0},
    )
    assert strategy._abnormal_candle(df) is False


def test_atr_pct_ok_within_band():
    assert strategy._atr_pct_ok(atr_last=0.5, close=100.0) is True


def test_atr_pct_ok_too_low():
    assert strategy._atr_pct_ok(atr_last=0.1, close=100.0) is False


def test_atr_pct_ok_too_high():
    assert strategy._atr_pct_ok(atr_last=1.5, close=100.0) is False


from tests.strategy_fixtures import make_pullback_confirmation_df


def test_score_pending_setup_within_bounds_and_passes_gate_long():
    df = make_pullback_confirmation_df("LONG").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    vol_ma = strategy.calculate_volume_ma(df, config.VOLUME_MA_PERIOD)
    ema20 = strategy.calculate_ema(df["close"], config.EMA_FAST_LEN)
    close = float(df["close"].iloc[-1])
    distance_pct = abs(close - float(ema20.iloc[-1])) / close

    score = strategy._score_pending_setup(
        "LONG", df, ema_trend, config.EMA_TREND_SLOPE_LOOKBACK, distance_pct, vol_ma
    )

    assert 0.0 <= score <= 100.0
    assert score >= config.MIN_SIGNAL_SCORE


def test_score_lower_when_pullback_distance_larger():
    df = make_pullback_confirmation_df("LONG").iloc[:-1]
    ema_trend = strategy.calculate_ema(df["close"], config.EMA_TREND_LEN)
    vol_ma = strategy.calculate_volume_ma(df, config.VOLUME_MA_PERIOD)

    score_tight = strategy._score_pending_setup(
        "LONG", df, ema_trend, config.EMA_TREND_SLOPE_LOOKBACK, 0.0005, vol_ma
    )
    score_wide = strategy._score_pending_setup(
        "LONG", df, ema_trend, config.EMA_TREND_SLOPE_LOOKBACK, 0.0025, vol_ma
    )

    assert score_tight > score_wide
