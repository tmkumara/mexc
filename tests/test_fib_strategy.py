import numpy as np
import pandas as pd

import fib_strategy
from fib_strategy import (
    FibLeg, find_swings, find_last_impulse_leg,
    fib_retracement_price, fib_extension_price,
)
from tests.fib_fixtures import make_1h_fib_df


def test_fractal_swing_detection_finds_known_pivots():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    # The rise segment peaks at story index 19 (leg_end); the decline
    # segment bottoms at story index 9 (leg_start). Story starts at
    # len(closed) - 40.
    story_start = len(closed) - 40
    leg_start_pos = story_start + 9
    leg_end_pos = story_start + 19

    assert bool(swings["swing_low"].iloc[leg_start_pos]) is True
    assert bool(swings["swing_high"].iloc[leg_end_pos]) is True


def test_fib_levels_computed_correctly_for_known_leg():
    # LONG leg: 90 -> 110, span 20.
    price_0 = fib_retracement_price(90.0, 110.0, "LONG", 0.0)
    price_50 = fib_retracement_price(90.0, 110.0, "LONG", 0.5)
    price_618 = fib_retracement_price(90.0, 110.0, "LONG", 0.618)
    price_100 = fib_retracement_price(90.0, 110.0, "LONG", 1.0)

    assert price_0 == 110.0
    assert price_50 == 100.0
    assert abs(price_618 - 97.64) < 1e-9
    assert price_100 == 90.0

    ext_1272 = fib_extension_price(90.0, 110.0, "LONG", 1.272)
    assert abs(ext_1272 - 115.44) < 1e-9


def test_fib_levels_computed_correctly_for_short_leg():
    # SHORT leg: 110 -> 90, span 20 (leg_start=110 high, leg_end=90 low).
    price_0 = fib_retracement_price(110.0, 90.0, "SHORT", 0.0)
    price_50 = fib_retracement_price(110.0, 90.0, "SHORT", 0.5)
    price_100 = fib_retracement_price(110.0, 90.0, "SHORT", 1.0)

    assert price_0 == 90.0
    assert price_50 == 100.0
    assert price_100 == 110.0

    ext_1272 = fib_extension_price(110.0, 90.0, "SHORT", 1.272)
    assert abs(ext_1272 - 84.56) < 1e-9


def test_find_last_impulse_leg_long():
    df = make_1h_fib_df("LONG")
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    leg = find_last_impulse_leg(swings, "LONG", lookback_bars=60)

    assert leg is not None
    assert abs(leg.leg_start - 90.0) < 0.01
    assert abs(leg.leg_end - 110.0) < 0.01
    assert leg.leg_start_idx < leg.leg_end_idx


def test_find_last_impulse_leg_short():
    df = make_1h_fib_df("SHORT", leg_start=110.0, leg_end=90.0)
    closed = df.iloc[:-1]
    swings = find_swings(closed, fractal_bars=5)

    leg = find_last_impulse_leg(swings, "SHORT", lookback_bars=60)

    assert leg is not None
    assert abs(leg.leg_start - 110.0) < 0.01
    assert abs(leg.leg_end - 90.0) < 0.01


def test_find_last_impulse_leg_returns_none_without_swings():
    # A monotonic (non-reversing) series has zero interior fractal swing
    # points -- each bar's fractal window max/min always falls at the
    # window's leading edge, never at the center. A perfectly flat/
    # duplicate-value series would instead tie for max/min at every
    # interior bar under find_swings' `==` comparison and spuriously
    # register as swings at every position, which isn't the "no swing
    # structure" case this test means to exercise.
    idx = pd.date_range("2026-01-01", periods=30, freq="1h")
    closes = 100.0 + np.arange(30) * 0.001
    flat = pd.DataFrame(
        {"open": closes, "high": closes + 0.1, "low": closes - 0.1, "close": closes, "volume": 1000.0},
        index=idx,
    )
    swings = find_swings(flat, fractal_bars=5)
    leg = find_last_impulse_leg(swings, "LONG", lookback_bars=60)
    assert leg is None
