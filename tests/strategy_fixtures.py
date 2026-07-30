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
