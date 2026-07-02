import pandas as pd
import pytest

from order_blocks import find_swings, detect_bos_choch, find_order_blocks


def _make_df() -> pd.DataFrame:
    rows = [
        # open,  high,  low,  close, volume
        (100,   101,   99,   100,   100),
        (100,   102,   99,   101,   100),
        (101,   103,   100,  102,   100),
        (102,   104,   101,  103,   100),   # swing high (104)
        (103,   103.5, 101,  102,   100),
        (102,   102.5, 98,   99,    100),
        (99,    100.5, 97,   98,    100),   # OB candle (bearish), also swing low (97)
        (98,    110,   97.5, 109,   500),   # displacement candle, breaks swing high
        (109,   111,   108,  110,   200),
        (110,   112,   109,  111,   200),
    ]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])


def test_find_swings_identifies_high_and_low():
    df = _make_df()
    swings = find_swings(df, length=2)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    assert any(s.bar_index == 3 and abs(s.price - 104.0) < 1e-9 for s in highs)
    assert any(s.bar_index == 6 and abs(s.price - 97.0) < 1e-9 for s in lows)


def test_detect_bos_choch_fires_on_structure_break():
    df = _make_df()
    swings = find_swings(df, length=2)
    events = detect_bos_choch(df, swings)
    assert len(events) == 1
    assert events[0].bar_index == 7
    assert events[0].direction == "LONG"
    assert events[0].kind == "CHoCH"


def test_find_order_blocks_detects_displaced_ob():
    df = _make_df()
    swings = find_swings(df, length=2)
    events = detect_bos_choch(df, swings)
    atr = pd.Series([1.0] * len(df))  # small ATR -> displacement gate passes easily

    obs = find_order_blocks(df, events, atr, displacement_atr_mult=1.5)

    assert len(obs) == 1
    ob = obs[0]
    assert ob.direction == "LONG"
    assert ob.formed_at_bar == 6
    assert ob.event_bar_index == 7
    assert abs(ob.low - 97.0) < 1e-9
    assert abs(ob.high - 100.5) < 1e-9
    assert ob.structure_event == "CHoCH"


def test_find_order_blocks_skips_weak_displacement_without_fvg():
    df = _make_df()
    swings = find_swings(df, length=2)
    events = detect_bos_choch(df, swings)
    atr = pd.Series([20.0] * len(df))  # large ATR -> displacement gate fails, no FVG present

    obs = find_order_blocks(df, events, atr, displacement_atr_mult=1.5)

    assert obs == []
