"""
Volume Profile computation from OHLCV candles.

Approximates POC/VAH/VAL/HVN/LVN by binning the window's price range and
distributing each candle's volume across the bins its [low, high] overlaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class VolumeProfile:
    poc: float
    vah: float
    val: float
    hvns: list[float] = field(default_factory=list)
    lvns: list[float] = field(default_factory=list)


def _smooth3(values: list[float]) -> list[float]:
    n = len(values)
    out = [0.0] * n
    for i in range(n):
        window = values[max(0, i - 1): min(n, i + 2)]
        out[i] = sum(window) / len(window)
    return out


def compute_volume_profile(
    df: pd.DataFrame,
    bins: int = 40,
    value_area_pct: float = 0.70,
    hvn_mult: float = 1.5,
    lvn_mult: float = 0.4,
) -> VolumeProfile | None:
    """
    df must have 'high', 'low', 'volume' columns, one row per candle,
    already restricted to the desired lookback window.
    Returns None if the window is degenerate (flat price range or no volume).
    """
    lo = float(df["low"].min())
    hi = float(df["high"].max())
    if hi <= lo:
        return None

    bin_size = (hi - lo) / bins
    bin_volume = [0.0] * bins

    for _, row in df.iterrows():
        row_low = float(row["low"])
        row_high = float(row["high"])
        row_vol = float(row["volume"])
        first_bin = max(0, min(bins - 1, int((row_low - lo) / bin_size)))
        last_bin = max(0, min(bins - 1, int((row_high - lo) / bin_size)))
        if last_bin < first_bin:
            first_bin, last_bin = last_bin, first_bin
        span = last_bin - first_bin + 1
        per_bin = row_vol / span
        for b in range(first_bin, last_bin + 1):
            bin_volume[b] += per_bin

    total = sum(bin_volume)
    if total <= 0:
        return None

    poc_bin = max(range(bins), key=lambda b: bin_volume[b])

    lo_b = hi_b = poc_bin
    covered = bin_volume[poc_bin]
    while covered < value_area_pct * total and (lo_b > 0 or hi_b < bins - 1):
        next_lo = bin_volume[lo_b - 1] if lo_b > 0 else -1.0
        next_hi = bin_volume[hi_b + 1] if hi_b < bins - 1 else -1.0
        if next_hi >= next_lo:
            hi_b += 1
            covered += bin_volume[hi_b]
        else:
            lo_b -= 1
            covered += bin_volume[lo_b]

    poc = lo + (poc_bin + 0.5) * bin_size
    vah = lo + (hi_b + 1) * bin_size
    val = lo + lo_b * bin_size

    smoothed = _smooth3(bin_volume)
    mean_vol = total / bins
    hvns = [lo + (b + 0.5) * bin_size for b in range(bins) if smoothed[b] > hvn_mult * mean_vol]
    lvns = [lo + (b + 0.5) * bin_size for b in range(bins) if smoothed[b] < lvn_mult * mean_vol]

    return VolumeProfile(poc=poc, vah=vah, val=val, hvns=hvns, lvns=lvns)


def vp_bias(close: float, vp: VolumeProfile) -> str | None:
    """Returns 'LONG', 'SHORT', or None (inside the value area, no bias)."""
    if close > vp.vah:
        return "LONG"
    if close < vp.val:
        return "SHORT"
    return None


def next_target(direction: str, entry_price: float, vp: VolumeProfile) -> float:
    """TP target: POC if not yet crossed, else the opposite Value Area edge."""
    if direction == "LONG":
        return vp.poc if entry_price < vp.poc else vp.vah
    return vp.poc if entry_price > vp.poc else vp.val
