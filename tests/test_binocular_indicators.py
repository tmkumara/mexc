import numpy as np
import pandas as pd
import pytest

from strategy import calculate_pvt, calculate_pvt_signal


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    """A clean, noiseless trend series (step>0 up, step<0 down) -- same
    shape as tests/test_indicators.py's private helper of the same name."""
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_pvt_accumulates_correctly():
    df = pd.DataFrame({
        "close": [100.0, 102.0, 101.0, 103.0],
        "volume": [1000.0, 1500.0, 1200.0, 1800.0],
    })
    pvt = calculate_pvt(df)
    assert pvt.iloc[0] == pytest.approx(0.0)
    assert pvt.iloc[1] == pytest.approx(30.0)
    expected_2 = 30.0 + 1200.0 * (101.0 - 102.0) / 102.0
    assert pvt.iloc[2] == pytest.approx(expected_2)
    expected_3 = expected_2 + 1800.0 * (103.0 - 101.0) / 101.0
    assert pvt.iloc[3] == pytest.approx(expected_3)


def test_pvt_signal_sma():
    pvt = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="SMA")
    assert signal.iloc[2] == pytest.approx((10.0 + 20.0 + 30.0) / 3)
    assert signal.iloc[4] == pytest.approx((30.0 + 40.0 + 50.0) / 3)


def test_pvt_signal_ema():
    pvt = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="EMA")
    # alpha = 2/(3+1) = 0.5, seed = first value (matches calculate_ema's convention)
    expected = [10.0, 15.0, 22.5, 31.25, 40.625]
    for got, want in zip(signal.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)


from strategy import calculate_chandelier_direction


def test_chandelier_direction_bullish():
    df = _trend_df(40, step=1.0)
    direction, _, _ = calculate_chandelier_direction(df, atr_period=10, multiplier=2.2)
    assert direction.iloc[-1] == 1


def test_chandelier_direction_bearish():
    df = _trend_df(40, step=-1.0)
    direction, _, _ = calculate_chandelier_direction(df, atr_period=10, multiplier=2.2)
    assert direction.iloc[-1] == -1


def test_chandelier_uses_previous_bar_stop_for_comparison():
    df = _trend_df(30, step=1.0)
    direction, long_stop_prev, short_stop_prev = calculate_chandelier_direction(
        df, atr_period=10, multiplier=2.2
    )
    for i in range(1, len(df)):
        close_i = df["close"].iloc[i]
        if close_i > short_stop_prev.iloc[i]:
            expected = 1
        elif close_i < long_stop_prev.iloc[i]:
            expected = -1
        else:
            expected = direction.iloc[i - 1]
        assert direction.iloc[i] == expected


def test_chandelier_does_not_use_future_data():
    df = _trend_df(40, step=1.0)
    dir_full, _, _ = calculate_chandelier_direction(df, atr_period=10, multiplier=2.2)
    dir_partial, _, _ = calculate_chandelier_direction(df.iloc[:25].copy(), atr_period=10, multiplier=2.2)
    for i in range(25):
        assert dir_full.iloc[i] == dir_partial.iloc[i]


from strategy import calculate_ema200, calculate_daily_vwap, calculate_ema


def test_ema200_matches_calculate_ema():
    df = _trend_df(210, step=0.5)
    ema200 = calculate_ema200(df, 200)
    expected = calculate_ema(df["close"], 200)
    pd.testing.assert_series_equal(ema200, expected)


def test_daily_vwap_resets_at_session_boundary():
    idx = pd.date_range("2026-01-01", periods=4, freq="12h")  # 2 candles/day, 2 days
    df = pd.DataFrame({
        "high":  [101.0, 103.0, 201.0, 203.0],
        "low":   [99.0, 101.0, 199.0, 201.0],
        "close": [100.0, 102.0, 200.0, 202.0],
        "volume": [10.0, 10.0, 10.0, 10.0],
    }, index=idx)
    vwap = calculate_daily_vwap(df)
    day1_vwap_bar2 = (100.0 * 10 + 102.0 * 10) / (10 + 10)
    assert vwap.iloc[1] == pytest.approx(day1_vwap_bar2)
    day2_typical_bar1 = (201.0 + 199.0 + 200.0) / 3
    assert vwap.iloc[2] == pytest.approx(day2_typical_bar1)


import strategy
from strategy import calculate_binocular_trigger, detect_transition


def test_detect_transition_new_buy():
    trigger = pd.DataFrame({"buy": [False, False, True], "sell": [False, False, False]})
    assert detect_transition(trigger) == "LONG"


def test_detect_transition_new_sell():
    trigger = pd.DataFrame({"buy": [False, False, False], "sell": [False, False, True]})
    assert detect_transition(trigger) == "SHORT"


def test_detect_transition_no_change_returns_none():
    trigger = pd.DataFrame({"buy": [False, True, True], "sell": [False, False, False]})
    assert detect_transition(trigger) is None


def _noisy_trend_df(n: int, step: float, start: float = 100.0, amp: float = 2.5, period: int = 9) -> pd.DataFrame:
    """A trending series with a sine wobble large enough to produce
    genuine down-ticks (not just a monotonic climb) -- a perfectly
    straight-line trend saturates RSI to ~100 for *every* period (no
    down days at all), so RSI_FAST and RSI_SLOW converge to identical
    values instead of separating. The wobble here is sized so the
    Chandelier direction and PVT-vs-signal still land net bullish/
    bearish, while RSI(25) genuinely reacts faster than RSI(55) to the
    recent leg. Numeric constants here are reasoned, not hand-executed
    against pandas -- same convention as tests/strategy_fixtures.py."""
    sign = 1.0 if step >= 0 else -1.0
    t = np.arange(n)
    closes = start + t * step + sign * amp * np.sin(t * 2 * np.pi / period)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_calculate_binocular_trigger_strong_uptrend_eventually_buys(monkeypatch):
    monkeypatch.setattr(strategy, "CHANDELIER_ATR_PERIOD", 10)
    monkeypatch.setattr(strategy, "CHANDELIER_MULTIPLIER", 2.2)
    monkeypatch.setattr(strategy, "PVT_SIGNAL_LENGTH", 21)
    monkeypatch.setattr(strategy, "PVT_SIGNAL_TYPE", "SMA")
    monkeypatch.setattr(strategy, "RSI_FAST_PERIOD", 25)
    monkeypatch.setattr(strategy, "RSI_SLOW_PERIOD", 55)
    df = _noisy_trend_df(220, step=1.0)
    trigger = calculate_binocular_trigger(df)
    assert bool(trigger["buy"].iloc[-1]) is True
    assert bool(trigger["sell"].iloc[-1]) is False
