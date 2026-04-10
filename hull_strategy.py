"""
Signal strategy: Hull Suite by Insilico — HMA 22 + HMA 55 on 15m
──────────────────────────────────────────────────────────────────
Hull Moving Average color (same as Hull Suite Pine Script):
  Green : HMA[i] > HMA[i-1]   (slope rising)
  Red   : HMA[i] < HMA[i-1]   (slope falling)

Long signal  (in order):
  1. HMA22 turns green  (was red the bar before)
  2. HMA55 turns green  (was red the bar before, while HMA22 is already green)
  → Signal fires on the bar that HMA55 turns green

Short signal (mirror):
  1. HMA22 turns red
  2. HMA55 turns red   (while HMA22 is already red)
  → Signal fires on the bar that HMA55 turns red
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta  # noqa: F401 — registers df.ta extension

from mexc_client import get_klines
from config import TP_PCT, SL_PCT, LEVERAGE, RISK_PCT

logger = logging.getLogger(__name__)

TIMEFRAME   = "15m"
KLINE_COUNT = 120   # enough history for HMA 55 + warm-up


@dataclass
class Signal:
    symbol:           str
    direction:        str        # "LONG" or "SHORT"
    entry_price:      float
    tp_price:         float
    sl_price:         float
    leverage:         int
    risk_pct:         float
    tp_roi_pct:       float
    sl_roi_pct:       float
    timeframe_summary: str
    generated_at:     datetime


def _hma(df: pd.DataFrame, length: int) -> pd.Series:
    """Return Hull Moving Average series (NaN-safe)."""
    col = f"HMA_{length}"
    df.ta.hma(length=length, append=True)
    return df[col]


def analyze_coin(symbol: str) -> Signal | None:
    """
    15m Hull Suite analysis.
    Returns a Signal when HMA55 changes color in the same direction as HMA22,
    i.e. HMA22 already flipped first (confirming direction), then HMA55 follows.
    """
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < 70:          # need warm-up for HMA 55
            return None

        # ── compute HMAs ──────────────────────────────────────────
        hma22 = _hma(df, 22)
        hma55 = _hma(df, 55)

        if hma22.isna().all() or hma55.isna().all():
            return None

        # ── slope / color ─────────────────────────────────────────
        # True = green (rising), False = red (falling)
        green22 = hma22 > hma22.shift(1)
        green55 = hma55 > hma55.shift(1)

        # detect color flip on HMA55
        turned_green55 = green55  & ~green55.shift(1).fillna(False)
        turned_red55   = ~green55 &  green55.shift(1).fillna(True)

        # ── evaluate the last *completed* bar (iloc[-2]) ──────────
        # iloc[-1] may be an incomplete in-progress candle; use [-2] as the
        # last closed bar and [-3] as the bar before it.
        idx = -2

        hma22_is_green  = bool(green22.iloc[idx])
        hma22_is_red    = not hma22_is_green

        hma55_went_green = bool(turned_green55.iloc[idx])
        hma55_went_red   = bool(turned_red55.iloc[idx])

        # ── signal conditions ─────────────────────────────────────
        logger.debug(
            f"{symbol} | hma22_green={hma22_is_green} "
            f"hma55_turned_green={hma55_went_green} "
            f"hma55_turned_red={hma55_went_red}"
        )

        if hma55_went_green and hma22_is_green:
            direction = "LONG"
        elif hma55_went_red and hma22_is_red:
            direction = "SHORT"
        else:
            return None

        entry_price = float(df["close"].iloc[idx])

        if direction == "LONG":
            tp_price = round(entry_price * (1 + TP_PCT), 8)
            sl_price = round(entry_price * (1 - SL_PCT), 8)
        else:
            tp_price = round(entry_price * (1 - TP_PCT), 8)
            sl_price = round(entry_price * (1 + SL_PCT), 8)

        tp_roi = TP_PCT * LEVERAGE * 100
        sl_roi = SL_PCT * LEVERAGE * 100

        return Signal(
            symbol           = symbol,
            direction        = direction,
            entry_price      = entry_price,
            tp_price         = tp_price,
            sl_price         = sl_price,
            leverage         = LEVERAGE,
            risk_pct         = RISK_PCT * 100,
            tp_roi_pct       = tp_roi,
            sl_roi_pct       = sl_roi,
            timeframe_summary= "Hull Suite | HMA 22 + HMA 55 | 15m",
            generated_at     = datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}")
        return None
