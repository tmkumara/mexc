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


def patch_klines(monkeypatch, strategy_module, df_15m: pd.DataFrame, df_5m: pd.DataFrame) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to fixtures by interval."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        if interval == "15m":
            return df_15m
        if interval == "5m":
            return df_5m
        raise ValueError(f"unexpected interval {interval!r} in test")

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)


def make_15m_zone_df(
    direction: str = "LONG",
    bars: int = 220,
    base_price: float = 100.0,
    zone_price: float | None = None,
) -> pd.DataFrame:
    """
    A flat 15m series with exactly one clean, un-broken pivot at
    `zone_price`: a pivot LOW (demand zone) for LONG, a pivot HIGH (supply
    zone) for SHORT. Tapers linearly to/from `zone_price` over
    ZONE_SWING_LENGTH=10 bars either side of the pivot, then returns to
    and stays at `base_price` -- the zone is never revisited, so it never
    gets marked BOS. Ends with one extra duplicated row so callers can
    safely `iloc[:-1]`.

    Tuning notes (Task 6, Step 5):
    - `bars` defaults to 220 (not 200): `evaluate_symbol` also uses this
      same 15m fixture as BTC-filter data (since `patch_klines` routes by
      interval, not symbol, and `ENABLE_BTC_FILTER` is on by default), and
      `build_btc_context` needs at least `TREND_EMA_PERIOD + 5` (205)
      closed candles or it returns None and every candidate is rejected
      with "BTC context unavailable".
    - The pivot is placed at `bars - 100` (not the series midpoint):
      `ZONE_MAX_AGE_BARS` (100) drops any zone older than 100 bars, so
      with more total bars the pivot must sit within the final ~100 bars
      rather than at dead center.
    - The pivot region (+-10 bars) uses a tight wick (0.1) so the zone's
      own top/bottom stay close to `zone_price`; bars outside that region
      use a wider "baseline" wick (0.3) purely to lift the *trailing* ATR
      (used both for the zone-confluence tolerance and the structural-SL
      buffer) without moving the zone's own boundaries. A single uniform
      wick can't satisfy both the confluence-tolerance gate and the
      structural-stop-width gate at once for a `push_step` big enough to
      clear the breakout buffer -- see the 5m fixture's own tuning note
      below for why the entry can't land far from the zone.
    """
    if zone_price is None:
        zone_price = base_price - 10.0 if direction == "LONG" else base_price + 10.0

    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
    mid = bars - 100
    depth = zone_price - base_price

    closes = np.full(bars, base_price)
    for offset in range(-10, 11):
        taper = 1.0 - abs(offset) / 10.0
        closes[mid + offset] = base_price + depth * taper

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    wick_pivot = 0.1
    wick_baseline = 0.3
    wicks = np.full(bars, wick_baseline)
    wicks[mid - 10: mid + 11] = wick_pivot

    highs = np.maximum(opens, closes) + wicks
    lows = np.minimum(opens, closes) - wicks
    volumes = np.full(bars, 1000.0)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_5m_trigger_df(
    direction: str = "LONG",
    bars: int = 90,
    base_price: float = 90.0,
    push_step: float = 0.025,
) -> pd.DataFrame:
    """
    A 5m series: tight chop for the first `bars-9` candles (keeps the
    Chandelier Exit stops close to price), then a clean `push_step`-per-bar
    directional push for the final 9 candles with ramping volume -- flips
    Chandelier direction, pushes PVT past its signal average, and skews
    RSI(25) past RSI(55). Ends with one extra duplicated row so callers can
    safely `iloc[:-1]`.

    Numeric constants here are reasoned, not hand-executed against pandas
    -- same convention as the module docstring above. If Chandelier
    direction, PVT-vs-signal, or RSI-fast-vs-slow don't land as expected
    for the intended direction, widen `push_step`, extend the push window,
    or steepen the volume ramp below and re-run; that is expected TDD
    iteration, not a defect in the test itself.

    Tuning notes (Task 6, Step 5): the original 20-bar push window with
    push_step=0.05 and a +-0.05 candle-wick buffer moved the entry price
    ~1.0 away from `base_price` by the time the breakout condition
    cleared -- comfortably past the demand/supply zone's own ATR-based
    confluence tolerance (a few tenths of a point) *and* past
    MAX_SL_PRICE_PCT's structural-stop budget (also a few tenths of a
    point at ~20x leverage), so a valid signal was mathematically
    unreachable at that displacement regardless of zone tuning. Shrinking
    the wick to near-zero, the push window to 9 bars, and push_step to
    0.025 keeps the final displacement to roughly a quarter of a point --
    just large enough to flip Chandelier/PVT/RSI and clear the breakout
    buffer, and small enough to land back inside the zone fixture's
    confluence tolerance and structural-SL budget above.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")
    window = 9
    flat_n = bars - window

    closes = np.empty(bars)
    closes[:flat_n] = base_price + np.sin(np.arange(flat_n) * 0.5) * 0.05
    for k in range(window):
        closes[flat_n + k] = closes[flat_n - 1] + sign * push_step * (k + 1)

    opens = np.empty(bars)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]

    volumes = np.full(bars, 500.0)
    volumes[flat_n:] = np.linspace(800.0, 3000.0, window)

    wick = 0.0005
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - wick
    if direction == "LONG":
        highs[-1] = max(highs[-1], closes[-1] + wick)
    else:
        lows[-1] = min(lows[-1], closes[-1] - wick)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])
