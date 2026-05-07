"""
Multi-timeframe strategy: 1H Trend + 15M Liquidity Sweep + 5M Confirmation.

Tier 1 — 1H trend:
  price > EMA50 > EMA200  →  LONG bias
  price < EMA50 < EMA200  →  SHORT bias

Tier 2 — 15M liquidity sweep:
  LONG:  a recent 15M candle swept below a prior swing low and closed back above it
  SHORT: a recent 15M candle swept above a prior swing high and closed back below it

Tier 3 — 5M confirmation (last completed candle):
  LONG:  bullish body (body ≥ 30% of range) + RSI(14) > 50 + volume ≥ 1.3× MA
  SHORT: bearish body (body ≥ 30% of range) + RSI(14) < 50 + volume ≥ 1.3× MA

SL: swept level − ATR buffer (LONG) / swept level + ATR buffer (SHORT)
TP: 2R from entry (TP1 at 1R shown in message)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from mexc_client import get_klines
from config import (
    LEVERAGE, MTF_1H, MTF_15M, ENTRY_TF,
    EMA_50, EMA_200,
    RSI_PERIOD,
    REWARD_RATIO, SL_ATR_BUFFER, MAX_RISK_PCT,
    VOLUME_MA_BARS, VOLUME_MIN_MULT,
)

logger = logging.getLogger(__name__)

KLINE_1H_COUNT:  int = 250   # EMA200 warm-up + buffer
KLINE_15M_COUNT: int = 60    # swing zone + recent sweep window
KLINE_5M_COUNT:  int = 50    # RSI + volume MA warm-up

# Swing-level detection (15M)
SWING_ZONE_START: int = -45  # oldest bar in swing-level window
SWING_ZONE_END:   int = -8   # newest bar in swing-level window (keep gap from sweep zone)
SWEEP_RECENT:     int = 8    # how many recent completed 15M bars to check for the sweep
PIVOT_LOOKBACK:   int = 3    # bars on each side required for pivot low/high


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


# ── swing-level helpers ───────────────────────────────────────────

def _pivot_low(df: pd.DataFrame, start: int, end: int) -> float | None:
    """Return the most recent pivot low (lower than PIVOT_LOOKBACK bars each side)."""
    sub = df["low"].iloc[start:end]
    for i in range(PIVOT_LOOKBACK, len(sub) - PIVOT_LOOKBACK):
        v = sub.iloc[i]
        if all(sub.iloc[i - PIVOT_LOOKBACK:i] > v) and \
           all(sub.iloc[i + 1:i + PIVOT_LOOKBACK + 1] > v):
            return float(v)
    return float(sub.min()) if len(sub) > 0 else None   # fallback: zone minimum


def _pivot_high(df: pd.DataFrame, start: int, end: int) -> float | None:
    """Return the most recent pivot high (higher than PIVOT_LOOKBACK bars each side)."""
    sub = df["high"].iloc[start:end]
    for i in range(PIVOT_LOOKBACK, len(sub) - PIVOT_LOOKBACK):
        v = sub.iloc[i]
        if all(sub.iloc[i - PIVOT_LOOKBACK:i] < v) and \
           all(sub.iloc[i + 1:i + PIVOT_LOOKBACK + 1] < v):
            return float(v)
    return float(sub.max()) if len(sub) > 0 else None   # fallback: zone maximum


def _detect_sweep(df_15m: pd.DataFrame, direction: str) -> float | None:
    """
    Find a recent liquidity sweep on 15M.
    Returns the swept level (SL anchor) or None.
    """
    if len(df_15m) < abs(SWING_ZONE_START) + 5:
        return None

    # Completed bars only; iloc[-1] is the forming candle
    recent = df_15m.iloc[-(SWEEP_RECENT + 1):-1]   # last SWEEP_RECENT completed bars

    if direction == "LONG":
        swing = _pivot_low(df_15m, SWING_ZONE_START, SWING_ZONE_END)
        if swing is None:
            return None
        for i in range(len(recent)):
            c = recent.iloc[i]
            if float(c["low"]) < swing and float(c["close"]) > swing:
                return swing
    else:
        swing = _pivot_high(df_15m, SWING_ZONE_START, SWING_ZONE_END)
        if swing is None:
            return None
        for i in range(len(recent)):
            c = recent.iloc[i]
            if float(c["high"]) > swing and float(c["close"]) < swing:
                return swing

    return None


# ── main analysis ────────────────────────────────────────────────

def analyze_coin(symbol: str) -> "Signal | None":
    try:
        # ── Tier 1: 1H trend ─────────────────────────────────────────
        df_1h = get_klines(symbol, MTF_1H, count=KLINE_1H_COUNT)
        if df_1h.empty or len(df_1h) < EMA_200 + 10:
            return None

        ema50_1h  = ta.ema(df_1h["close"], length=EMA_50)
        ema200_1h = ta.ema(df_1h["close"], length=EMA_200)

        if pd.isna(ema50_1h.iloc[-2]) or pd.isna(ema200_1h.iloc[-2]):
            return None

        close_1h = float(df_1h["close"].iloc[-2])
        e50      = float(ema50_1h.iloc[-2])
        e200     = float(ema200_1h.iloc[-2])

        if   close_1h > e50 and e50 > e200:
            direction = "LONG"
        elif close_1h < e50 and e50 < e200:
            direction = "SHORT"
        else:
            logger.debug(f"{symbol}: no clear 1H trend")
            return None

        # ── Tier 2: 15M liquidity sweep ───────────────────────────────
        df_15m = get_klines(symbol, MTF_15M, count=KLINE_15M_COUNT)
        if df_15m.empty or len(df_15m) < abs(SWING_ZONE_START) + 5:
            return None

        swept_level = _detect_sweep(df_15m, direction)
        if swept_level is None:
            logger.debug(f"{symbol}: no 15M {direction} sweep found")
            return None

        # ── Tier 3: 5M confirmation ───────────────────────────────────
        df_5m = get_klines(symbol, ENTRY_TF, count=KLINE_5M_COUNT)
        if df_5m.empty or len(df_5m) < VOLUME_MA_BARS + 5:
            return None

        rsi_5m = ta.rsi(df_5m["close"], length=RSI_PERIOD)
        vol_ma = df_5m["volume"].rolling(VOLUME_MA_BARS).mean()

        c = df_5m.iloc[-2]   # last completed 5M candle
        co = float(c["open"])
        cc = float(c["close"])
        ch = float(c["high"])
        cl = float(c["low"])

        candle_range = ch - cl
        body         = abs(cc - co)
        body_ratio   = body / candle_range if candle_range > 0 else 0.0

        rsi_val = float(rsi_5m.iloc[-2]) if not pd.isna(rsi_5m.iloc[-2]) else None
        vol_cur = float(df_5m["volume"].iloc[-2])
        vol_avg = float(vol_ma.iloc[-2]) if not pd.isna(vol_ma.iloc[-2]) else None

        if rsi_val is None or vol_avg is None:
            return None

        if direction == "LONG":
            body_ok    = cc > co and body_ratio >= 0.30
            rsi_ok     = rsi_val > 50
        else:
            body_ok    = cc < co and body_ratio >= 0.30
            rsi_ok     = rsi_val < 50

        volume_ok = vol_avg > 0 and vol_cur >= VOLUME_MIN_MULT * vol_avg

        if not (body_ok and rsi_ok and volume_ok):
            logger.debug(
                f"{symbol}: 5M confirm fail "
                f"(body={body_ratio:.2f} rsi={rsi_val:.1f} vol={vol_cur:.0f}/{vol_avg*VOLUME_MIN_MULT:.0f})"
            )
            return None

        # ── SL / TP ───────────────────────────────────────────────────
        entry = float(df_5m["close"].iloc[-2])

        atr_5m  = ta.atr(df_5m["high"], df_5m["low"], df_5m["close"], length=14)
        atr_val = float(atr_5m.iloc[-2]) if not pd.isna(atr_5m.iloc[-2]) else entry * 0.001

        if direction == "LONG":
            if swept_level >= entry:
                return None
            sl_price = round(swept_level - SL_ATR_BUFFER * atr_val, 8)
            risk     = entry - sl_price
            tp1      = round(entry + risk, 8)
            tp_price = round(entry + REWARD_RATIO * risk, 8)
        else:
            if swept_level <= entry:
                return None
            sl_price = round(swept_level + SL_ATR_BUFFER * atr_val, 8)
            risk     = sl_price - entry
            tp1      = round(entry - risk, 8)
            tp_price = round(entry - REWARD_RATIO * risk, 8)

        risk_pct = risk / entry * 100
        if risk_pct > MAX_RISK_PCT:
            logger.debug(f"{symbol}: SL too wide ({risk_pct:.2f}% > {MAX_RISK_PCT}%)")
            return None

        tp_roi_pct = risk_pct * REWARD_RATIO * LEVERAGE
        sl_roi_pct = risk_pct * LEVERAGE

        # ── Quality score (0–100) ─────────────────────────────────────
        trend_score  = min(abs(e50 - e200) / e200 / 0.03, 1.0)          # EMA separation
        rsi_score    = min(abs(rsi_val - 50) / 30.0, 1.0)               # RSI distance from 50
        vol_score    = min((vol_cur / (VOLUME_MIN_MULT * vol_avg) - 1) / 1.5, 1.0)  # vol excess

        score = round(
            (0.40 * trend_score +
             0.35 * rsi_score  +
             0.25 * vol_score) * 100,
            1,
        )

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{tp_roi_pct:.1f}% ROI) "
            f"SL={sl_price:.6g} (-{sl_roi_pct:.1f}% ROI) | "
            f"sweep={swept_level:.6g} risk={risk_pct:.3f}% RSI={rsi_val:.1f} score={score}"
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
                f"1H EMA{EMA_50}/EMA{EMA_200} → 15M Sweep → 5M | "
                f"TP1=${tp1:,.6g} TP2=${tp_price:,.6g}"
            ),
            generated_at      = datetime.now(timezone.utc),
            score             = score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
