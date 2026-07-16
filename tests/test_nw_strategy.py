# tests/test_nw_strategy.py
from nw_strategy import _detect_trend_htf, find_swing_low, find_swing_high, _detect_nw_signal
from tests.nw_fixtures import make_4h_trend_df, make_swing_fixture, make_1h_nw_df


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


def test_detect_nw_signal_long_fires_on_bullish_flip_with_confirmation():
    df = make_1h_nw_df("LONG")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_nw_signal(closed, "LONG")
    assert ok is True, reason
    assert details["close"] == closed["close"].iloc[-1]
    assert details["swing_level"] is not None
    assert 0.0 <= details["rsi"] <= 100.0


def test_detect_nw_signal_short_fires_on_bearish_flip_with_confirmation():
    df = make_1h_nw_df("SHORT")
    closed = df.iloc[:-1]
    ok, reason, details = _detect_nw_signal(closed, "SHORT")
    assert ok is True, reason


def test_detect_nw_signal_rejects_when_no_flip_on_latest_bar():
    # A pure, unbroken decline never flips -- the latest bar is still
    # falling, not turning up.
    import numpy as np
    import pandas as pd
    closes = 100.0 - np.arange(80) * 0.05
    df = pd.DataFrame({
        "open": closes + 0.01, "high": closes + 0.05, "low": closes - 0.05,
        "close": closes, "volume": np.full(80, 1000.0),
    })
    ok, reason, _ = _detect_nw_signal(df, "LONG")
    assert ok is False
    assert "flip" in reason


def test_detect_nw_signal_rejects_low_volume():
    df = make_1h_nw_df("LONG", volume_mult=0.5)
    closed = df.iloc[:-1]
    ok, reason, _ = _detect_nw_signal(closed, "LONG")
    assert ok is False
    assert "volume" in reason
