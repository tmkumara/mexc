import pandas as pd
import pytest

from volume_profile import VolumeProfile, compute_volume_profile, vp_bias, next_target


def _make_df(volumes: list[float]) -> pd.DataFrame:
    """10 candles, each a distinct 1-unit price bin from 100 to 110."""
    rows = []
    for i, vol in enumerate(volumes):
        rows.append({"low": 100.0 + i, "high": 101.0 + i, "volume": vol})
    return pd.DataFrame(rows)


def test_poc_is_the_highest_volume_bin():
    df = _make_df([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    vp = compute_volume_profile(df, bins=10)
    assert vp is not None
    assert abs(vp.poc - 105.5) < 1e-9


def test_value_area_covers_at_least_target_pct():
    df = _make_df([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    vp = compute_volume_profile(df, bins=10, value_area_pct=0.70)
    assert vp.val <= vp.poc <= vp.vah

    total = sum([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    # bin i occupies price [100+i, 101+i); check volume of bins whose
    # midpoint falls inside [val, vah]
    covered = 0.0
    volumes = [10, 10, 10, 10, 10, 100, 10, 10, 10, 10]
    for i, vol in enumerate(volumes):
        mid = 100.0 + i + 0.5
        if vp.val <= mid <= vp.vah:
            covered += vol
    assert covered >= 0.70 * total


def test_hvn_detected_near_poc():
    df = _make_df([10, 10, 10, 10, 10, 100, 10, 10, 10, 10])
    vp = compute_volume_profile(df, bins=10, hvn_mult=1.5)
    assert any(abs(h - 105.5) < 1.5 for h in vp.hvns)


def test_degenerate_flat_range_returns_none():
    df = pd.DataFrame([{"low": 100.0, "high": 100.0, "volume": 10.0}] * 5)
    assert compute_volume_profile(df, bins=10) is None


def test_vp_bias():
    vp = VolumeProfile(poc=105.0, vah=110.0, val=100.0, hvns=[], lvns=[])
    assert vp_bias(111.0, vp) == "LONG"
    assert vp_bias(95.0, vp) == "SHORT"
    assert vp_bias(105.0, vp) is None


def test_next_target_long():
    vp = VolumeProfile(poc=105.0, vah=110.0, val=100.0, hvns=[], lvns=[])
    assert next_target("LONG", 102.0, vp) == 105.0   # entry below POC -> POC
    assert next_target("LONG", 107.0, vp) == 110.0   # entry above POC -> VAH


def test_next_target_short():
    vp = VolumeProfile(poc=105.0, vah=110.0, val=100.0, hvns=[], lvns=[])
    assert next_target("SHORT", 108.0, vp) == 105.0  # entry above POC -> POC
    assert next_target("SHORT", 103.0, vp) == 100.0  # entry below POC -> VAL
