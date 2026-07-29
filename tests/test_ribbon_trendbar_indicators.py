import numpy as np
import pandas as pd
import pytest

from strategy import calculate_ema_ribbon, calculate_trend_bar, _detect_ribbon_flip

LENGTHS = (30, 35, 40, 45, 50)
BASELINE = 60


def _flat_df(n: int, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open": np.full(n, price), "high": np.full(n, price + 0.1),
        "low": np.full(n, price - 0.1), "close": np.full(n, price),
        "volume": np.full(n, 1000.0),
    })


def test_ema_ribbon_returns_all_six_series():
    df = _flat_df(80)
    ribbon = calculate_ema_ribbon(df, LENGTHS, BASELINE)
    for col in ("ma1", "ma2", "ma3", "ma4", "ma5", "baseline"):
        assert col in ribbon.columns
        assert len(ribbon[col]) == len(df)
    # A flat series converges every EMA to the same price.
    assert ribbon["ma1"].iloc[-1] == pytest.approx(100.0, abs=1e-6)
    assert ribbon["baseline"].iloc[-1] == pytest.approx(100.0, abs=1e-6)


def test_trend_bar_green_when_candle_above_channel():
    n = 60
    df = _flat_df(n, price=100.0)
    # Push the final candle's entire range far above the trailing PAC.
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [110.0, 111.0, 109.5, 110.5]
    trend_bar = calculate_trend_bar(df, pac_length=50)
    assert trend_bar.iloc[-1] == "green"


def test_trend_bar_red_when_candle_below_channel():
    n = 60
    df = _flat_df(n, price=100.0)
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [90.0, 90.5, 89.0, 89.5]
    trend_bar = calculate_trend_bar(df, pac_length=50)
    assert trend_bar.iloc[-1] == "red"


def test_trend_bar_gray_when_candle_straddles_channel():
    df = _flat_df(60, price=100.0)
    trend_bar = calculate_trend_bar(df, pac_length=50)
    # A flat series never clears its own trailing channel -- always gray.
    assert trend_bar.iloc[-1] == "gray"


def test_trend_bar_does_not_use_future_data():
    n = 60
    df = _flat_df(n, price=100.0)
    df.loc[df.index[-1], ["open", "high", "low", "close"]] = [110.0, 111.0, 109.5, 110.5]
    full = calculate_trend_bar(df, pac_length=50)
    partial = calculate_trend_bar(df.iloc[:40].copy(), pac_length=50)
    for i in range(40):
        assert full.iloc[i] == partial.iloc[i]


def test_detect_ribbon_flip_finds_recent_bullish_flip():
    # 71 flat bars (long enough for every EMA to fully converge), then a
    # short 8-bar push -- from a fully-converged flat state, all 6 EMAs
    # separate in alpha order (fastest reacts most) on the very first
    # pushed bar, so the flip lands right at the push's start and a short
    # push keeps it comfortably inside the 12-bar lookback by series end.
    n = 79
    closes = np.full(n, 100.0)
    closes[71:] = 100.0 + np.arange(1, 9) * 3.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction == "LONG"
    assert flip_index is not None
    assert flip_index >= n - 1 - 12


def test_detect_ribbon_flip_finds_recent_bearish_flip():
    n = 79
    closes = np.full(n, 100.0)
    closes[71:] = 100.0 - np.arange(1, 9) * 3.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction == "SHORT"
    assert flip_index is not None


def test_detect_ribbon_flip_rejects_when_flip_outside_lookback_window():
    # Same bullish setup as above, but query with lookback_bars=1 -- the
    # flip (which needs several bars of the push to complete) will be
    # older than a 1-bar window even though the ribbon IS currently aligned.
    n = 80
    closes = np.full(n, 100.0)
    closes[60:] = 100.0 + np.arange(1, 21) * 3.0
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=1)
    assert direction is None


def test_detect_ribbon_flip_rejects_when_ribbon_not_currently_aligned():
    df = _flat_df(80, price=100.0)
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction is None
    assert flip_index is None


def test_detect_ribbon_flip_finds_latest_flip_after_a_revert():
    # Flip bullish, revert to bearish, flip bullish again -- must return
    # the SECOND flip's index, not the first. No flat tail after the
    # second push: reversing an already-established opposite alignment
    # (unlike the very first flip from full convergence) takes many bars
    # to first close the gap and then re-separate, so the series ends
    # right at the push -- a flat tail would let the ribbon re-converge
    # and erase the very flip this test is checking for.
    n = 130
    closes = np.full(n, 100.0)
    closes[40:70] = 100.0 + np.arange(1, 31) * 3.0        # first bullish push
    closes[70:100] = closes[69] - np.arange(1, 31) * 3.0  # revert bearish
    closes[100:n] = closes[99] + np.arange(1, 31) * 3.0   # second bullish push, series ends here
    df = pd.DataFrame({
        "open": closes, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": np.full(n, 1000.0),
    })
    direction, flip_index = _detect_ribbon_flip(df, LENGTHS, BASELINE, lookback_bars=12)
    assert direction == "LONG"
    # The second flip must be found -- well past the midpoint of the series.
    assert flip_index > 100
