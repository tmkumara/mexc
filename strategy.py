"""
ZLSMA + Chandelier Exit strategy — two signal modes.

MODE A — Continuation (CE fires while already above/below ZLSMA):
  LONG:  CE flips bullish at last candle  AND  close > ZLSMA
         AND  previous ZLSMA_SEPARATION_CANDLES candle lows all stayed ABOVE ZLSMA

  SHORT: CE flips bearish at last candle  AND  close < ZLSMA
         AND  previous ZLSMA_SEPARATION_CANDLES candle highs all stayed BELOW ZLSMA

MODE B — Crossover confirmation (CE fires first, ZLSMA crossover follows):
  LONG:  CE flipped bullish within last CE_CROSS_LOOKBACK bars while close was BELOW ZLSMA
         AND  last ZLSMA_CROSS_CONFIRM candles all have close ABOVE ZLSMA (confirmed cross)
         AND  CE direction is currently still bullish

  SHORT: CE flipped bearish within last CE_CROSS_LOOKBACK bars while close was ABOVE ZLSMA
         AND  last ZLSMA_CROSS_CONFIRM candles all have close BELOW ZLSMA
         AND  CE direction is currently still bearish

Either mode passing triggers a signal. Mode A is checked first.

Indicators:
  ZLSMA(200) — lsma = linreg(close,200); zlsma = 2*lsma - linreg(lsma,200)
  CE(1, 2.0) — Chandelier Exit, ATR period=1, multiplier=2.0, ratcheting stops

Risk management:
  TP = entry ± (TP_ROI_PCT / 100 / LEVERAGE) × entry
  SL = entry ∓ (SL_ROI_PCT / 100 / LEVERAGE) × entry
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta  # noqa: F401

from mexc_client import get_klines
from config import (
    LEVERAGE, TIMEFRAME,
    ZLSMA_LENGTH, CE_ATR_PERIOD, CE_ATR_MULT,
    TP_ROI_PCT, SL_ROI_PCT,
    ZLSMA_SEPARATION_CANDLES, ZLSMA_CROSS_CONFIRM, CE_CROSS_LOOKBACK,
)

logger = logging.getLogger(__name__)

KLINE_COUNT = ZLSMA_LENGTH * 2 + 50


@dataclass
class Signal:
    symbol:            str
    direction:         str        # "LONG" | "SHORT"
    entry_price:       float
    tp_price:          float
    sl_price:          float
    leverage:          int
    tp_roi_pct:        float
    sl_roi_pct:        float
    timeframe_summary: str
    generated_at:      datetime


# ── Indicator helpers ─────────────────────────────────────────────

def _zlsma(close: pd.Series, length: int) -> pd.Series:
    lsma  = ta.linreg(close, length=length)
    lsma2 = ta.linreg(lsma,  length=length)
    return 2 * lsma - lsma2


def _chandelier_exit(df: pd.DataFrame, atr_period: int, mult: float) -> pd.Series:
    """
    Returns direction series: 1 = bullish, -1 = bearish.
    Implements ratcheting stops from the original Lazybear indicator.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    atr = ta.atr(high, low, close, length=atr_period)

    raw_long_stop  = close.rolling(atr_period).max() - mult * atr
    raw_short_stop = close.rolling(atr_period).min() + mult * atr

    n          = len(df)
    long_stop  = raw_long_stop.copy()
    short_stop = raw_short_stop.copy()
    direction  = pd.Series(np.ones(n, dtype=float), index=df.index)

    for i in range(1, n):
        if pd.notna(long_stop.iloc[i]) and pd.notna(long_stop.iloc[i - 1]):
            if close.iloc[i - 1] > long_stop.iloc[i - 1]:
                long_stop.iloc[i] = max(long_stop.iloc[i], long_stop.iloc[i - 1])

        if pd.notna(short_stop.iloc[i]) and pd.notna(short_stop.iloc[i - 1]):
            if close.iloc[i - 1] < short_stop.iloc[i - 1]:
                short_stop.iloc[i] = min(short_stop.iloc[i], short_stop.iloc[i - 1])

        prev_long  = long_stop.iloc[i - 1]
        prev_short = short_stop.iloc[i - 1]
        prev_dir   = direction.iloc[i - 1]

        if pd.isna(prev_long) or pd.isna(prev_short):
            direction.iloc[i] = prev_dir
        elif close.iloc[i] > prev_short:
            direction.iloc[i] = 1
        elif close.iloc[i] < prev_long:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = prev_dir

    return direction.astype(int)


# ── Signal mode checks ────────────────────────────────────────────

def _check_mode_a(df: pd.DataFrame, zlsma: pd.Series, ce_dir: pd.Series) -> str | None:
    """
    Mode A: CE flips at the last completed candle while price is already
    on the correct side of ZLSMA, with clean separation for previous N candles.
    Returns 'LONG', 'SHORT', or None.
    """
    dir_cur  = int(ce_dir.iloc[-2])
    dir_prev = int(ce_dir.iloc[-3])

    flipped_bullish = (dir_prev == -1) and (dir_cur == 1)
    flipped_bearish = (dir_prev ==  1) and (dir_cur == -1)

    if not flipped_bullish and not flipped_bearish:
        return None

    close_cur = float(df["close"].iloc[-2])
    zlsma_cur = float(zlsma.iloc[-2])

    if flipped_bullish and close_cur > zlsma_cur:
        direction = "LONG"
    elif flipped_bearish and close_cur < zlsma_cur:
        direction = "SHORT"
    else:
        return None

    # Separation filter: previous N candle wicks must not touch ZLSMA
    for lookback in range(3, 3 + ZLSMA_SEPARATION_CANDLES):
        z = float(zlsma.iloc[-lookback])
        if pd.isna(z):
            return None
        if direction == "LONG" and float(df["low"].iloc[-lookback]) <= z:
            logger.debug(f"Mode A LONG sep fail at -{lookback}")
            return None
        if direction == "SHORT" and float(df["high"].iloc[-lookback]) >= z:
            logger.debug(f"Mode A SHORT sep fail at -{lookback}")
            return None

    return direction


def _check_mode_b(df: pd.DataFrame, zlsma: pd.Series, ce_dir: pd.Series) -> str | None:
    """
    Mode B: CE flipped while price was on the wrong side of ZLSMA (CE first),
    followed by ZLSMA_CROSS_CONFIRM consecutive candles confirmed on the correct side.
    Returns 'LONG', 'SHORT', or None.
    """
    # CE must currently be pointing in a direction
    cur_ce = int(ce_dir.iloc[-2])

    for signal_dir, ce_signal, zlsma_side in [
        ("LONG",  1, lambda c, z: c > z),
        ("SHORT", -1, lambda c, z: c < z),
    ]:
        if cur_ce != ce_signal:
            continue

        # Last ZLSMA_CROSS_CONFIRM candles must all be on correct side of ZLSMA
        confirmed = True
        for i in range(2, 2 + ZLSMA_CROSS_CONFIRM):
            if pd.isna(zlsma.iloc[-i]):
                confirmed = False
                break
            if not zlsma_side(float(df["close"].iloc[-i]), float(zlsma.iloc[-i])):
                confirmed = False
                break
        if not confirmed:
            continue

        # Search back from before the confirmation window for a CE flip
        # that occurred while price was on the OPPOSITE side of ZLSMA
        start = 2 + ZLSMA_CROSS_CONFIRM
        for k in range(start, start + CE_CROSS_LOOKBACK):
            if k + 1 >= len(df):
                break
            if any(pd.isna(v) for v in [
                ce_dir.iloc[-k], ce_dir.iloc[-(k + 1)], zlsma.iloc[-k]
            ]):
                break

            ce_k      = int(ce_dir.iloc[-k])
            ce_k_prev = int(ce_dir.iloc[-(k + 1)])
            flipped   = (ce_k == ce_signal) and (ce_k_prev == -ce_signal)

            if flipped:
                # Flip must have occurred while price was on the OPPOSITE side
                close_at_flip = float(df["close"].iloc[-k])
                zlsma_at_flip = float(zlsma.iloc[-k])
                wrong_side = not zlsma_side(close_at_flip, zlsma_at_flip)
                if wrong_side:
                    return signal_dir
                # Found a flip but it was on the correct side already → stop search
                break

    return None


# ── Main analysis ─────────────────────────────────────────────────

def analyze_coin(symbol: str) -> Signal | None:
    """Run ZLSMA + CE analysis (Mode A and B). Returns first matching Signal, else None."""
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < ZLSMA_LENGTH * 2 + 10:
            logger.debug(f"{symbol}: not enough candles ({len(df)})")
            return None

        zlsma  = _zlsma(df["close"], ZLSMA_LENGTH)
        ce_dir = _chandelier_exit(df, CE_ATR_PERIOD, CE_ATR_MULT)

        min_idx = 2 + max(ZLSMA_SEPARATION_CANDLES, ZLSMA_CROSS_CONFIRM + CE_CROSS_LOOKBACK)
        if any(pd.isna(v) for v in [zlsma.iloc[-2], zlsma.iloc[-3],
                                     ce_dir.iloc[-2], ce_dir.iloc[-3]]):
            return None

        # Try Mode A first, then Mode B
        direction = _check_mode_a(df, zlsma, ce_dir)
        mode = "A"
        if direction is None:
            direction = _check_mode_b(df, zlsma, ce_dir)
            mode = "B"

        if direction is None:
            return None

        entry         = float(df["close"].iloc[-2])
        zlsma_cur     = float(zlsma.iloc[-2])
        price_move_tp = entry * (TP_ROI_PCT / 100.0 / LEVERAGE)
        price_move_sl = entry * (SL_ROI_PCT / 100.0 / LEVERAGE)

        if direction == "LONG":
            tp_price = round(entry + price_move_tp, 8)
            sl_price = round(entry - price_move_sl, 8)
        else:
            tp_price = round(entry - price_move_tp, 8)
            sl_price = round(entry + price_move_sl, 8)

        logger.info(
            f"[SIGNAL/{mode}] {direction} {symbol} @ {entry} | "
            f"TP={tp_price} (+{TP_ROI_PCT}% ROI) SL={sl_price} (-{SL_ROI_PCT}% ROI) | "
            f"ZLSMA={zlsma_cur:.6g}"
        )

        return Signal(
            symbol            = symbol,
            direction         = direction,
            entry_price       = entry,
            tp_price          = tp_price,
            sl_price          = sl_price,
            leverage          = LEVERAGE,
            tp_roi_pct        = TP_ROI_PCT,
            sl_roi_pct        = SL_ROI_PCT,
            timeframe_summary = f"ZLSMA({ZLSMA_LENGTH}) + CE({CE_ATR_PERIOD}, {CE_ATR_MULT}) | {TIMEFRAME}",
            generated_at      = datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
