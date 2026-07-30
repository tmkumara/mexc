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
