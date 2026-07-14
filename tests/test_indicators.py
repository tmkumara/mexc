import numpy as np
import pandas as pd
import pytest

from strategy import calculate_ema, calculate_rsi, calculate_atr, calculate_supertrend


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    """A clean, noiseless trend series (step>0 up, step<0 down)."""
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_ema_values():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    ema = calculate_ema(series, 3)
    # alpha = 2/(3+1) = 0.5, seed = first value
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    for got, want in zip(ema.tolist(), expected):
        assert got == pytest.approx(want, abs=1e-9)


def test_rsi_uptrend():
    df = _trend_df(40, step=1.0)
    rsi = calculate_rsi(df["close"], 14)
    assert rsi.iloc[-1] > 70.0


def test_rsi_downtrend():
    df = _trend_df(40, step=-1.0)
    rsi = calculate_rsi(df["close"], 14)
    assert rsi.iloc[-1] < 30.0


def test_atr_values():
    df = pd.DataFrame({
        "open":  [100.0, 101.0, 99.0, 102.0],
        "high":  [101.5, 102.0, 101.0, 103.0],
        "low":   [99.5, 100.0, 98.0, 101.0],
        "close": [101.0, 99.0, 102.0, 102.5],
    })
    atr = calculate_atr(df, 3)
    assert not np.isnan(atr.iloc[-1])
    assert atr.iloc[-1] > 0.0


def test_supertrend_bullish_direction():
    df = _trend_df(60, step=0.8)
    st = calculate_supertrend(df, atr_period=10, multiplier=3.0)
    assert st["supertrend_direction"].iloc[-1] == 1


def test_supertrend_bearish_direction():
    df = _trend_df(60, step=-0.8)
    st = calculate_supertrend(df, atr_period=10, multiplier=3.0)
    assert st["supertrend_direction"].iloc[-1] == -1


def test_supertrend_does_not_use_future_data():
    df = _trend_df(60, step=0.8)
    st_full = calculate_supertrend(df, atr_period=10, multiplier=3.0)
    st_partial = calculate_supertrend(df.iloc[:40].copy(), atr_period=10, multiplier=3.0)

    for i in range(40):
        assert st_full["supertrend_direction"].iloc[i] == st_partial["supertrend_direction"].iloc[i]
        assert st_full["supertrend_line"].iloc[i] == pytest.approx(
            st_partial["supertrend_line"].iloc[i], abs=1e-9
        )
