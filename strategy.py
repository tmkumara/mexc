"""
ZLSMA + Chandelier Exit strategy on 5m candles.

Entry conditions (last completed candle, ALL must be true):
  LONG  — Chandelier Exit flips bullish (-1 → 1)
          AND  close > ZLSMA
          AND  lows of previous ZLSMA_SEPARATION_CANDLES candles all stayed ABOVE ZLSMA
               (no wick touched ZLSMA — confirms clean uptrend separation)

  SHORT — Chandelier Exit flips bearish ( 1 → -1)
          AND  close < ZLSMA
          AND  highs of previous ZLSMA_SEPARATION_CANDLES candles all stayed BELOW ZLSMA
               (no wick touched ZLSMA — confirms clean downtrend separation)

Indicators:
  ZLSMA(200)   — Zero Lag Least Squares MA: lsma = linreg(close,200); zlsma = 2*lsma - linreg(lsma,200)
  CE(1, 2.0)   — Chandelier Exit; ATR period=1, multiplier=2.0
                 LongStop  = highest(close, 1) - 2*ATR(1)  [ratcheted up]
                 ShortStop = lowest(close, 1)  + 2*ATR(1)  [ratcheted down]
                 dir: 1 if close > prev ShortStop, -1 if close < prev LongStop, else prev dir

Risk management (fixed ROI):
  TP = entry ± (TP_ROI_PCT / 100 / LEVERAGE) × entry   (+3% ROI at 20x = +0.15% price)
  SL = entry ∓ (SL_ROI_PCT / 100 / LEVERAGE) × entry   (-10% ROI at 20x = -0.50% price)
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
)

logger = logging.getLogger(__name__)

# linreg applied twice: ZLSMA needs ~2×ZLSMA_LENGTH bars to warm up
KLINE_COUNT = ZLSMA_LENGTH * 2 + 50

# Candles before the signal candle whose wicks must NOT touch ZLSMA.
# Raise to 10 for a stricter "cleanly separated" requirement.
ZLSMA_SEPARATION_CANDLES = 5


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


def _zlsma(close: pd.Series, length: int) -> pd.Series:
    """Zero Lag Least Squares Moving Average."""
    lsma  = ta.linreg(close, length=length)
    lsma2 = ta.linreg(lsma,  length=length)
    return 2 * lsma - lsma2


def _chandelier_exit(df: pd.DataFrame, atr_period: int, mult: float) -> pd.Series:
    """
    Chandelier Exit direction series.
    Returns pd.Series of int: 1 = bullish, -1 = bearish.
    Implements the ratcheting stop mechanism from the original Lazybear indicator.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    atr = ta.atr(high, low, close, length=atr_period)

    # Raw stops (before ratchet)
    raw_long_stop  = close.rolling(atr_period).max() - mult * atr
    raw_short_stop = close.rolling(atr_period).min() + mult * atr

    n = len(df)
    long_stop  = raw_long_stop.copy()
    short_stop = raw_short_stop.copy()
    direction  = pd.Series(np.ones(n, dtype=float), index=df.index)

    for i in range(1, n):
        # Ratchet long stop: only move up when close stays above it
        if pd.notna(long_stop.iloc[i]) and pd.notna(long_stop.iloc[i - 1]):
            if close.iloc[i - 1] > long_stop.iloc[i - 1]:
                long_stop.iloc[i] = max(long_stop.iloc[i], long_stop.iloc[i - 1])

        # Ratchet short stop: only move down when close stays below it
        if pd.notna(short_stop.iloc[i]) and pd.notna(short_stop.iloc[i - 1]):
            if close.iloc[i - 1] < short_stop.iloc[i - 1]:
                short_stop.iloc[i] = min(short_stop.iloc[i], short_stop.iloc[i - 1])

        # Direction: uses previous bar's stop levels
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


def analyze_coin(symbol: str) -> Signal | None:
    """Run ZLSMA + CE analysis on one symbol. Returns Signal on entry flip, else None."""
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < ZLSMA_LENGTH * 2 + 10:
            logger.debug(f"{symbol}: not enough candles ({len(df)})")
            return None

        zlsma = _zlsma(df["close"], ZLSMA_LENGTH)
        ce_dir = _chandelier_exit(df, CE_ATR_PERIOD, CE_ATR_MULT)

        # Use last *completed* candle (idx=-2); idx=-1 may still be forming
        if any(pd.isna(v) for v in [zlsma.iloc[-2], zlsma.iloc[-3],
                                     ce_dir.iloc[-2], ce_dir.iloc[-3]]):
            return None

        close_cur  = float(df["close"].iloc[-2])
        zlsma_cur  = float(zlsma.iloc[-2])
        dir_cur    = int(ce_dir.iloc[-2])
        dir_prev   = int(ce_dir.iloc[-3])

        # CE flip detection
        flipped_bullish = (dir_prev == -1) and (dir_cur == 1)
        flipped_bearish = (dir_prev ==  1) and (dir_cur == -1)

        if not flipped_bullish and not flipped_bearish:
            return None

        # ZLSMA trend filter
        above_zlsma = close_cur > zlsma_cur
        below_zlsma = close_cur < zlsma_cur

        if flipped_bullish and above_zlsma:
            direction = "LONG"
        elif flipped_bearish and below_zlsma:
            direction = "SHORT"
        else:
            logger.debug(
                f"{symbol} | CE flip={'bull' if flipped_bullish else 'bear'} "
                f"but close {'above' if above_zlsma else 'below'} ZLSMA — filtered"
            )
            return None

        # ── ZLSMA separation filter ────────────────────────────────
        # Previous N candles must show clean separation from ZLSMA:
        #   LONG:  lows all above ZLSMA  (no wick touched or crossed it)
        #   SHORT: highs all below ZLSMA (no wick touched or crossed it)
        # Candles checked: idx=-3 back through idx=-(2+ZLSMA_SEPARATION_CANDLES)
        for lookback in range(3, 3 + ZLSMA_SEPARATION_CANDLES):
            candle_zlsma = float(zlsma.iloc[-lookback])
            if pd.isna(candle_zlsma):
                logger.debug(f"{symbol}: NaN ZLSMA at lookback {lookback}, skipping")
                return None
            if direction == "LONG":
                if float(df["low"].iloc[-lookback]) <= candle_zlsma:
                    logger.debug(
                        f"{symbol}: LONG separation failed — low touched ZLSMA "
                        f"{ZLSMA_SEPARATION_CANDLES - (lookback - 3)} candles back"
                    )
                    return None
            else:
                if float(df["high"].iloc[-lookback]) >= candle_zlsma:
                    logger.debug(
                        f"{symbol}: SHORT separation failed — high touched ZLSMA "
                        f"{ZLSMA_SEPARATION_CANDLES - (lookback - 3)} candles back"
                    )
                    return None

        entry         = close_cur
        price_move_tp = entry * (TP_ROI_PCT / 100.0 / LEVERAGE)
        price_move_sl = entry * (SL_ROI_PCT / 100.0 / LEVERAGE)

        if direction == "LONG":
            tp_price = round(entry + price_move_tp, 8)
            sl_price = round(entry - price_move_sl, 8)
        else:
            tp_price = round(entry - price_move_tp, 8)
            sl_price = round(entry + price_move_sl, 8)

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry} | "
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
