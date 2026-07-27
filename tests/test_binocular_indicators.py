import numpy as np
import pandas as pd
import pytest

from strategy import (
    calculate_chandelier_exit,
    calculate_pvt,
    calculate_pvt_signal,
    find_pivot_highs,
    find_pivot_lows,
)


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_chandelier_exit_bullish_direction():
    df = _trend_df(60, step=0.8)
    chand = calculate_chandelier_exit(df, atr_period=10, multiplier=2.2)
    assert chand["chandelier_direction"].iloc[-1] == 1


def test_chandelier_exit_bearish_direction():
    df = _trend_df(60, step=-0.8)
    chand = calculate_chandelier_exit(df, atr_period=10, multiplier=2.2)
    assert chand["chandelier_direction"].iloc[-1] == -1


def test_chandelier_exit_does_not_use_future_data():
    df = _trend_df(60, step=0.8)
    full = calculate_chandelier_exit(df, atr_period=10, multiplier=2.2)
    partial = calculate_chandelier_exit(df.iloc[:40].copy(), atr_period=10, multiplier=2.2)
    for i in range(40):
        assert full["chandelier_direction"].iloc[i] == partial["chandelier_direction"].iloc[i]
        assert full["chandelier_long_stop"].iloc[i] == pytest.approx(
            partial["chandelier_long_stop"].iloc[i], abs=1e-9
        )


def test_pvt_accumulates_correctly():
    df = pd.DataFrame({
        "close": [100.0, 110.0, 99.0],
        "volume": [1000.0, 1000.0, 1000.0],
    })
    pvt = calculate_pvt(df)
    assert pvt.iloc[0] == pytest.approx(0.0, abs=1e-9)
    assert pvt.iloc[1] == pytest.approx(100.0, abs=1e-9)
    assert pvt.iloc[2] == pytest.approx(0.0, abs=1e-6)


def test_pvt_signal_sma():
    pvt = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="SMA")
    expected = [1.0, 1.5, 2.0, 3.0, 4.0]
    for got, want in zip(signal.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_pvt_signal_ema():
    pvt = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    signal = calculate_pvt_signal(pvt, length=3, ma_type="EMA")
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    for got, want in zip(signal.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_pivot_high_detection():
    n = 7
    df = pd.DataFrame({
        "high":  [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0],
        "low":   [0.5, 1.5, 2.5, 9.5, 2.5, 1.5, 0.5],
        "close": [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0],
        "open":  [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0],
        "volume": [1000.0] * n,
    })
    pivots = find_pivot_highs(df, swing_length=3)
    assert pivots.iloc[3] == pytest.approx(10.0)
    assert pivots.drop(index=3).isna().all()


def test_pivot_low_detection():
    n = 7
    df = pd.DataFrame({
        "high":  [10.5, 9.5, 8.5, 2.0, 8.5, 9.5, 10.5],
        "low":   [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0],
        "close": [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0],
        "open":  [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0],
        "volume": [1000.0] * n,
    })
    pivots = find_pivot_lows(df, swing_length=3)
    assert pivots.iloc[3] == pytest.approx(1.0)
    assert pivots.drop(index=3).isna().all()
