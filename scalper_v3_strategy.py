"""
Super Scalper v3 integration layer.

Wraps SuperScalper (super_scalper_v3.py) with:
  - a per-symbol rolling window of the last SCALPER_V3_HISTORY_BARS CLOSED
    candles, refreshed from market_data.get_market_klines() on every call.
    The still-forming candle is always dropped via iloc[:-1], matching the
    completed-candle-only convention used everywhere else in this bot.
    (ws_manager.py only tracks live ticker prices, not a candle stream --
    see CLAUDE.md -- so the rolling window is rebuilt from the same REST/
    cache-backed candle source strategy.py already uses, rather than from
    ws_manager.)
  - the regime/confluence gate from SuperScalper.confluence_ok().
  - two additional safety gates before a signal can fire: a best-effort
    liq_estimator cluster check, and a funding-rate filter via
    mexc_client.get_ticker().
  - TP1 (kc_mid) / TP2 (kc_upper/kc_lower) / SL (supertrend line)
    construction, and a breakeven-after-TP1 hook for the outcome tracker.

v3 is entirely additive and OFF by default (SCALPER_V3_ENABLED=false) --
see main.py's scan_and_fire_signals_v3, which is only scheduled when the
flag is set. Nothing here touches the live v1 Simple Supertrend Pullback
scan path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

import config as cfg
from super_scalper_v3 import SuperScalper
from market_data import get_market_klines
from mexc_client import get_ticker
from liq_estimator import LiqEstimator

logger = logging.getLogger(__name__)


def scalper_kwargs() -> dict:
    return dict(
        atr_period=cfg.SCALPER_V3_ATR_PERIOD,
        atr_mult=cfg.SCALPER_V3_ATR_MULT,
        kc_ema=cfg.SCALPER_V3_KC_EMA,
        kc_atr_period=cfg.SCALPER_V3_KC_ATR_PERIOD,
        kc_mult=cfg.SCALPER_V3_KC_MULT,
        entry_zone=cfg.SCALPER_V3_ENTRY_ZONE,
        slope_lookback=cfg.SCALPER_V3_SLOPE_LOOKBACK,
        ao_fast=cfg.SCALPER_V3_AO_FAST,
        ao_slow=cfg.SCALPER_V3_AO_SLOW,
        strength_lookback=cfg.SCALPER_V3_STRENGTH_LOOKBACK,
        adx_period=cfg.SCALPER_V3_ADX_PERIOD,
        adx_min=cfg.SCALPER_V3_ADX_MIN,
        chop_period=cfg.SCALPER_V3_CHOP_PERIOD,
        chop_max=cfg.SCALPER_V3_CHOP_MAX,
        expand_period=cfg.SCALPER_V3_EXPAND_PERIOD,
        expand_min=cfg.SCALPER_V3_EXPAND_MIN,
    )


@dataclass
class ScalperV3Signal:
    symbol: str
    direction: str          # "LONG" | "SHORT"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    generated_at: datetime
    trend: str
    strength: int
    ao: float
    kc_pos: float
    kc_slope: float
    regime: str
    regime_votes: int
    adx: float
    chop: float
    expansion: float
    funding_rate: float
    entry_kind: str = "flip"    # "flip" (confluence_ok, SuperTrend flip required) | "pullback" (pullback_entry_ok, mid-trend continuation)
    setup_reason: str = "SuperTrend flip + Keltner confluence + regime filter"


@dataclass
class SkippedSignal:
    symbol: str
    direction: str | None
    reason: str
    generated_at: datetime
    details: dict = field(default_factory=dict)


# ── per-symbol rolling state ─────────────────────────────────────────

_engines: dict[str, SuperScalper] = {}
_history: dict[str, pd.DataFrame] = {}
_liq_estimators: dict[str, LiqEstimator] = {}


def get_engine(symbol: str) -> SuperScalper:
    """Public accessor for this symbol's SuperScalper instance (e.g. so a
    caller can recompute() the current rolling window for outcome
    tracking without re-deriving the confluence gate)."""
    return _engine_for(symbol)


def _engine_for(symbol: str) -> SuperScalper:
    """One SuperScalper instance per symbol (stateless compute, but kept
    per-symbol so future per-symbol config overrides are a drop-in)."""
    engine = _engines.get(symbol)
    if engine is None:
        engine = SuperScalper(**scalper_kwargs())
        _engines[symbol] = engine
    return engine


def get_liq_estimator(symbol: str) -> LiqEstimator | None:
    """Best-effort accessor. liq_estimator.py is not wired to a live OI
    poller by default in v3 (that plumbing was retired with the old
    liquidation-scalp strategy -- see CLAUDE.md) so this normally returns
    None and the liq filter fails open. Call register_liq_estimator() to
    feed real OI samples in and activate the filter for a symbol."""
    return _liq_estimators.get(symbol)


def register_liq_estimator(symbol: str, estimator: LiqEstimator) -> None:
    _liq_estimators[symbol] = estimator


def reset_state() -> None:
    """Clear all rolling history/engines. Used by tests and backtests."""
    _engines.clear()
    _history.clear()
    _liq_estimators.clear()


def update_rolling_history(symbol: str) -> pd.DataFrame | None:
    """Fetch the latest closed candles and merge them into this symbol's
    rolling window, keeping only the last SCALPER_V3_HISTORY_BARS closed
    bars. Returns the updated window, or None if data is unavailable."""
    raw = get_market_klines(symbol, cfg.SCALPER_V3_TIMEFRAME, count=cfg.SCALPER_V3_HISTORY_BARS + 5)
    if raw is None or raw.empty or len(raw) < 2:
        return None

    closed = raw.iloc[:-1].copy()  # never include the still-forming bar

    existing = _history.get(symbol)
    if existing is None or existing.empty:
        merged = closed
    else:
        merged = pd.concat([existing, closed])
        merged = merged[~merged.index.duplicated(keep="last")]
        merged.sort_index(inplace=True)

    merged = merged.tail(cfg.SCALPER_V3_HISTORY_BARS)
    _history[symbol] = merged
    return merged


# ── safety gates ──────────────────────────────────────────────────────

def _funding_filter_ok(symbol: str, direction: str) -> tuple[bool, float, str]:
    """Blocks entries where funding is stacked hard against the trade
    direction (crowded-trade / squeeze risk). Positive funding = longs
    pay shorts, i.e. the crowd is long."""
    ticker = get_ticker(symbol)
    if ticker is None:
        return True, 0.0, ""  # unavailable -- fail open, don't block on missing data

    funding_pct = ticker["funding_rate"] * 100.0
    limit = cfg.SCALPER_V3_MAX_ADVERSE_FUNDING_PCT

    if direction == "LONG" and funding_pct > limit:
        return False, funding_pct, f"funding {funding_pct:.3f}% > +{limit:.3f}% (crowded long)"
    if direction == "SHORT" and funding_pct < -limit:
        return False, funding_pct, f"funding {funding_pct:.3f}% < -{limit:.3f}% (crowded short)"
    return True, funding_pct, ""


def _liq_filter_ok(symbol: str, direction: str, entry: float, sl: float) -> tuple[bool, str]:
    """Best-effort liquidation-cluster check: blocks entries whose stop
    sits right on top of a crowded same-side liquidation zone (other
    traders' stops clustered exactly where ours would trigger -- a level
    market makers are more likely to sweep). Fails open when no estimator
    is registered for the symbol (the common case in v3 -- see
    get_liq_estimator)."""
    estimator = get_liq_estimator(symbol)
    if estimator is None:
        return True, ""

    side = "long" if direction == "LONG" else "short"
    clustered = estimator.significant_clusters(entry)
    if not clustered:
        return True, ""

    band_lo, band_hi = sorted((entry, sl))
    magnitude = sum(mag for price, s, mag in clustered if s == side and band_lo <= price <= band_hi)
    if magnitude <= 0:
        return True, ""

    return False, f"stop zone crowded with {magnitude:.0f} notional of other {side} liquidations"


# ── evaluate ──────────────────────────────────────────────────────────

def _calc_tp_sl(direction: str, sig: dict) -> tuple[float, float, float]:
    """SL = supertrend line. TP1 = kc_mid, TP2 = kc_upper for longs
    (mirrored for shorts)."""
    sl = sig["stop_loss"]
    if direction == "LONG":
        return sl, sig["kc_mid"], sig["kc_upper"]
    return sl, sig["kc_mid"], sig["kc_lower"]


def valid_v3_geometry(direction: str, entry: float, sl: float, tp1: float, tp2: float) -> bool:
    if min(entry, sl, tp1, tp2) <= 0:
        return False
    if direction == "LONG":
        return sl < entry < tp1 <= tp2
    return tp2 <= tp1 < entry < sl


def evaluate_symbol_v3(symbol: str, df: pd.DataFrame | None = None) -> ScalperV3Signal | SkippedSignal | None:
    """
    Evaluate one symbol for a v3 Super Scalper signal.

    Two independent entry paths (both from super_scalper_v3.SuperScalper):
      - "flip":     confluence_ok() -- requires a fresh SuperTrend flip on
                    this candle, gated by regime + kc_pos/kc_slope/ao/strength.
      - "pullback": pullback_entry_ok() -- no flip required, just an
                    ongoing TRENDING regime plus the same kc_pos/kc_slope/ao
                    alignment. Added after backtesting real BTC/ETH/SOL 5m
                    data showed confluence_ok()'s kc_pos and kc_slope
                    conditions are almost never simultaneously true AT a
                    flip bar (kc_slope is still negative whenever kc_pos is
                    low enough to qualify for a LONG, and vice versa for
                    SHORT) -- flip-only entries produced ~0 qualifying
                    trades across 6 weeks on any of the 3 symbols. This is
                    the entry method super_scalper_v3.py itself appears to
                    provide for exactly this gap (continuation pullback
                    mid-trend, decoupled from the flip event).

    Returns:
        ScalperV3Signal  -- an entry path fired and all filters passed.
        SkippedSignal    -- a flip fired but confluence_ok() (or a later
                             filter) rejected it. Pullback misses are NOT
                             logged as skips -- unlike a flip, "no pullback
                             entry on this bar" is the normal continuous
                             state during a trend, not a discrete rejected
                             event, so logging every such bar would just
                             flood skipped_signals with noise.
        None             -- no flip and no pullback entry on the latest
                             closed candle (the common case).

    `df` may be supplied directly (used by the backtester, which owns its
    own bar-by-bar window); otherwise the live rolling history is used.
    """
    now = datetime.now(timezone.utc)

    if df is None:
        df = update_rolling_history(symbol)
    if df is None or len(df) < max(cfg.SCALPER_V3_HISTORY_BARS // 3, 60):
        return None

    engine = _engine_for(symbol)
    try:
        computed = engine.compute(df)
        sig = engine.latest_signal(computed)
    except Exception as e:
        logger.error("[V3-EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        return None

    if sig["side"] is not None:
        direction = "LONG" if sig["side"] == "BUY" else "SHORT"
        if not engine.confluence_ok(sig, min_strength=cfg.SCALPER_V3_MIN_STRENGTH):
            return SkippedSignal(
                symbol=symbol, direction=direction, generated_at=now,
                reason=_confluence_reject_reason(sig, direction),
                details=sig,
            )
        entry_kind = "flip"
    else:
        if sig["regime"] != "TRENDING" or not engine.pullback_entry_ok(sig):
            return None
        direction = "LONG" if sig["trend"] == "BULLISH" else "SHORT"
        entry_kind = "pullback"

    entry = sig["price"]
    sl, tp1, tp2 = _calc_tp_sl(direction, sig)

    if not valid_v3_geometry(direction, entry, sl, tp1, tp2):
        return SkippedSignal(
            symbol=symbol, direction=direction, generated_at=now,
            reason="invalid_geometry", details=sig,
        )

    liq_ok, liq_reason = _liq_filter_ok(symbol, direction, entry, sl)
    if not liq_ok:
        return SkippedSignal(
            symbol=symbol, direction=direction, generated_at=now,
            reason=f"liq_estimator: {liq_reason}", details=sig,
        )

    funding_ok, funding_pct, funding_reason = _funding_filter_ok(symbol, direction)
    if not funding_ok:
        return SkippedSignal(
            symbol=symbol, direction=direction, generated_at=now,
            reason=f"funding: {funding_reason}", details=sig,
        )

    return ScalperV3Signal(
        symbol=symbol,
        direction=direction,
        entry_price=round(entry, 8),
        sl_price=round(sl, 8),
        tp1_price=round(tp1, 8),
        tp2_price=round(tp2, 8),
        generated_at=now,
        trend=sig["trend"],
        strength=sig["strength"],
        ao=sig["ao"],
        kc_pos=sig["kc_pos"],
        kc_slope=sig["kc_slope"],
        regime=sig["regime"],
        regime_votes=sig["regime_votes"],
        adx=sig["adx"],
        chop=sig["chop"],
        expansion=sig["expansion"],
        funding_rate=funding_pct,
        entry_kind=entry_kind,
        setup_reason=(
            "SuperTrend flip + Keltner confluence + regime filter" if entry_kind == "flip"
            else "Mid-trend pullback continuation (pullback_entry_ok, TRENDING regime)"
        ),
    )


def _confluence_reject_reason(sig: dict, direction: str) -> str:
    """Best-effort categorisation of why confluence_ok() rejected, for the
    skipped_signals log (mirrors strategy.py's _reason_bucket pattern)."""
    if sig["regime"] == "RANGING":
        return "regime_ranging"
    if sig["regime"] == "TRANSITION":
        return "regime_transition_insufficient_strength"

    c = cfg
    if direction == "LONG":
        if sig["trend"] != "BULLISH":
            return "trend_mismatch"
        if sig["kc_pos"] > c.SCALPER_V3_ENTRY_ZONE:
            return "kc_pos_outside_entry_zone"
        if sig["kc_slope"] <= 0.05:
            return "kc_slope_flat"
        if not (sig["ao"] > 0 or sig["ao_rising"]):
            return "ao_bearish"
        if sig["strength"] < c.SCALPER_V3_MIN_STRENGTH:
            return "strength_below_min"
    else:
        if sig["trend"] != "BEARISH":
            return "trend_mismatch"
        if sig["kc_pos"] < 1 - c.SCALPER_V3_ENTRY_ZONE:
            return "kc_pos_outside_entry_zone"
        if sig["kc_slope"] >= -0.05:
            return "kc_slope_flat"
        if not (sig["ao"] < 0 or not sig["ao_rising"]):
            return "ao_bullish"
        if sig["strength"] < c.SCALPER_V3_MIN_STRENGTH:
            return "strength_below_min"
    return "confluence_other"


# ── breakeven management ───────────────────────────────────────────────

def apply_breakeven(direction: str, entry: float, sl: float, tp1: float, high: float, low: float) -> float:
    """Given one bar's high/low after entry, return the (possibly moved)
    SL: once TP1 fills, SL moves to breakeven (entry price). Pure function
    so both the live outcome tracker and the backtester share one
    implementation."""
    if not cfg.SCALPER_V3_BREAKEVEN_AFTER_TP1:
        return sl
    if direction == "LONG" and high >= tp1:
        return max(sl, entry)
    if direction == "SHORT" and low <= tp1:
        return min(sl, entry)
    return sl


def build_supertrend_series(direction: str, computed_df: pd.DataFrame) -> pd.Series:
    """Extract the SuperTrend line as a plain Series for walk_trade()."""
    return computed_df["supertrend"]


def walk_trade(
    direction: str,
    entry_price: float,
    initial_sl: float,
    tp1_price: float,
    tp2_price: float,
    bars: pd.DataFrame,
) -> dict:
    """
    Walk CLOSED bars AFTER entry and resolve the trade outcome. Shared by
    the live outcome tracker (main.py) and the backtester (backtest/
    engine.py) so live and backtested TP1/TP2/SL/breakeven/trailing-stop
    semantics can never drift apart.

    `bars` must contain columns high/low/close/supertrend, one row per
    CLOSED candle strictly after the entry candle, in chronological order.
    The SL level in effect while bar i is forming is the supertrend value
    as of the close of bar i-1 (the previous CLOSED bar) -- never bar i's
    own value, which would be lookahead (bar i's supertrend can only be
    known once bar i itself has closed).

    Same-bar tie-break: if both SL and a TP are touched within one bar,
    the stop wins (conservative), matching outcome_check.check_tp_sl.

    Returns:
        {
          "status": "win" | "loss" | "pending",
          "exit_price": float | None,
          "exit_reason": "tp2" | "sl" | "breakeven" | None,
          "tp1_hit": bool,
          "tp1_hit_at_idx": int | None,
          "bars_held": int,
          "final_sl": float,   # trailing SL level in effect at return time
        }
    """
    sl_level = initial_sl
    tp1_hit = False
    tp1_hit_idx: int | None = None
    prev_trail = initial_sl

    for i in range(len(bars)):
        high = float(bars["high"].iloc[i])
        low = float(bars["low"].iloc[i])

        # Trail using the PREVIOUS closed bar's supertrend value (no lookahead).
        if direction == "LONG":
            sl_level = max(sl_level, prev_trail)
            if tp1_hit:
                sl_level = max(sl_level, entry_price)
        else:
            sl_level = min(sl_level, prev_trail)
            if tp1_hit:
                sl_level = min(sl_level, entry_price)

        hit_sl = (low <= sl_level) if direction == "LONG" else (high >= sl_level)
        if hit_sl:
            # Once TP1 has hit, SL only ever trails to entry or better (see
            # above), so a stop-out post-TP1 is always breakeven-or-profit.
            reason = "breakeven" if tp1_hit else "sl"
            status = "win" if tp1_hit else "loss"
            return {
                "status": status, "exit_price": sl_level, "exit_reason": reason,
                "tp1_hit": tp1_hit, "tp1_hit_at_idx": tp1_hit_idx,
                "bars_held": i + 1, "final_sl": sl_level,
            }

        hit_tp1 = (high >= tp1_price) if direction == "LONG" else (low <= tp1_price)
        if hit_tp1 and not tp1_hit:
            tp1_hit = True
            tp1_hit_idx = i

        hit_tp2 = (high >= tp2_price) if direction == "LONG" else (low <= tp2_price)
        if hit_tp2:
            return {
                "status": "win", "exit_price": tp2_price, "exit_reason": "tp2",
                "tp1_hit": True, "tp1_hit_at_idx": tp1_hit_idx if tp1_hit_idx is not None else i,
                "bars_held": i + 1, "final_sl": sl_level,
            }

        prev_trail = float(bars["supertrend"].iloc[i])

    return {
        "status": "pending", "exit_price": None, "exit_reason": None,
        "tp1_hit": tp1_hit, "tp1_hit_at_idx": tp1_hit_idx,
        "bars_held": len(bars), "final_sl": sl_level,
    }
