# tests/test_nw_strategy.py
from nw_strategy import _detect_trend_htf, find_swing_low, find_swing_high
from tests.nw_fixtures import make_4h_trend_df, make_swing_fixture


def test_detect_trend_htf_long_when_trending_up():
    df = make_4h_trend_df("LONG")
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) == "LONG"


def test_detect_trend_htf_short_when_trending_down():
    df = make_4h_trend_df("SHORT")
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) == "SHORT"


def test_detect_trend_htf_none_when_insufficient_history():
    df = make_4h_trend_df("LONG", bars=50)
    closed = df.iloc[:-1]
    assert _detect_trend_htf(closed) is None


def test_find_swing_low_detects_known_pivot():
    df = make_swing_fixture(kind="low", fractal_bars=5, pivot_value=95.0)
    end_idx = len(df) - 1
    result = find_swing_low(df, end_idx, lookback=20, fractal_bars=5)
    assert result is not None
    assert abs(result - 95.0) < 1e-9


def test_find_swing_high_detects_known_pivot():
    df = make_swing_fixture(kind="high", fractal_bars=5, pivot_value=105.0)
    end_idx = len(df) - 1
    result = find_swing_high(df, end_idx, lookback=20, fractal_bars=5)
    assert result is not None
    assert abs(result - 105.0) < 1e-9


def test_find_swing_low_returns_none_when_no_fractal_in_lookback():
    # Monotonic decline -- no interior fractal low exists (the minimum is
    # always at the trailing edge, never confirmed by 5 higher bars after
    # it), so nothing should be found within a short lookback.
    import numpy as np
    import pandas as pd
    closes = 100.0 - np.arange(15) * 0.5
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "volume": np.full(15, 1000.0),
    })
    result = find_swing_low(df, end_idx=14, lookback=8, fractal_bars=5)
    assert result is None
