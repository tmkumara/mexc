import numpy as np
import pandas as pd
import pytest

from strategy import calculate_zlema, calculate_zlema_band, calculate_zlema_trend_state


def _trend_df(n: int, step: float, start: float = 100.0) -> pd.DataFrame:
    closes = start + np.arange(n) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.full(n, 1000.0),
    })


def test_zlema_matches_hand_computed_construction():
    # length=5 -> lag = floor((5-1)/2) = 2
    closes = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0])
    zlema = calculate_zlema(closes, length=5)

    lag = 2
    adjusted = 2 * closes - closes.shift(lag)
    expected = adjusted.ewm(span=5, adjust=False).mean()

    pd.testing.assert_series_equal(zlema, expected, check_names=False)


def test_zlema_lag_floor_division():
    # length=70 -> lag = floor((70-1)/2) = 34, matches architecture.txt exactly
    closes = pd.Series(np.arange(100, dtype=float))
    zlema = calculate_zlema(closes, length=70)
    adjusted = 2 * closes - closes.shift(34)
    expected = adjusted.ewm(span=70, adjust=False).mean()
    pd.testing.assert_series_equal(zlema, expected, check_names=False)


def test_zlema_band_widens_with_multiplier():
    df = _trend_df(250, step=0.3)
    zlema = calculate_zlema(df["close"], length=70)
    upper_1x, lower_1x = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=0.1)
    upper_2x, lower_2x = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=0.2)

    i = -1
    assert (upper_2x.iloc[i] - zlema.iloc[i]) == pytest.approx(2 * (upper_1x.iloc[i] - zlema.iloc[i]), rel=1e-6)
    assert (zlema.iloc[i] - lower_2x.iloc[i]) == pytest.approx(2 * (zlema.iloc[i] - lower_1x.iloc[i]), rel=1e-6)


def test_trend_state_starts_neutral_then_flips_on_cross_above():
    df = _trend_df(250, step=0.0)  # flat until the breakout below
    df.loc[df.index[-30:], "close"] = df["close"].iloc[-31] + np.linspace(0, 50, 30)
    df.loc[df.index[-30:], "high"] = df.loc[df.index[-30:], "close"] + 0.2
    df.loc[df.index[-30:], "low"] = df.loc[df.index[-30:], "close"] - 0.2

    zlema = calculate_zlema(df["close"], length=70)
    upper, lower = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=0.2)
    state = calculate_zlema_trend_state(df, zlema, upper, lower)

    assert state.iloc[0] == 0
    assert state.iloc[-1] == 1


def test_trend_state_holds_through_a_dip_that_does_not_cross_opposite_band():
    df = _trend_df(300, step=0.4)  # strong steady uptrend
    zlema = calculate_zlema(df["close"], length=70)
    upper, lower = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=0.2)
    state = calculate_zlema_trend_state(df, zlema, upper, lower)

    flip_idx = state[state == 1].index[0]
    flip_pos = df.index.get_loc(flip_idx)
    # dip the very next bar back toward zlema without crossing the lower band
    df.loc[df.index[flip_pos + 1], "close"] = float(zlema.iloc[flip_pos + 1])
    zlema2 = calculate_zlema(df["close"], length=70)
    upper2, lower2 = calculate_zlema_band(df, zlema2, atr_period=70, atr_lookback=210, multiplier=0.2)
    state2 = calculate_zlema_trend_state(df, zlema2, upper2, lower2)

    assert state2.iloc[flip_pos + 1] == 1  # held, did not reset to 0 or flip to -1


def test_trend_state_flips_to_bearish_on_cross_below():
    df = _trend_df(250, step=-0.4)
    zlema = calculate_zlema(df["close"], length=70)
    upper, lower = calculate_zlema_band(df, zlema, atr_period=70, atr_lookback=210, multiplier=0.2)
    state = calculate_zlema_trend_state(df, zlema, upper, lower)
    assert state.iloc[-1] == -1
