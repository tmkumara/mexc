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

    dip_depth default changed from 1.0 to 0.3: the original value flipped
    the 5m Supertrend bearish and it never recovered bullish by the
    confirmation candle, making valid LONG signals impossible.

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
    -- same convention as make_5m_pullback_df above. If Chandelier
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
