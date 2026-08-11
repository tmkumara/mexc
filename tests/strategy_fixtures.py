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

# Tuned against the real calculate_zlema/calculate_zlema_band/
# calculate_zlema_trend_state (see make_zero_lag_trend_df's docstring for
# why these aren't the plan's original literal constants).
TREND_LEG_BARS = 20
TREND_LEG_STEP_PCT = 0.008
PULLBACK_TREND_STEP_PCT = 0.00005
PULLBACK_OFFSET_PCT = 0.0001


def make_zero_lag_trend_df(
    direction: str = "LONG", bars: int = 320, start_price: float = 100.0, freq: str = "1h",
) -> pd.DataFrame:
    """MACRO_TF/TREND_TF fixture -- long enough (>= ZERO_LAG_LENGTH +
    ZERO_LAG_BAND_LOOKBACK + margin) for calculate_zlema_trend_state to
    settle into a single directional state well before the end of the
    series. Ends with one duplicated last row so callers can safely
    iloc[:-1] to drop the 'forming' candle.

    NOT a pure smooth exponential the whole way through (the first draft
    of this fixture was, per the plan's literal fixture code, and it
    turned out direction-INSENSITIVE against the real
    calculate_zlema_trend_state): calculate_zlema's `2*close -
    close.shift(lag)` bootstrap, evaluated at the single bar where the
    shift first stops being NaN, effectively diffs against the series'
    very first sample. For a pure constant-ratio geometric series that
    produces one large, sign-ambiguous transient exactly at
    initialization -- empirically it was found to latch
    calculate_zlema_trend_state's state to +1 for EITHER direction's pure
    trend at plausible growth rates (verified for magnitudes from 0.3% to
    5% per bar), because the HOLD-previous-state branch keeps whatever
    the bootstrap transient set unless a later bar genuinely re-crosses
    the opposite band -- which a smooth, non-accelerating geometric climb
    essentially never does once its steady-state offset from its own
    ZLEMA stabilizes under the band width.

    The fix used here: a completely flat, near-zero-volatility baseline
    (bars - TREND_LEG_BARS of it, so ZLEMA/ATR/band start from a clean,
    stable state well clear of the shift-to-index-0 artifact) followed by
    TREND_LEG_BARS of a real directional leg steep enough
    (TREND_LEG_STEP_PCT/bar) to break price through the correct band side
    -- verified directionally correct (LONG -> +1, SHORT -> -1) across
    bars=320 at both 1h and 4h spacing."""
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq=freq)
    trend_leg_bars = min(TREND_LEG_BARS, max(bars - 1, 1))
    flat_bars = bars - trend_leg_bars
    flat_closes = np.full(flat_bars, start_price)
    trend_closes = start_price * (1.0 + TREND_LEG_STEP_PCT * sign) ** np.arange(1, trend_leg_bars + 1)
    closes = np.concatenate([flat_closes, trend_closes])
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_zero_lag_pullback_df(
    direction: str = "LONG", bars: int = 100, start_price: float = 100.0,
) -> pd.DataFrame:
    """PULLBACK_TF (15m) fixture: a steady trend, then a final closed
    candle that pulls back to within a small distance of its own ZLEMA
    (comfortably inside the default PULLBACK_DISTANCE_PCT=0.10% band).
    SHORT mirrors every inequality. Ends with one duplicated last row.

    PULLBACK_TREND_STEP_PCT is deliberately much smaller than
    make_zero_lag_trend_df's leg -- at only bars=100 (ZERO_LAG_LENGTH=70's
    EMA has not fully converged), a faster trend leaves ZLEMA still
    catching up to price, which dominates the close-vs-ZLEMA distance and
    swamps the deliberate PULLBACK_OFFSET_PCT nudge this fixture is
    actually trying to test. A slow-enough trend keeps ZLEMA close to
    price so the final candle's small pullback nudge is what determines
    distance_pct."""
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="15min")
    trend_bars = bars - 1
    closes = start_price * (1.0 + PULLBACK_TREND_STEP_PCT * sign) ** np.arange(trend_bars)
    trend_last = closes[-1]
    pullback_close = trend_last * (1 - PULLBACK_OFFSET_PCT * sign)
    closes = np.concatenate([closes, [pullback_close]])
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) + 0.05
    lows = np.minimum(opens, closes) - 0.05
    volumes = np.full(bars, 1000.0)
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=idx,
    )
    return pd.concat([df, df.iloc[[-1]]])


def make_zero_lag_crossover_df(
    direction: str = "LONG", bars: int = 90, start_price: float = 100.0,
) -> pd.DataFrame:
    """ENTRY_TF (5m) fixture: price sits on the WRONG side of its own
    ZLEMA for most of the series, then the final closed candle crosses
    back with a directional confirmation close (close > open for LONG,
    close < open for SHORT) -- the shape check_setup_confirmation's
    pending_pullback stage looks for. Ends with one duplicated last row."""
    sign = 1.0 if direction == "LONG" else -1.0
    idx = pd.date_range("2026-01-01", periods=bars, freq="5min")
    below_bars = bars - 1
    closes = start_price * (1.0 - 0.0008 * sign) ** np.arange(below_bars)
    last_below = closes[-1]
    crossover_close = last_below * (1 + 0.01 * sign)
    closes = np.concatenate([closes, [crossover_close]])
    opens = np.empty_like(closes)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    opens[-1] = last_below
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


def patch_klines_multi(monkeypatch, strategy_module, dfs_by_interval: dict) -> None:
    """Route strategy.get_market_klines(symbol, interval, count) to a
    per-interval fixture -- needed once a strategy fetches more than one
    timeframe (TREND_TF and ENTRY_TF) in the same detection pass."""

    def _fake(symbol: str, interval: str, count: int = 100) -> pd.DataFrame:
        import pandas as pd
        return dfs_by_interval.get(interval, pd.DataFrame())

    monkeypatch.setattr(strategy_module, "get_market_klines", _fake)
