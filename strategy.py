"""
Signal strategy: EMA Trend + RSI Pullback + MACD Confirmation
─────────────────────────────────────────────────────────────
Timeframe roles:
  1h  → Trend direction  (EMA20 / EMA50 crossover)
  15m → Momentum confirm (RSI14 + MACD)
  5m  → Entry timing     (price vs EMA20, volume spike)

Long signal conditions:
  1h:  EMA20 > EMA50  AND  close > EMA20
  15m: RSI < 55  AND  RSI rising over last 2 bars  AND  MACD hist > 0
  5m:  close > EMA20  AND  previous close ≤ EMA20 (bounce)  AND  volume > avg*1.3

Short signal conditions (mirror):
  1h:  EMA20 < EMA50  AND  close < EMA20
  15m: RSI > 45  AND  RSI falling over last 2 bars  AND  MACD hist < 0
  5m:  close < EMA20  AND  previous close ≥ EMA20 (rejection)  AND  volume > avg*1.3
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from mexc_client import get_klines
from config import TP_PCT, SL_PCT, LEVERAGE, RISK_PCT

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    direction: str          # "LONG" or "SHORT"
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    risk_pct: float
    tp_roi_pct: float
    sl_roi_pct: float
    timeframe_summary: str
    generated_at: datetime


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.ta.ema(length=20, append=True)
    df.ta.ema(length=50, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    return df


def _trend_1h(symbol: str) -> str | None:
    """Returns 'LONG', 'SHORT', or None if no clear trend."""
    df = get_klines(symbol, "1h", count=60)
    if df.empty or len(df) < 51:
        return None
    df = _add_indicators(df)

    last = df.iloc[-1]
    ema20 = last.get("EMA_20")
    ema50 = last.get("EMA_50")
    close = last["close"]

    if pd.isna(ema20) or pd.isna(ema50):
        return None

    if ema20 > ema50 and close > ema20:
        return "LONG"
    if ema20 < ema50 and close < ema20:
        return "SHORT"
    return None


def _momentum_15m(symbol: str, direction: str) -> bool:
    """Returns True if 15m momentum confirms the direction."""
    df = get_klines(symbol, "15m", count=60)
    if df.empty or len(df) < 35:
        return False
    df = _add_indicators(df)

    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    prev2 = df.iloc[-3]

    rsi      = last.get("RSI_14")
    rsi_prev = prev.get("RSI_14")
    macdh    = last.get("MACDh_12_26_9")

    if pd.isna(rsi) or pd.isna(rsi_prev) or pd.isna(macdh):
        return False

    rsi_rising  = rsi > rsi_prev > prev2.get("RSI_14", rsi_prev)
    rsi_falling = rsi < rsi_prev < prev2.get("RSI_14", rsi_prev)

    if direction == "LONG":
        return rsi < 55 and rsi_rising and macdh > 0
    else:
        return rsi > 45 and rsi_falling and macdh < 0


def _entry_5m(symbol: str, direction: str) -> float | None:
    """
    Returns the entry price if 5m shows a valid bounce/rejection,
    otherwise None.
    """
    df = get_klines(symbol, "5m", count=60)
    if df.empty or len(df) < 22:
        return None
    df = _add_indicators(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    ema20      = last.get("EMA_20")
    close      = last["close"]
    prev_close = prev["close"]
    prev_ema20 = prev.get("EMA_20")

    if pd.isna(ema20) or pd.isna(prev_ema20):
        return None

    # Volume filter: current bar volume > 1.3× recent average
    avg_vol = df["volume"].iloc[-20:-1].mean()
    if avg_vol == 0 or last["volume"] < avg_vol * 1.3:
        return None

    if direction == "LONG":
        # Price crossed above EMA20 on this bar
        bounced = close > ema20 and prev_close <= prev_ema20
        if bounced:
            return close

    else:  # SHORT
        # Price crossed below EMA20 on this bar
        rejected = close < ema20 and prev_close >= prev_ema20
        if rejected:
            return close

    return None


def analyze_coin(symbol: str) -> Signal | None:
    """
    Full multi-timeframe analysis. Returns a Signal if conditions are met,
    otherwise None.
    """
    try:
        # Step 1: 1h trend
        direction = _trend_1h(symbol)
        if not direction:
            return None

        # Step 2: 15m momentum
        if not _momentum_15m(symbol, direction):
            return None

        # Step 3: 5m entry
        entry_price = _entry_5m(symbol, direction)
        if entry_price is None:
            return None

        # Build TP / SL
        if direction == "LONG":
            tp_price = round(entry_price * (1 + TP_PCT), 8)
            sl_price = round(entry_price * (1 - SL_PCT), 8)
        else:
            tp_price = round(entry_price * (1 - TP_PCT), 8)
            sl_price = round(entry_price * (1 + SL_PCT), 8)

        tp_roi = TP_PCT * LEVERAGE * 100
        sl_roi = SL_PCT * LEVERAGE * 100

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            risk_pct=RISK_PCT * 100,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary="Entry: 5m | Confirm: 15m | Trend: 1h",
            generated_at=datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}")
        return None
