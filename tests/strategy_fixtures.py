"""
Deterministic OHLCV fixture builders for strategy tests.

Numeric constants here are reasoned, not hand-executed against pandas --
if a test using these fails for the wrong reason (RSI/EMA-distance/ATR
ratio landing outside the expected band), adjust the constants below and
re-run. That is expected TDD iteration, not a defect in the test itself.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_15m_trend_df(direction: str = "LONG", bars: int = 220, start_price: float = 100.0) -> pd.DataFrame:
    """
    A steadily trending, noiseless 15m series -- long enough for EMA200 +
    Supertrend(10, 3.0) to settle cleanly. Ends with one extra duplicated
    row so callers can safely `iloc[:-1]` to drop the "forming" candle.
    """
    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
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


def patch_klines(monkeypatch, strategy_module, df: pd.DataFrame) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to a single fixture."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        return df

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)


def make_ribbon_trendbar_df(
    direction: str = "LONG",
    bars: int = 200,
    base_price: float = 100.0,
    push_bars: int = 8,
    push_step: float = 0.04,
) -> pd.DataFrame:
    """
    A single-timeframe series: flat/ranging for the first
    `bars - push_bars` candles (long enough for every EMA, including the
    60-period ribbon baseline, to fully converge -- keeps the EMA ribbon
    compressed near `base_price`), then a clean `push_step`-per-bar
    directional push for the final `push_bars` candles. From a fully
    converged state, all 6 EMAs separate in alpha order (fastest reacts
    most) on the very first pushed bar, so the ribbon flip lands right at
    the push's start, not partway through it -- `push_bars` must stay
    short (well under RIBBON_LOOKBACK_BARS) or the flip ages out of the
    lookback window by the time the series ends. By the end of an 8-bar
    push, the candle range has also cleared far enough beyond the
    50-period Price-Action-Channel for the Trend Bar to confirm.
    Ends with one extra duplicated row so callers can safely `iloc[:-1]`.

    Numeric constants here are reasoned, not hand-executed against
    pandas -- same convention as every other fixture in this file. If
    the ribbon flip or Trend Bar confirmation don't land within
    RIBBON_LOOKBACK_BARS of each other for the intended direction, widen
    `push_step`, shorten `push_bars` further, or narrow the flat-period
    range and re-run; that is expected TDD iteration, not a defect in
    the test itself.

    Tuning notes (Task 5, Step 5): push_bars=8, push_step=0.3 (an
    earlier attempt) lands the ribbon flip and Trend Bar confirmation
    correctly, but the total push distance (~2 price units) makes the
    structural SL (swing low/high since the flip, minus an ATR buffer)
    exceed MAX_SL_PRICE_PCT (0.5% of entry) by a wide margin --
    `stop_too_wide` rejects every candidate regardless of the ribbon/
    Trend Bar gates passing. Shrinking push_step to 0.04 keeps the total
    displacement small enough (~0.3-0.4 price units on a ~100 base
    price) to land inside the 0.5% SL budget while still clearing both
    gates, verified empirically for both directions (LONG: RR=1.70,
    SHORT: RR=1.69, both above MIN_RR=1.5).
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
    flat_n = bars - push_bars

    # Perfectly flat (not a small oscillation) so every EMA -- including
    # the 60-period ribbon baseline -- converges to exactly base_price
    # with zero residual gap. A small sine wobble was tried here first and
    # occasionally produced a spurious full bearish (or bullish) alignment
    # during the "flat" period by chance, aging the real flip out of the
    # lookback window on one side only -- not reproducible symmetrically.
    closes = np.full(bars, base_price)
    for k in range(push_bars):
        closes[flat_n + k] = closes[flat_n - 1] + sign * push_step * (k + 1)

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    wick = 0.1
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - wick
    volumes = np.full(bars, 1000.0)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])
