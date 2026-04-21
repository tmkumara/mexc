"""
Hull Suite by inSilico on 15m candles.

Signal conditions:
  LONG  — Hull bar flips red → green  (HMA crosses above HMA[2])
  SHORT — Hull bar flips green → red  (HMA crosses below HMA[2])

Risk management (fixed ROI targets):
  TP = entry ± (TP_ROI_PCT / 100 / LEVERAGE) × entry
  SL = entry ∓ (SL_ROI_PCT / 100 / LEVERAGE) × entry
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta  # noqa: F401

from mexc_client import get_klines
from config import LEVERAGE, TIMEFRAME, HULL_LENGTH, TP_ROI_PCT, SL_ROI_PCT

logger = logging.getLogger(__name__)

# Enough bars to warm up WMA(55) + WMA(27) + WMA(8) + 5 look-back
KLINE_COUNT = 200


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


def _hull_suite(close: pd.Series, length: int) -> pd.Series:
    """
    Hull Suite by inSilico.
    Returns HMA series; bar is 'green' when hma > hma.shift(2).
    """
    half     = max(1, length // 2)
    sqrt_len = max(1, round(length ** 0.5))

    wma_half = ta.wma(close, length=half)
    wma_full = ta.wma(close, length=length)
    diff     = 2 * wma_half - wma_full
    hma      = ta.wma(diff, length=sqrt_len)
    return hma


def analyze_coin(symbol: str) -> Signal | None:
    """Run Hull Suite analysis on one symbol. Returns Signal on color flip, else None."""
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < HULL_LENGTH + 30:
            logger.debug(f"{symbol}: not enough candles ({len(df)})")
            return None

        hma      = _hull_suite(df["close"], HULL_LENGTH)
        is_green = hma > hma.shift(2)   # True = green bar, False = red bar

        # Use last *completed* candle (idx=-2); idx=-1 may still be forming
        if pd.isna(hma.iloc[-2]) or pd.isna(hma.iloc[-4]) or \
           pd.isna(hma.iloc[-3]) or pd.isna(hma.iloc[-5]):
            return None

        cur_green  = bool(is_green.iloc[-2])
        prev_green = bool(is_green.iloc[-3])

        if not prev_green and cur_green:
            direction = "LONG"
        elif prev_green and not cur_green:
            direction = "SHORT"
        else:
            return None

        entry        = float(df["close"].iloc[-2])
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
            f"TP={tp_price} (+{TP_ROI_PCT}% ROI) SL={sl_price} (-{SL_ROI_PCT}% ROI)"
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
            timeframe_summary = f"Hull Suite({HULL_LENGTH}) | {TIMEFRAME}",
            generated_at      = datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
