"""
Liquidity Sweep Retest Scalping Strategy.

Inspired by the Daily Price Action liquidity sweep reversal model.
Reference: https://dailypriceaction.com/blog/liquidity-sweep-reversals/

Flow:
  Tier 1 — 1H structure:
    2 consecutive pivot HH  →  LONG bias
    2 consecutive pivot LL  →  SHORT bias
    If both or neither detected, skip (no clear market structure).

  Tier 2 — 15M sweep + acceptance:
    LONG:  sweep candle wick < pivot_low  AND  close > pivot_low
           → zone_low  = sweep wick low
           → zone_high = swept pivot low
           Acceptance: a subsequent 15M bar closes above sweep_candle_high.
           Invalidation: any 15M bar closes below zone_low → skip.
    SHORT: sweep candle wick > pivot_high AND  close < pivot_high
           → zone_low  = swept pivot high
           → zone_high = sweep wick high
           Acceptance: a subsequent 15M bar closes below sweep_candle_low.
           Invalidation: any 15M bar closes above zone_high → skip.

  Tier 3 — 5M retest + confirmation:
    LONG:  last completed 5M bar touches zone (low ≤ zone_high + 0.1%)
           + bullish close (body ≥ 30%) + RSI > 50 + vol ≥ 1.3× MA + close > EMA50
    SHORT: last completed 5M bar touches zone (high ≥ zone_low − 0.1%)
           + bearish close (body ≥ 30%) + RSI < 50 + vol ≥ 1.3× MA + close < EMA50

  SL: zone_low  − 0.5 × ATR  (LONG)
      zone_high + 0.5 × ATR  (SHORT)
  TP: 2R from entry
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import NamedTuple

import pandas as pd
import pandas_ta as ta

from mexc_client import get_klines
from config import (
    LEVERAGE, MTF_1H, SWEEP_TF, ENTRY_TF, EMA_50,
    RSI_PERIOD, REWARD_RATIO, SL_ATR_BUFFER,
    MAX_RISK_PCT, VOLUME_MA_BARS, VOLUME_MIN_MULT,
)

logger = logging.getLogger(__name__)

# ── Candle counts ─────────────────────────────────────────────────
KLINE_1H_COUNT    = 120   # 1H bars  (~5 days for structure)
KLINE_SWEEP_COUNT = 120   # 15M bars (~30 hours for sweep search)
KLINE_ENTRY_COUNT = 70    # 5M bars  (~6 hours; EMA50 needs ≥50 bars)

# ── Pivot detection ───────────────────────────────────────────────
PIVOT_LOOKBACK = 3        # bars each side required to confirm a pivot

# ── Structure window ─────────────────────────────────────────────
STRUCTURE_1H_BARS = 80    # 1H bars scanned for consecutive HH/LL

# ── Sweep search window ───────────────────────────────────────────
SWEEP_SEARCH_BARS = 60    # 15M bars scanned for sweep candles (~15 h)

# ── Zone touch tolerance ──────────────────────────────────────────
ZONE_BUFFER = 0.001       # 0.1% tolerance for zone touch on 5M


class SweepResult(NamedTuple):
    zone_low:     float
    zone_high:    float
    sweep_bar_idx: int


@dataclass
class Signal:
    symbol:            str
    direction:         str
    entry_price:       float
    tp_price:          float
    sl_price:          float
    leverage:          int
    tp_roi_pct:        float
    sl_roi_pct:        float
    timeframe_summary: str
    generated_at:      datetime
    score:             float = 0.0


# ── Pivot helpers ────────────────────────────────────────────────

def _pivot_highs(series: pd.Series) -> list[tuple[int, float]]:
    """Return (relative_index, price) for each pivot high in series."""
    n = len(series)
    result = []
    for i in range(PIVOT_LOOKBACK, n - PIVOT_LOOKBACK):
        v = float(series.iloc[i])
        if all(series.iloc[i - PIVOT_LOOKBACK:i] < v) and \
           all(series.iloc[i + 1:i + PIVOT_LOOKBACK + 1] < v):
            result.append((i, v))
    return result


def _pivot_lows(series: pd.Series) -> list[tuple[int, float]]:
    """Return (relative_index, price) for each pivot low in series."""
    n = len(series)
    result = []
    for i in range(PIVOT_LOOKBACK, n - PIVOT_LOOKBACK):
        v = float(series.iloc[i])
        if all(series.iloc[i - PIVOT_LOOKBACK:i] > v) and \
           all(series.iloc[i + 1:i + PIVOT_LOOKBACK + 1] > v):
            result.append((i, v))
    return result


# ── Tier 1: 1H market structure ──────────────────────────────────

def _bullish_structure_idx(df_1h: pd.DataFrame) -> int | None:
    """
    Find the most recent pair of consecutive HH in 1H.
    Returns absolute index of the second (latest) HH, or None.
    """
    n = len(df_1h)
    ws = max(0, n - STRUCTURE_1H_BARS - PIVOT_LOOKBACK - 1)
    ph = _pivot_highs(df_1h["high"].iloc[ws:-1])   # exclude forming bar
    if len(ph) < 2:
        return None
    ph_abs = [(ws + i, p) for i, p in ph]
    for k in range(len(ph_abs) - 1, 0, -1):
        idx2, h2 = ph_abs[k]
        idx1, h1 = ph_abs[k - 1]
        if h2 > h1:
            logger.debug(f"  1H bullish HH: {h1:.6g}@{idx1} → {h2:.6g}@{idx2}")
            return idx2
    return None


def _bearish_structure_idx(df_1h: pd.DataFrame) -> int | None:
    """
    Find the most recent pair of consecutive LL in 1H.
    Returns absolute index of the second (latest) LL, or None.
    """
    n = len(df_1h)
    ws = max(0, n - STRUCTURE_1H_BARS - PIVOT_LOOKBACK - 1)
    pl = _pivot_lows(df_1h["low"].iloc[ws:-1])
    if len(pl) < 2:
        return None
    pl_abs = [(ws + i, p) for i, p in pl]
    for k in range(len(pl_abs) - 1, 0, -1):
        idx2, l2 = pl_abs[k]
        idx1, l1 = pl_abs[k - 1]
        if l2 < l1:
            logger.debug(f"  1H bearish LL: {l1:.6g}@{idx1} → {l2:.6g}@{idx2}")
            return idx2
    return None


# ── Tier 2: 15M sweep + acceptance ───────────────────────────────

def _find_long_sweep(df: pd.DataFrame) -> SweepResult | None:
    """
    Search SWEEP_SEARCH_BARS of 15M history for the most recent valid LONG setup:
      - sweep candle wick < pivot_low AND close > pivot_low
      - a subsequent bar closes above sweep_candle_high (acceptance)
      - no subsequent bar closes below zone_low (no invalidation)
    Returns SweepResult or None.
    """
    n = len(df)
    ws = max(0, n - SWEEP_SEARCH_BARS - PIVOT_LOOKBACK - 1)
    pl = _pivot_lows(df["low"].iloc[ws:-1])
    if not pl:
        return None
    pl_abs = [(ws + i, p) for i, p in pl]

    # Scan from newest completed bar backwards
    for sweep_i in range(n - 2, max(ws, n - SWEEP_SEARCH_BARS) - 1, -1):
        bl = float(df["low"].iloc[sweep_i])
        bc = float(df["close"].iloc[sweep_i])
        bh = float(df["high"].iloc[sweep_i])

        # Find the most recent pivot low that was swept by this bar
        swept_pivot = None
        for pi_abs, pi_price in reversed(pl_abs):
            if pi_abs >= sweep_i:
                continue
            if bl < pi_price < bc:   # wick below pivot, close recovers above
                swept_pivot = pi_price
                break

        if swept_pivot is None:
            continue

        zone_low  = bl
        zone_high = swept_pivot

        post = df.iloc[sweep_i + 1:-1]   # completed bars after sweep
        if post.empty:
            continue

        # Acceptance: a bar after sweep must close above sweep_candle_high
        if float(post["close"].max()) <= bh:
            logger.debug(f"  LONG zone [{zone_low:.5g},{zone_high:.5g}] pending acceptance")
            continue

        # Invalidation: no bar after sweep may close below zone_low
        if float(post["close"].min()) < zone_low:
            logger.debug(f"  LONG zone [{zone_low:.5g},{zone_high:.5g}] invalidated (close below wick)")
            continue

        logger.info(
            f"  LONG sweep zone confirmed: [{zone_low:.6g}, {zone_high:.6g}] "
            f"sweep@bar{sweep_i}"
        )
        return SweepResult(zone_low=zone_low, zone_high=zone_high, sweep_bar_idx=sweep_i)

    return None


def _find_short_sweep(df: pd.DataFrame) -> SweepResult | None:
    """
    Search SWEEP_SEARCH_BARS of 15M history for the most recent valid SHORT setup:
      - sweep candle wick > pivot_high AND close < pivot_high
      - a subsequent bar closes below sweep_candle_low (acceptance)
      - no subsequent bar closes above zone_high (no invalidation)
    Returns SweepResult or None.
    """
    n = len(df)
    ws = max(0, n - SWEEP_SEARCH_BARS - PIVOT_LOOKBACK - 1)
    ph = _pivot_highs(df["high"].iloc[ws:-1])
    if not ph:
        return None
    ph_abs = [(ws + i, p) for i, p in ph]

    for sweep_i in range(n - 2, max(ws, n - SWEEP_SEARCH_BARS) - 1, -1):
        bh = float(df["high"].iloc[sweep_i])
        bc = float(df["close"].iloc[sweep_i])
        bl = float(df["low"].iloc[sweep_i])

        swept_pivot = None
        for pi_abs, pi_price in reversed(ph_abs):
            if pi_abs >= sweep_i:
                continue
            if bc < pi_price < bh:   # wick above pivot, close drops below
                swept_pivot = pi_price
                break

        if swept_pivot is None:
            continue

        zone_low  = swept_pivot
        zone_high = bh

        post = df.iloc[sweep_i + 1:-1]
        if post.empty:
            continue

        # Acceptance: a bar after sweep must close below sweep_candle_low
        if float(post["close"].min()) >= bl:
            logger.debug(f"  SHORT zone [{zone_low:.5g},{zone_high:.5g}] pending acceptance")
            continue

        # Invalidation: no bar after sweep may close above zone_high
        if float(post["close"].max()) > zone_high:
            logger.debug(f"  SHORT zone [{zone_low:.5g},{zone_high:.5g}] invalidated (close above wick)")
            continue

        logger.info(
            f"  SHORT sweep zone confirmed: [{zone_low:.6g}, {zone_high:.6g}] "
            f"sweep@bar{sweep_i}"
        )
        return SweepResult(zone_low=zone_low, zone_high=zone_high, sweep_bar_idx=sweep_i)

    return None


# ── Main analysis ────────────────────────────────────────────────

def analyze_coin(symbol: str) -> "Signal | None":
    try:
        # ── Tier 1: 1H market structure ───────────────────────────
        df_1h = get_klines(symbol, MTF_1H, count=KLINE_1H_COUNT)
        if df_1h.empty or len(df_1h) < PIVOT_LOOKBACK * 2 + 5:
            return None

        long_idx  = _bullish_structure_idx(df_1h)
        short_idx = _bearish_structure_idx(df_1h)

        if long_idx is None and short_idx is None:
            logger.debug(f"{symbol}: no 1H market structure")
            return None

        # When both exist, pick the more recent structure
        if long_idx is not None and short_idx is not None:
            direction = "LONG" if long_idx >= short_idx else "SHORT"
        elif long_idx is not None:
            direction = "LONG"
        else:
            direction = "SHORT"

        logger.debug(f"{symbol}: 1H {direction} bias confirmed (2 HH/LL structure)")

        # ── Tier 2: 15M sweep + acceptance ────────────────────────
        df_15m = get_klines(symbol, SWEEP_TF, count=KLINE_SWEEP_COUNT)
        if df_15m.empty or len(df_15m) < 30:
            return None

        if direction == "LONG":
            sweep_result = _find_long_sweep(df_15m)
        else:
            sweep_result = _find_short_sweep(df_15m)

        if sweep_result is None:
            logger.debug(f"{symbol}: no valid {direction} sweep zone on 15M")
            return None

        zone_low, zone_high, sweep_idx = sweep_result
        logger.info(
            f"{symbol}: {direction} sweep zone [{zone_low:.6g}, {zone_high:.6g}] "
            f"accepted — checking 5M retest"
        )

        # ── Tier 3: 5M retest + confirmation ──────────────────────
        df_5m = get_klines(symbol, ENTRY_TF, count=KLINE_ENTRY_COUNT)
        if df_5m.empty or len(df_5m) < VOLUME_MA_BARS + 5:
            return None

        rsi   = ta.rsi(df_5m["close"], length=RSI_PERIOD)
        vol_ma = df_5m["volume"].rolling(VOLUME_MA_BARS).mean()
        ema50 = ta.ema(df_5m["close"], length=EMA_50)
        atr   = ta.atr(df_5m["high"], df_5m["low"], df_5m["close"], length=14)

        c  = df_5m.iloc[-2]   # last completed 5M candle
        co = float(c["open"])
        cc = float(c["close"])
        ch = float(c["high"])
        cl = float(c["low"])

        cr     = ch - cl
        body   = abs(cc - co)
        bratio = body / cr if cr > 0 else 0.0

        rsi_val = float(rsi.iloc[-2])    if not pd.isna(rsi.iloc[-2])    else 50.0
        vol_cur = float(df_5m["volume"].iloc[-2])
        vol_avg = float(vol_ma.iloc[-2]) if not pd.isna(vol_ma.iloc[-2]) else 0.0
        ema_val = float(ema50.iloc[-2])  if not pd.isna(ema50.iloc[-2])  else 0.0
        atr_val = float(atr.iloc[-2])    if not pd.isna(atr.iloc[-2])    else cc * 0.001

        # Zone touch: last 5M bar must be touching / inside the sweep zone
        if direction == "LONG":
            touches_zone = cl <= zone_high * (1 + ZONE_BUFFER)
        else:
            touches_zone = ch >= zone_low * (1 - ZONE_BUFFER)

        if not touches_zone:
            logger.debug(f"{symbol}: {direction} 5M bar not yet touching zone")
            return None

        logger.info(f"{symbol}: {direction} 5M retest confirmed — verifying confirmation filters")

        # Confirmation filters
        if direction == "LONG":
            body_ok = cc > co and bratio >= 0.30
            rsi_ok  = rsi_val > 50
            ema_ok  = ema_val > 0 and cc > ema_val
        else:
            body_ok = cc < co and bratio >= 0.30
            rsi_ok  = rsi_val < 50
            ema_ok  = ema_val > 0 and cc < ema_val

        vol_ok = vol_avg > 0 and vol_cur >= VOLUME_MIN_MULT * vol_avg

        if not (body_ok and rsi_ok and vol_ok and ema_ok):
            logger.debug(
                f"{symbol}: {direction} confirm fail — "
                f"body={bratio:.2f} rsi={rsi_val:.1f} "
                f"vol={vol_cur:.0f}/{vol_avg * VOLUME_MIN_MULT:.0f} ema_ok={ema_ok}"
            )
            return None

        # ── SL / TP ───────────────────────────────────────────────
        entry = cc

        if direction == "LONG":
            sl_price = round(zone_low - SL_ATR_BUFFER * atr_val, 8)
            if sl_price >= entry:
                return None
            risk = entry - sl_price
            tp1  = round(entry + risk, 8)
            tp_price = round(entry + REWARD_RATIO * risk, 8)
        else:
            sl_price = round(zone_high + SL_ATR_BUFFER * atr_val, 8)
            if sl_price <= entry:
                return None
            risk = sl_price - entry
            tp1  = round(entry - risk, 8)
            tp_price = round(entry - REWARD_RATIO * risk, 8)

        risk_pct = risk / entry * 100
        if risk_pct > MAX_RISK_PCT:
            logger.debug(f"{symbol}: SL too wide ({risk_pct:.2f}% > {MAX_RISK_PCT}%)")
            return None

        tp_roi_pct = risk_pct * REWARD_RATIO * LEVERAGE
        sl_roi_pct = risk_pct * LEVERAGE

        # ── Quality score (0–100) ─────────────────────────────────
        rsi_score  = min(abs(rsi_val - 50) / 30.0, 1.0)
        vol_score  = min((vol_cur / (VOLUME_MIN_MULT * vol_avg) - 1) / 1.5, 1.0) if vol_avg > 0 else 0.0
        zone_score = min((zone_high - zone_low) / zone_high / 0.005, 1.0)   # wider zone = cleaner sweep

        score = round(
            (0.40 * rsi_score + 0.35 * vol_score + 0.25 * zone_score) * 100, 1
        )

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{tp_roi_pct:.1f}%) "
            f"SL={sl_price:.6g} (-{sl_roi_pct:.1f}%) | "
            f"zone=[{zone_low:.6g},{zone_high:.6g}] "
            f"risk={risk_pct:.3f}% RSI={rsi_val:.1f} score={score}"
        )

        return Signal(
            symbol            = symbol,
            direction         = direction,
            entry_price       = entry,
            tp_price          = tp_price,
            sl_price          = sl_price,
            leverage          = LEVERAGE,
            tp_roi_pct        = tp_roi_pct,
            sl_roi_pct        = sl_roi_pct,
            timeframe_summary = (
                f"Liquidity Sweep | "
                f"1H Struct → 15M Sweep+Accept → 5M Retest | "
                f"zone=[{zone_low:.5g},{zone_high:.5g}] | "
                f"TP1=${tp1:,.6g} TP2=${tp_price:,.6g}"
            ),
            generated_at      = datetime.now(timezone.utc),
            score             = score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
