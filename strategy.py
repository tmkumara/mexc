"""
Signal strategy: EMA Trend + RSI + MACD + ADX + 4h Macro Alignment
───────────────────────────────────────────────────────────────────
Timeframe roles:
  4h  → Macro trend       (EMA20 / EMA50 crossover)
  1h  → Micro trend + ADX (EMA20 / EMA50 crossover + ADX ≥ 25)
  15m → Momentum confirm  (RSI14 + MACD histogram)
  5m  → Entry timing      (2-bar EMA bounce/rejection + volume ≥ 2× avg)

Long signal conditions:
  4h:  EMA20 > EMA50  AND  close > EMA20            (macro trend up)
  1h:  EMA20 > EMA50  AND  close > EMA20  AND  ADX ≥ 25  (strong uptrend)
  15m: RSI < 55  AND  RSI rising over last 2 bars  AND  MACD hist > 0
  5m:  bar[-3] close ≤ EMA20  →  bar[-2] crossed above  →  bar[-1] confirmed above
       AND  both bar[-2] and bar[-1] volume ≥ avg * 2.0

Short signal conditions (mirror):
  4h:  EMA20 < EMA50  AND  close < EMA20
  1h:  EMA20 < EMA50  AND  close < EMA20  AND  ADX ≥ 25
  15m: RSI > 45  AND  RSI falling over last 2 bars  AND  MACD hist < 0
  5m:  bar[-3] close ≥ EMA20  →  bar[-2] crossed below  →  bar[-1] confirmed below
       AND  both bar[-2] and bar[-1] volume ≥ avg * 2.0

Why this achieves a higher win rate:
  - 4h alignment eliminates counter-trend trades
  - ADX ≥ 25 removes sideways / choppy market signals
  - 2-bar confirmation avoids EMA wicks / fakeouts
  - 2× volume requirement ensures genuine momentum
  - Tight TP (0.15% / +3% ROI) is reached far more often than 0.5%
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from mexc_client import get_klines
from config import TP_PCT, SL_PCT, LEVERAGE, RISK_PCT

logger = logging.getLogger(__name__)

ADX_MIN = 25       # minimum ADX to confirm a strong trend (filters choppy markets)
VOLUME_MULT = 2.0  # entry bars must exceed this multiple of recent average volume


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
    df.ta.adx(length=14, append=True)
    return df


def _trend_4h(symbol: str) -> str | None:
    """Returns 'LONG', 'SHORT', or None — macro trend from 4h chart."""
    df = get_klines(symbol, "4h", count=60)
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


def _trend_1h(symbol: str) -> str | None:
    """Returns 'LONG', 'SHORT', or None — trend + ADX strength check on 1h."""
    df = get_klines(symbol, "1h", count=60)
    if df.empty or len(df) < 51:
        return None
    df = _add_indicators(df)

    last = df.iloc[-1]
    ema20 = last.get("EMA_20")
    ema50 = last.get("EMA_50")
    close = last["close"]
    adx   = last.get("ADX_14")

    if pd.isna(ema20) or pd.isna(ema50) or pd.isna(adx):
        return None

    # Require strong trend — ADX < 25 means choppy / sideways, skip
    if adx < ADX_MIN:
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
    Returns entry price if 5m shows a 2-bar confirmed bounce/rejection.

    Pattern required:
      bar[-3] (before):   on the wrong side of EMA20
      bar[-2] (crossover): price crosses EMA20 with volume ≥ 2× avg
      bar[-1] (confirm):  price stays on the right side with volume ≥ 2× avg

    Entry price = close of bar[-1] (the confirmation bar).
    """
    df = get_klines(symbol, "5m", count=60)
    if df.empty or len(df) < 23:
        return None
    df = _add_indicators(df)

    confirm = df.iloc[-1]   # confirmation bar (completed)
    cross   = df.iloc[-2]   # crossover bar
    before  = df.iloc[-3]   # bar before the crossover

    # Volume baseline: exclude the two entry bars to avoid self-reference
    avg_vol = df["volume"].iloc[-20:-2].mean()
    if avg_vol == 0:
        return None

    cross_vol_ok   = cross["volume"]   >= avg_vol * VOLUME_MULT
    confirm_vol_ok = confirm["volume"] >= avg_vol * VOLUME_MULT
    if not (cross_vol_ok and confirm_vol_ok):
        return None

    cross_ema   = cross.get("EMA_20")
    confirm_ema = confirm.get("EMA_20")
    before_ema  = before.get("EMA_20")

    if pd.isna(cross_ema) or pd.isna(confirm_ema) or pd.isna(before_ema):
        return None

    if direction == "LONG":
        # before was at/below EMA, cross bounced above, confirm stayed above
        crossover = cross["close"] > cross_ema and before["close"] <= before_ema
        confirmed = confirm["close"] > confirm_ema
        if crossover and confirmed:
            return confirm["close"]

    else:  # SHORT
        # before was at/above EMA, cross rejected below, confirm stayed below
        crossover = cross["close"] < cross_ema and before["close"] >= before_ema
        confirmed = confirm["close"] < confirm_ema
        if crossover and confirmed:
            return confirm["close"]

    return None


def analyze_coin(symbol: str) -> Signal | None:
    """
    Full multi-timeframe analysis. Returns a Signal if all conditions are met,
    otherwise None.

    Filter pipeline (fail-fast, cheapest checks first where possible):
      1. 4h macro trend  — eliminates counter-trend setups
      2. 1h micro trend + ADX ≥ 25  — ensures strong momentum
      3. 4h / 1h alignment  — both must agree on direction
      4. 15m RSI + MACD  — confirms momentum on entry TF
      5. 5m 2-bar EMA bounce + 2× volume  — precise, confirmed entry
    """
    try:
        # Step 1: 4h macro trend
        macro = _trend_4h(symbol)
        if not macro:
            return None

        # Step 2: 1h micro trend + ADX
        direction = _trend_1h(symbol)
        if not direction:
            return None

        # Step 3: 4h and 1h must agree
        if macro != direction:
            return None

        # Step 4: 15m momentum
        if not _momentum_15m(symbol, direction):
            return None

        # Step 5: 5m 2-bar confirmed entry
        entry_price = _entry_5m(symbol, direction)
        if entry_price is None:
            return None

        # Build TP / SL prices
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
            timeframe_summary="Entry: 5m (2-bar) | Confirm: 15m | Trend: 1h ADX≥25 | Macro: 4h",
            generated_at=datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}")
        return None
