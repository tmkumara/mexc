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


def make_trend_df(
    direction: str = "LONG", bars: int = 260, start_price: float = 100.0, freq: str = "15min"
) -> pd.DataFrame:
    """A steadily trending, noiseless series -- long enough for EMA200 +
    its slope lookback to settle cleanly on either TREND_TF or ENTRY_TF.
    Ends with one extra duplicated row so callers can safely `iloc[:-1]`
    to drop the "forming" candle."""
    idx = pd.date_range("2026-01-01", periods=bars, freq=freq)
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


def make_15m_trend_df(direction: str = "LONG", bars: int = 220, start_price: float = 100.0) -> pd.DataFrame:
    """Kept for existing callers -- delegates to make_trend_df with freq="15min"."""
    return make_trend_df(direction, bars=bars, start_price=start_price, freq="15min")


def make_pullback_confirmation_df(
    direction: str = "LONG", bars: int = 260, start_price: float = 100.0
) -> pd.DataFrame:
    """ENTRY_TF (5m) fixture: a steady trend (EMA20/EMA50 aligned and
    separated) for most of the series, a 3-candle pullback toward EMA20
    that pulls RSI14 down/up into the reset zone, then a strong
    confirming candle (closes beyond open/EMA20/prior high-low, elevated
    volume) that should satisfy detect_pending_setup's full pipeline.
    SHORT mirrors every inequality. Ends with one duplicated last row so
    callers can safely iloc[:-1] to drop the "forming" candle, matching
    every other fixture in this module."""
    sign = 1.0 if direction == "LONG" else -1.0
    trend_bars = bars - 4
    step = 0.25 * sign
    closes = list(start_price + np.arange(trend_bars) * step)
    trend_last = closes[-1]

    pullback = [
        trend_last - 1.0 * sign,
        trend_last - 3.0 * sign,
        trend_last - 3.5 * sign,
    ]
    closes.extend(pullback)

    confirm_close = pullback[-1] + 1.2 * sign
    closes.append(confirm_close)

    closes = np.array(closes)
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]

    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    volumes[-1] = 1400.0   # 1.4x -- above VOLUME_CONFIRM_MULT's default 1.15x

    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")
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


def patch_klines_multi(monkeypatch, strategy_module, dfs_by_interval: dict) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to a
    per-interval fixture -- needed once a strategy fetches more than one
    timeframe (TREND_TF and ENTRY_TF) in the same detection pass."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        import pandas as pd
        return dfs_by_interval.get(interval, pd.DataFrame())

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)
