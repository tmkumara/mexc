"""
Deterministic OHLCV fixture builders for Fibonacci MTF strategy tests.

Numeric constants here are reasoned, not hand-executed against pandas --
if a test using these fails for the wrong reason (RSI/volume landing
outside the expected band), adjust the constants below and re-run. That is
expected TDD iteration, not a defect in the test itself (same convention
as tests/strategy_fixtures.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_4h_trend_df(direction: str = "LONG", bars: int = 220, start_price: float = 100.0) -> pd.DataFrame:
    """Steadily trending, noiseless 4H series -- long enough for EMA200 to
    settle cleanly. Ends with one duplicated row so callers can `iloc[:-1]`
    to drop the "forming" candle."""
    idx = pd.date_range("2026-01-01", periods=bars, freq="4h")
    step = 0.15 if direction == "LONG" else -0.15
    closes = start_price + np.arange(bars) * step
    opens = closes - step
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_1h_fib_df(
    direction: str = "LONG",
    bars: int = 140,
    leg_start: float = 90.0,
    leg_end: float = 110.0,
    confirm_break: float = 0.6,
    confirm_volume_mult: float = 1.6,
) -> pd.DataFrame:
    """
    A 1H series ending in: quiet padding, a clean impulse leg (swing
    low/high at leg_start/leg_end over 20 bars), a 10-bar retracement into
    the middle of the 0.5-0.618 Fibonacci zone, a 9-bar hold near the zone,
    then a confirmation candle that closes back out of the zone in the
    trend direction with a volume spike. Ends with one duplicated row so
    callers can `iloc[:-1]` to drop the "forming" candle.

    Story layout (40 bars, LONG shown -- SHORT mirrors leg_start/leg_end):
      story[0:10]   decline from padding level down to leg_start (swing low)
      story[10:20]  rise from leg_start up to leg_end (swing high)
      story[20:30]  retracement from leg_end down to zone midpoint
      story[30:39]  hold near the zone midpoint (small zigzag)
      story[39]     confirmation candle, closes back out of the zone
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="1h")

    span = abs(leg_end - leg_start)
    zone_mid = leg_end - sign * 0.559 * span   # midpoint of the 0.5-0.618 zone
    zone_edge = leg_end - sign * 0.5 * span    # the 0.5 level (nearer to leg_end)

    # pad + decline together form ONE continuous monotonic run down to
    # leg_start -- built as a single linspace then sliced, so there is no
    # value discontinuity (and therefore no spurious extra swing high) at
    # the pad/decline boundary.
    n_pad = bars - 40
    combined_decline = np.linspace(leg_start + sign * 3.0, leg_start, n_pad + 10)
    pad = combined_decline[:n_pad]
    decline = combined_decline[n_pad:]
    rise = np.linspace(leg_start, leg_end, 11)[1:]
    retrace = np.linspace(leg_end, zone_mid, 11)[1:]

    # Continue the retracement's decline direction at a much gentler slope
    # instead of zigzagging. A zigzag creates local fractal extrema in the
    # hold segment that are *more recent* than the true leg_end/leg_start
    # swing points and get incorrectly picked up as "most recent swing" by
    # find_last_impulse_leg, which anchors on recency, not magnitude. A
    # monotonic continuation has no interior local extrema (each point's
    # fractal window max/min always falls at the window's leading edge),
    # so the true leg boundaries remain the most recent detected swings.
    hold = np.linspace(zone_mid, zone_mid - sign * 0.5, 10)[1:]

    confirm_close = zone_edge + sign * confirm_break
    closes = np.concatenate([pad, decline, rise, retrace, hold, [confirm_close]])

    opens = np.empty(bars)
    opens[0] = closes[0] - sign * 0.05
    opens[1:] = closes[:-1]

    volumes = np.full(bars, 1000.0)
    volumes[-1] = 1000.0 * confirm_volume_mult

    # Small wick offset -- must stay well under the 0.01 price tolerance
    # test_find_last_impulse_leg_long/_short use when comparing the
    # detected swing's high/low value back to the intended leg_start/
    # leg_end (which are baked into `close`, not `high`/`low`).
    highs = np.maximum(opens, closes) + 0.001
    lows = np.minimum(opens, closes) - 0.001

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def patch_fib_klines(monkeypatch, fib_strategy_module, df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> None:
    """Route fib_strategy.get_market_klines(symbol, interval, count) to fixtures by interval."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        if interval == "4h":
            return df_4h
        if interval == "1h":
            return df_1h
        raise ValueError(f"unexpected interval {interval!r} in test")

    monkeypatch.setattr(fib_strategy_module, "get_market_klines", _fake)
