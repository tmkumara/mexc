"""
RSI Heatmap + Fibonacci Golden Pocket + EMA/MACD/RSI scalping strategy.

Coin selection (coin_scanner, 4h RSI):
  Oversold  (RSI < 40) → LONG candidates
  Overbought (RSI > 65) → SHORT candidates

Per-coin signal logic (3-tier multi-timeframe):

  Tier 1 — Fibonacci qualification (1h):
    Find the most recent impulse swing (A→B for LONG, C→D for SHORT)
    with at least FIB_MIN_IMPULSE_PCT size. Current price must be in the
    golden pocket: 61.8%–78.6% retracement of that impulse.
    TP = 1.272 Fibonacci extension beyond the swing high/low.
    SL = FIB_SL_BUFFER_PCT beyond the swing anchor (A for LONG, C for SHORT).
    Gates: TP_ROI >= MIN_TP_ROI_PCT (50%) and R:R >= MIN_RR_RATIO (2:1).

  Tier 2 — Entry timing (15m):
    EMA(9) crosses EMA(21) in signal direction.
    Close on correct side of EMA(200).
    RSI(14) > 50 (LONG) or < 50 (SHORT).
    MACD histogram > 0 (LONG) or < 0 (SHORT).
    Volume >= VOLUME_MIN_MULT × 20-bar MA.

  Tier 3 — Quality score (0–100):
    MACD histogram strength  25%
    RSI distance from 50     20%
    FVG confluence (1h)      20%
    EMA(9/21) spread         20%
    Volume ratio             15%
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta

from mexc_client import get_klines
from config import (
    LEVERAGE, TIMEFRAME,
    EMA_FAST, EMA_SLOW, EMA_TREND,
    RSI_PERIOD,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL_PERIOD,
    VOLUME_MA_BARS, VOLUME_MIN_MULT,
    FIB_HTF, FIB_LOOKBACK, FIB_PIVOT_STRENGTH,
    FIB_MIN_IMPULSE_PCT, FIB_GOLDEN_LOW, FIB_GOLDEN_HIGH,
    FIB_TP_EXTENSION, FIB_SL_BUFFER_PCT,
    MIN_TP_ROI_PCT, MIN_RR_RATIO,
)

logger = logging.getLogger(__name__)

KLINE_COUNT = EMA_TREND + 100          # 300 candles for 15m indicators
BTC_EMA_PROXIMITY_PCT: float = 0.005   # 0.5% band — BTC bias treated as ambiguous


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


# ── Fibonacci helpers ─────────────────────────────────────────────

def _find_pivots(high: pd.Series, low: pd.Series, strength: int) -> tuple[pd.Series, pd.Series]:
    """
    Return boolean Series marking pivot highs and pivot lows.
    A pivot high at index i: high[i] is the max over [i-strength, i+strength].
    A pivot low  at index i: low[i]  is the min over [i-strength, i+strength].
    """
    n = len(high)
    pivot_high = pd.Series(False, index=high.index)
    pivot_low  = pd.Series(False, index=low.index)

    for i in range(strength, n - strength):
        window_high = high.iloc[i - strength: i + strength + 1]
        window_low  = low.iloc[i  - strength: i + strength + 1]
        if high.iloc[i] == window_high.max():
            pivot_high.iloc[i] = True
        if low.iloc[i] == window_low.min():
            pivot_low.iloc[i] = True

    return pivot_high, pivot_low


def _get_fibonacci_zone(symbol: str, direction: str) -> dict | None:
    """
    Fetch 1h klines, detect the most recent significant impulse swing,
    check if current price is in the 61.8–78.6% golden pocket, and
    compute dynamic TP (1.272 extension) and SL (beyond swing anchor).

    Returns dict with keys: sl_price, tp_price, tp_roi_pct, sl_roi_pct,
    swing_a, swing_b  — or None if conditions are not met.
    """
    try:
        df = get_klines(symbol, FIB_HTF, count=FIB_LOOKBACK)
        if df.empty or len(df) < FIB_PIVOT_STRENGTH * 2 + 10:
            logger.debug(f"{symbol}: insufficient 1h candles for Fibonacci")
            return None

        high = df["high"]
        low  = df["low"]

        pivot_high, pivot_low = _find_pivots(high, low, FIB_PIVOT_STRENGTH)

        ph_idx = [i for i in range(len(df)) if pivot_high.iloc[i]]
        pl_idx = [i for i in range(len(df)) if pivot_low.iloc[i]]

        current_price = float(df["close"].iloc[-2])

        if direction == "LONG":
            # Need: pivot_low (A) followed by pivot_high (B), price now retracing
            best = None
            for bi in reversed(ph_idx):
                b_price = float(high.iloc[bi])
                # Find the most recent pivot low BEFORE this pivot high
                prior_lows = [li for li in pl_idx if li < bi]
                if not prior_lows:
                    continue
                ai = prior_lows[-1]
                a_price = float(low.iloc[ai])
                impulse_pct = (b_price - a_price) / a_price * 100
                if impulse_pct < FIB_MIN_IMPULSE_PCT:
                    continue
                best = (a_price, b_price)
                break

            if best is None:
                logger.debug(f"{symbol}: no valid LONG impulse found")
                return None

            a, b = best
            golden_low  = b - FIB_GOLDEN_HIGH * (b - a)   # 78.6% retrace
            golden_high = b - FIB_GOLDEN_LOW  * (b - a)   # 61.8% retrace

            if not (golden_low <= current_price <= golden_high):
                logger.debug(
                    f"{symbol}: price {current_price:.4f} not in LONG golden pocket "
                    f"[{golden_low:.4f}, {golden_high:.4f}]"
                )
                return None

            tp_price = round(a + FIB_TP_EXTENSION * (b - a), 8)
            sl_price = round(a * (1 - FIB_SL_BUFFER_PCT / 100), 8)

        else:  # SHORT
            # Need: pivot_high (C) followed by pivot_low (D), price now retracing up
            best = None
            for di in reversed(pl_idx):
                d_price = float(low.iloc[di])
                prior_highs = [hi for hi in ph_idx if hi < di]
                if not prior_highs:
                    continue
                ci = prior_highs[-1]
                c_price = float(high.iloc[ci])
                impulse_pct = (c_price - d_price) / c_price * 100
                if impulse_pct < FIB_MIN_IMPULSE_PCT:
                    continue
                best = (c_price, d_price)
                break

            if best is None:
                logger.debug(f"{symbol}: no valid SHORT impulse found")
                return None

            c, d = best
            golden_low  = d + FIB_GOLDEN_LOW  * (c - d)   # 61.8% retrace up
            golden_high = d + FIB_GOLDEN_HIGH * (c - d)   # 78.6% retrace up

            if not (golden_low <= current_price <= golden_high):
                logger.debug(
                    f"{symbol}: price {current_price:.4f} not in SHORT golden pocket "
                    f"[{golden_low:.4f}, {golden_high:.4f}]"
                )
                return None

            tp_price = round(c - FIB_TP_EXTENSION * (c - d), 8)
            sl_price = round(c * (1 + FIB_SL_BUFFER_PCT / 100), 8)

        # Validate TP/SL side
        if direction == "LONG" and (tp_price <= current_price or sl_price >= current_price):
            return None
        if direction == "SHORT" and (tp_price >= current_price or sl_price <= current_price):
            return None

        risk   = abs(current_price - sl_price) / current_price
        reward = abs(tp_price - current_price) / current_price

        if risk <= 0 or reward <= 0:
            return None

        rr = reward / risk
        if rr < MIN_RR_RATIO:
            logger.debug(f"{symbol}: R:R {rr:.2f} < {MIN_RR_RATIO}, skipping")
            return None

        tp_roi_pct = reward * LEVERAGE * 100
        sl_roi_pct = risk   * LEVERAGE * 100

        if tp_roi_pct < MIN_TP_ROI_PCT:
            logger.debug(f"{symbol}: TP ROI {tp_roi_pct:.1f}% < {MIN_TP_ROI_PCT}%, skipping")
            return None

        return {
            "sl_price":   sl_price,
            "tp_price":   tp_price,
            "tp_roi_pct": round(tp_roi_pct, 1),
            "sl_roi_pct": round(sl_roi_pct, 1),
        }

    except Exception as e:
        logger.error(f"Fibonacci zone error for {symbol}: {e}", exc_info=True)
        return None


# ── FVG helper ────────────────────────────────────────────────────

def _detect_fvg(df: pd.DataFrame, direction: str, lookback: int = 20) -> float:
    """
    Scan last `lookback` completed 1h candles for a Fair Value Gap near price.
    Returns score 0.0–1.0:
      1.0 → FVG exists and current price is within 0.5% of it
      0.5 → FVG exists but farther (within 2%)
      0.0 → no FVG found
    """
    if len(df) < lookback + 3:
        return 0.0

    current_price = float(df["close"].iloc[-2])
    end = len(df) - 2   # last completed candle index

    for i in range(end - lookback, end - 1):
        if i < 1:
            continue
        h_prev = float(df["high"].iloc[i - 1])
        l_prev = float(df["low"].iloc[i - 1])
        h_next = float(df["high"].iloc[i + 1])
        l_next = float(df["low"].iloc[i + 1])

        if direction == "LONG":
            # Bullish FVG: gap between candle[i-1].high and candle[i+1].low
            if l_next > h_prev:
                gap_mid = (l_next + h_prev) / 2
                proximity = abs(current_price - gap_mid) / current_price
                if proximity < 0.005:
                    return 1.0
                if proximity < 0.02:
                    return 0.5
        else:
            # Bearish FVG: gap between candle[i-1].low and candle[i+1].high
            if h_next < l_prev:
                gap_mid = (h_next + l_prev) / 2
                proximity = abs(current_price - gap_mid) / current_price
                if proximity < 0.005:
                    return 1.0
                if proximity < 0.02:
                    return 0.5

    return 0.0


# ── Main analysis ─────────────────────────────────────────────────

def analyze_coin(symbol: str, direction: str) -> Signal | None:
    """
    Run full 3-tier analysis:
      1. Fibonacci golden pocket check (1h) — hard gate
      2. EMA(9/21/200) + MACD + RSI + Volume on 15m — entry timing
      3. FVG confluence + multi-factor quality score
    Returns Signal or None.
    """
    try:
        # ── Tier 1: Fibonacci zone (1h) ───────────────────────────
        fib = _get_fibonacci_zone(symbol, direction)
        if fib is None:
            return None

        # ── Tier 2: 15m entry timing ──────────────────────────────
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < EMA_TREND + 50:
            logger.debug(f"{symbol}: insufficient 15m candles")
            return None

        close  = df["close"]
        volume = df["volume"]

        ema_fast  = ta.ema(close, length=EMA_FAST)
        ema_slow  = ta.ema(close, length=EMA_SLOW)
        ema_trend = ta.ema(close, length=EMA_TREND)
        rsi       = ta.rsi(close, length=RSI_PERIOD)
        macd_df   = ta.macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL_PERIOD)
        hist_col  = f"MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL_PERIOD}"
        macd_hist = macd_df[hist_col]

        required = [
            ema_fast.iloc[-2],  ema_fast.iloc[-3],
            ema_slow.iloc[-2],  ema_slow.iloc[-3],
            ema_trend.iloc[-2],
            rsi.iloc[-2],
            macd_hist.iloc[-2],
        ]
        if any(pd.isna(v) for v in required):
            return None

        ef_cur  = float(ema_fast.iloc[-2])
        ef_prev = float(ema_fast.iloc[-3])
        es_cur  = float(ema_slow.iloc[-2])
        es_prev = float(ema_slow.iloc[-3])
        et      = float(ema_trend.iloc[-2])
        rsi_cur = float(rsi.iloc[-2])
        hist    = float(macd_hist.iloc[-2])
        entry   = float(close.iloc[-2])
        vol_ma  = float(volume.rolling(VOLUME_MA_BARS).mean().iloc[-2])
        vol_cur = float(volume.iloc[-2])

        crossed_up   = (ef_prev <= es_prev) and (ef_cur > es_cur)
        crossed_down = (ef_prev >= es_prev) and (ef_cur < es_cur)

        if direction == "LONG":
            if not (crossed_up and entry > et and rsi_cur > 50 and hist > 0):
                return None
        else:
            if not (crossed_down and entry < et and rsi_cur < 50 and hist < 0):
                return None

        if vol_ma > 0 and vol_cur < VOLUME_MIN_MULT * vol_ma:
            logger.debug(f"{symbol}: volume filter fail")
            return None

        # ── Tier 3: Quality score ─────────────────────────────────
        # Factor 1 — MACD histogram strength (25%)
        hist_score = min(abs(hist) / (entry * 0.001), 1.0)

        # Factor 2 — RSI distance from 50 (20%)
        rsi_score = min(abs(rsi_cur - 50) / 30.0, 1.0)

        # Factor 3 — FVG confluence on 1h (20%)
        try:
            df_1h   = get_klines(symbol, FIB_HTF, count=50)
            fvg_score = _detect_fvg(df_1h, direction)
        except Exception:
            fvg_score = 0.0

        # Factor 4 — EMA(fast/slow) spread (20%)
        spread_score = min(abs(ef_cur - es_cur) / entry * 100 / 0.5, 1.0)

        # Factor 5 — Volume ratio (15%)
        vol_ratio  = (vol_cur / vol_ma) if vol_ma > 0 else 1.0
        vol_score  = min(vol_ratio / 3.0, 1.0)

        score = round(
            (0.25 * hist_score + 0.20 * rsi_score + 0.20 * fvg_score +
             0.20 * spread_score + 0.15 * vol_score) * 100,
            1,
        )

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"SL={fib['sl_price']:.6g} (-{fib['sl_roi_pct']:.1f}% ROI) "
            f"TP={fib['tp_price']:.6g} (+{fib['tp_roi_pct']:.1f}% ROI) | "
            f"score={score} RSI={rsi_cur:.1f} MACDh={hist:.6f} "
            f"vol={vol_ratio:.1f}x FVG={fvg_score:.1f}"
        )

        return Signal(
            symbol            = symbol,
            direction         = direction,
            entry_price       = entry,
            tp_price          = fib["tp_price"],
            sl_price          = fib["sl_price"],
            leverage          = LEVERAGE,
            tp_roi_pct        = fib["tp_roi_pct"],
            sl_roi_pct        = fib["sl_roi_pct"],
            timeframe_summary = (
                f"Fib(1h)+EMA({EMA_FAST}/{EMA_SLOW}/{EMA_TREND})+"
                f"MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL_PERIOD})+"
                f"RSI({RSI_PERIOD}) | {TIMEFRAME}"
            ),
            generated_at      = datetime.now(timezone.utc),
            score             = score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None


# ── BTC macro bias ────────────────────────────────────────────────

def get_btc_bias() -> str | None:
    """
    Return "LONG" if BTC is above its EMA(200), "SHORT" if below.
    Returns None when data is unavailable or BTC is within
    BTC_EMA_PROXIMITY_PCT of EMA(200) (ambiguous zone → fail-open).
    """
    try:
        df = get_klines("BTC_USDT", TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < EMA_TREND + 50:
            logger.warning("get_btc_bias: insufficient BTC klines")
            return None

        btc_ema200 = ta.ema(df["close"], length=EMA_TREND)
        if pd.isna(btc_ema200.iloc[-2]):
            return None

        btc_close = float(df["close"].iloc[-2])
        ema_val   = float(btc_ema200.iloc[-2])

        proximity = abs(btc_close - ema_val) / ema_val
        if proximity < BTC_EMA_PROXIMITY_PCT:
            return None

        return "LONG" if btc_close > ema_val else "SHORT"

    except Exception as e:
        logger.error(f"get_btc_bias error: {e}", exc_info=True)
        return None
