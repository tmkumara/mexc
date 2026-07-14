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


def make_5m_pullback_df(
    direction: str = "LONG",
    bars: int = 60,
    reclaim_offset: float = 0.15,
    confirm_body: float = 0.20,
    confirm_volume_mult: float = 1.5,
    dip_depth: float = 0.3,
) -> pd.DataFrame:
    """
    A 5m series: steady trend on the correct side of EMA20 for its first
    `bars - 5` bars, a 3-bar pullback (positions -4..-2) that dips/pokes
    through EMA20, then a confirmation candle (position -1) reclaiming
    EMA20 by `reclaim_offset` over the EMA level at the prior bar (-2),
    which keeps the anti-chase distance comfortably under
    MAX_EMA_DISTANCE_PCT (0.3%). Ends with one extra duplicated row so
    callers can safely `iloc[:-1]`.

    Indexing (0-indexed, `bars` total rows before the forming-candle dupe):
      bars-1            confirmation candle (position -1)
      bars-4..bars-2     3-bar pullback window (positions -4..-2)
      bars-5            pre-pullback reference bar (position -5)
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")

    step = 0.05
    closes = np.zeros(bars)
    closes[: bars - 4] = 100.0 + sign * np.arange(bars - 4) * step

    base = closes[bars - 5]
    closes[bars - 4] = base + sign * (-dip_depth)          # sharp dip/poke
    closes[bars - 3] = closes[bars - 4] + sign * (-0.2)     # continued softness
    closes[bars - 2] = closes[bars - 3] + sign * 0.3        # stabilizing

    opens = np.empty(bars)
    opens[0] = closes[0] - sign * step
    opens[1:bars - 1] = closes[0:bars - 2]
    # Confirmation candle's open is set below once its close is known.

    volumes = np.full(bars, 1000.0)
    volumes[-1] = 1000.0 * confirm_volume_mult

    # EMA20 evolves recursively; rather than re-derive it by hand for the
    # confirmation candle, compute the running EMA of everything up to
    # bars-2 and place the confirmation close `reclaim_offset` above/below
    # (LONG/SHORT) the EMA level AT bar bars-2 -- since EMA's one-step
    # update satisfies sign(close - ema_new) == sign(close - ema_prior),
    # this guarantees the reclaim condition with the same margin regardless
    # of the exact smoothing constant.
    partial_close = pd.Series(closes[: bars - 1])
    ema20_partial = partial_close.ewm(span=20, adjust=False).mean()
    ema_at_prior_bar = float(ema20_partial.iloc[-1])

    closes[bars - 1] = ema_at_prior_bar + sign * reclaim_offset
    opens[bars - 1] = closes[bars - 1] - sign * confirm_body

    highs = np.maximum(opens, closes) + 0.2
    lows = np.minimum(opens, closes) - 0.2

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def patch_klines(monkeypatch, strategy_module, df_15m: pd.DataFrame, df_5m: pd.DataFrame) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to fixtures by interval."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        if interval == "15m":
            return df_15m
        if interval == "5m":
            return df_5m
        raise ValueError(f"unexpected interval {interval!r} in test")

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)
