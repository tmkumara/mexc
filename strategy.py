"""
Supertrend + EMA200 + 4-filter fakeout guard on 1h candles.

Entry conditions (ALL must be true):
  1. Supertrend flips direction  (-1 → +1 for LONG, +1 → -1 for SHORT)
  2. Close is on the correct side of EMA200
  3. RSI(14) confirms momentum  (> 50 LONG / < 50 SHORT)
  4. [Fakeout filter] Previous candle also on correct side of EMA200
                      (2 consecutive closes — kills single-candle spikes)
  5. [Fakeout filter] Signal candle body ratio >= 50% of candle range
                      AND body direction matches signal
                      (kills wick-dominated / doji spikes)
  6. [Fakeout filter] ADX(14) >= 25
                      (market must be in a trending state, not sideways chop)
  7. [Fakeout filter] Volume >= 1.5 × 20-bar volume MA
                      (genuine participation, not low-volume fake move)

Risk management:
  SL = Supertrend band value (ATR-adaptive dynamic stop)
  TP = Entry ± REWARD_RATIO × |Entry − SL|   (default 2:1 R:R)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta  # noqa: F401 — registers df.ta extension

from mexc_client import get_klines
from config import (
    LEVERAGE,
    TIMEFRAME,
    ST_LENGTH,
    ST_MULTIPLIER,
    EMA_TREND_PERIOD,
    REWARD_RATIO,
)

logger = logging.getLogger(__name__)

# Indicator periods
RSI_PERIOD      = 14
ADX_PERIOD      = 14
VOLUME_MA_BARS  = 20
VOLUME_MIN_MULT = 1.5   # volume must be >= 1.5× the 20-bar MA
BODY_RATIO_MIN  = 0.50  # signal candle body must be >= 50% of its range
ADX_MIN         = 25    # ADX below this = sideways, no trade

# Need enough history: EMA200 warm-up + ADX/RSI headroom
KLINE_COUNT = 300


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


def analyze_coin(symbol: str) -> Signal | None:
    """
    Run full signal analysis on one symbol.
    Returns a Signal on a confirmed trend flip, None otherwise.
    """
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < EMA_TREND_PERIOD + 30:
            logger.debug(f"{symbol}: not enough candles ({len(df)})")
            return None

        # ── compute indicators ────────────────────────────────────
        df.ta.supertrend(length=ST_LENGTH, multiplier=ST_MULTIPLIER, append=True)
        df["ema200"] = df.ta.ema(length=EMA_TREND_PERIOD)
        df["rsi"]    = df.ta.rsi(length=RSI_PERIOD)
        df.ta.adx(length=ADX_PERIOD, append=True)
        df["vol_ma"] = df["volume"].rolling(VOLUME_MA_BARS).mean()

        # Locate dynamic Supertrend column names
        dir_cols = [c for c in df.columns if c.startswith("SUPERTd_")]
        val_cols = [c for c in df.columns if c.startswith("SUPERT_")
                    and not c.startswith(("SUPERTd_", "SUPERTl_", "SUPERTs_"))]
        adx_cols = [c for c in df.columns if c.startswith("ADX_")
                    and not c.startswith(("ADX_D", "ADX_I"))]

        if not dir_cols or not val_cols or not adx_cols:
            logger.warning(f"{symbol}: missing indicator columns — {list(df.columns)}")
            return None

        dir_col = dir_cols[0]
        val_col = val_cols[0]
        adx_col = adx_cols[0]

        # ── last *completed* candle idx=-2; idx=-1 may still be forming ──
        idx = -2

        # Supertrend
        st_dir_cur  = df[dir_col].iloc[idx]
        st_dir_prev = df[dir_col].iloc[idx - 1]
        st_val      = df[val_col].iloc[idx]

        # Price / trend
        close        = float(df["close"].iloc[idx])
        open_        = float(df["open"].iloc[idx])
        high_        = float(df["high"].iloc[idx])
        low_         = float(df["low"].iloc[idx])
        close_prev   = float(df["close"].iloc[idx - 1])
        ema200       = float(df["ema200"].iloc[idx])
        ema200_prev  = float(df["ema200"].iloc[idx - 1])

        # Momentum / volatility
        rsi          = float(df["rsi"].iloc[idx])
        adx          = float(df[adx_col].iloc[idx])
        vol_cur      = float(df["volume"].iloc[idx])
        vol_ma       = float(df["vol_ma"].iloc[idx])

        # Skip if any required value is NaN
        if any(pd.isna(v) for v in [st_dir_cur, st_dir_prev, st_val,
                                     ema200, ema200_prev, rsi, adx, vol_ma]):
            return None

        st_dir_cur  = int(st_dir_cur)
        st_dir_prev = int(st_dir_prev)
        st_val      = float(st_val)

        # ── 1. Direction flip ──────────────────────────────────────
        flipped_bullish = (st_dir_prev == -1) and (st_dir_cur == 1)
        flipped_bearish = (st_dir_prev ==  1) and (st_dir_cur == -1)

        if not flipped_bullish and not flipped_bearish:
            return None

        # ── 2. EMA200 side ─────────────────────────────────────────
        above_ema = close > ema200
        below_ema = close < ema200

        # ── 3. RSI momentum confirmation ───────────────────────────
        rsi_bull = rsi > 50
        rsi_bear = rsi < 50

        # ── Fakeout filter 1: 2 consecutive closes vs EMA200 ───────
        # Requires PREVIOUS candle to also be on the correct side.
        # Eliminates single-candle spikes that immediately reverse.
        consec_above = above_ema and (close_prev > ema200_prev)
        consec_below = below_ema and (close_prev < ema200_prev)

        # ── Fakeout filter 2: candle body quality ──────────────────
        # Body must be >= BODY_RATIO_MIN of the full candle range AND
        # must point in the signal direction (bullish body for LONG, etc.)
        # Eliminates wick-dominated doji/pin-bar fakeouts.
        candle_range = high_ - low_
        body_size    = abs(close - open_)
        body_ratio   = (body_size / candle_range) if candle_range > 0 else 0
        body_ok      = body_ratio >= BODY_RATIO_MIN
        bullish_body = close > open_
        bearish_body = close < open_

        # ── Fakeout filter 3: ADX trend strength ───────────────────
        # ADX < ADX_MIN = sideways / choppy market → skip entirely.
        # Prevents Supertrend from whipsawing during consolidation.
        adx_ok = adx >= ADX_MIN

        # ── Fakeout filter 4: volume participation ─────────────────
        # Signal candle must have meaningful volume vs recent average.
        # Low-volume spikes through EMA200 are institutional stop hunts,
        # not genuine trend changes.
        vol_ok = (vol_ma > 0) and (vol_cur >= VOLUME_MIN_MULT * vol_ma)

        # ── Combine all conditions ─────────────────────────────────
        if (flipped_bullish and above_ema and rsi_bull
                and consec_above and body_ok and bullish_body
                and adx_ok and vol_ok):
            direction = "LONG"

        elif (flipped_bearish and below_ema and rsi_bear
                and consec_below and body_ok and bearish_body
                and adx_ok and vol_ok):
            direction = "SHORT"

        else:
            logger.debug(
                f"{symbol} | flip={'bull' if flipped_bullish else 'bear'} "
                f"ema_side={'above' if above_ema else 'below'} "
                f"rsi={rsi:.1f} consec={'✓' if (consec_above or consec_below) else '✗'} "
                f"body={body_ratio:.2f}({'✓' if body_ok else '✗'}) "
                f"adx={adx:.1f}({'✓' if adx_ok else '✗'}) "
                f"vol={vol_cur/vol_ma:.2f}x({'✓' if vol_ok else '✗'})"
            )
            return None

        # ── price targets ──────────────────────────────────────────
        entry = close
        risk  = abs(entry - st_val)

        if risk == 0:
            logger.debug(f"{symbol}: zero risk distance, skipping")
            return None

        if direction == "LONG":
            sl_price = round(st_val, 8)
            tp_price = round(entry + REWARD_RATIO * risk, 8)
        else:
            sl_price = round(st_val, 8)
            tp_price = round(entry - REWARD_RATIO * risk, 8)

        risk_pct = risk / entry
        sl_roi   = risk_pct * LEVERAGE * 100
        tp_roi   = risk_pct * REWARD_RATIO * LEVERAGE * 100

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry} | "
            f"SL={sl_price} TP={tp_price} | "
            f"risk={risk_pct*100:.2f}% ROI: +{tp_roi:.1f}% / -{sl_roi:.1f}% | "
            f"RSI={rsi:.1f} ADX={adx:.1f} VOL={vol_cur/vol_ma:.1f}x body={body_ratio:.2f}"
        )

        return Signal(
            symbol            = symbol,
            direction         = direction,
            entry_price       = entry,
            tp_price          = tp_price,
            sl_price          = sl_price,
            leverage          = LEVERAGE,
            tp_roi_pct        = tp_roi,
            sl_roi_pct        = sl_roi,
            timeframe_summary = (
                f"Supertrend({ST_LENGTH},{ST_MULTIPLIER}) + EMA{EMA_TREND_PERIOD} | {TIMEFRAME}"
            ),
            generated_at      = datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
