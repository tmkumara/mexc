"""
Deterministic OHLCV fixture builders for Nadaraya-Watson kernel strategy
tests.

Numeric constants here are reasoned, not hand-executed against pandas -- if
a test using these fails for the wrong reason, adjust the constants below
and re-run. That is expected TDD iteration, not a defect in the test itself
(same convention as tests/fib_fixtures.py).
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


def make_swing_fixture(kind: str = "low", fractal_bars: int = 5, pivot_value: float = 95.0) -> pd.DataFrame:
    """A short, deterministic 1H series with exactly one unambiguous
    fractal pivot at its center: `fractal_bars` strictly-descending bars
    into the pivot, then `fractal_bars` strictly-ascending bars out of it
    (mirrored for a swing high). No duplicated forming-candle row -- callers
    pass this directly (find_swing_low/high take an explicit end_idx)."""
    n = fractal_bars * 2 + 1
    idx = pd.date_range("2026-01-01", periods=n, freq="1h")
    sign = 1.0 if kind == "low" else -1.0
    left = pivot_value + sign * np.arange(fractal_bars, 0, -1) * 1.0
    right = pivot_value + sign * np.arange(1, fractal_bars + 1) * 1.0
    closes = np.concatenate([left, [pivot_value], right])
    opens = closes.copy()
    highs = closes + 0.3
    lows = closes - 0.3
    if kind == "low":
        lows = closes.copy()
        lows[fractal_bars] = pivot_value
    else:
        highs = closes.copy()
        highs[fractal_bars] = pivot_value
    volumes = np.full(n, 1000.0)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )


def make_1h_nw_df(
    direction: str = "LONG",
    pad_bars: int = 60,
    base_price: float = 100.0,
    dip_depth: float = 0.5,
    recovery_depth: float = 0.2,
    final_decline_depth: float = 0.2,
    bounce_size: float = 0.4,
    volume_mult: float = 1.8,
) -> pd.DataFrame:
    """
    LONG story (SHORT mirrors the sign of every price move):
      pad:            `pad_bars` flat bars at base_price (warmup only, and
                       keeps RSI's EWM average away from a 0/0 edge case).
      dip_down:       10 bars, monotonic decline base_price ->
                       base_price - dip_depth.
      dip_recover:    5 bars, monotonic rise back up to
                       base_price - dip_depth + recovery_depth -- confirms
                       an interior NW_SWING_FRACTAL_BARS-fractal low at the
                       dip bottom, findable by find_swing_low within
                       NW_SWING_LOOKBACK_BARS of the signal bar.
      final_decline:  10 bars, monotonic decline from the recovered level
                       down to a new, lower bottom -- guarantees the kernel
                       estimate is still declining into the last two
                       pre-signal bars (the "was_bearish" half of the
                       bullish slope-flip condition).
      signal bar:     1 bar, closes `bounce_size` above the final_decline
                       bottom, with a volume spike -- this is the bar
                       evaluate_symbol_nw/_detect_nw_signal check.

    Ends with one duplicated row so callers can `iloc[:-1]` to drop the
    "forming" candle -- the *second-to-last* row of the returned frame is
    the actual signal bar once that drop happens.

    Numeric constants here are reasoned, not hand-executed against pandas --
    if a test fails for the wrong reason (kernel flip / RSI / swing not
    landing as expected), adjust dip_depth/recovery_depth/
    final_decline_depth/bounce_size and re-run. Same convention as
    tests/fib_fixtures.py.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    total_bars = pad_bars + 10 + 5 + 10 + 1
    idx = pd.date_range("2026-01-01", periods=total_bars, freq="1h")

    pad = np.full(pad_bars, base_price)
    dip_bottom = base_price - sign * dip_depth
    dip_down = np.linspace(base_price, dip_bottom, 11)[1:]
    recovered_level = dip_bottom + sign * recovery_depth
    dip_recover = np.linspace(dip_bottom, recovered_level, 6)[1:]
    final_bottom = recovered_level - sign * final_decline_depth
    final_decline = np.linspace(recovered_level, final_bottom, 11)[1:]
    signal_close = final_bottom + sign * bounce_size

    closes = np.concatenate([pad, dip_down, dip_recover, final_decline, [signal_close]])
    opens = np.empty(len(closes))
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    highs = np.maximum(opens, closes) + 0.02
    lows = np.minimum(opens, closes) - 0.02

    volumes = np.full(len(closes), 1000.0)
    volumes[-1] = 1000.0 * volume_mult

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def patch_nw_klines(monkeypatch, nw_strategy_module, df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> None:
    """Route nw_strategy.get_market_klines(symbol, interval, count) to
    fixtures by interval."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        if interval == "4h":
            return df_4h
        if interval == "1h":
            return df_1h
        raise ValueError(f"unexpected interval {interval!r} in test")

    monkeypatch.setattr(nw_strategy_module, "get_market_klines", _fake)
