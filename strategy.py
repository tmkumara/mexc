"""
Supertrend + EMA200 trend filter on 1h candles.

Signal rules:
  LONG:  Supertrend flips bullish (direction -1 → +1)
         AND close > EMA200  (macro uptrend)

  SHORT: Supertrend flips bearish (direction +1 → -1)
         AND close < EMA200  (macro downtrend)

Risk management:
  SL  = Supertrend band value at signal bar (ATR-adaptive stop)
  TP  = Entry ± REWARD_RATIO × |Entry − SL|   (default 2:1 R:R)
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

# Need enough history for EMA200 to stabilise
KLINE_COUNT = 250


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
    Run Supertrend + EMA200 analysis on one symbol.
    Returns a Signal dataclass on a fresh direction flip, else None.
    """
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < EMA_TREND_PERIOD + 20:
            logger.debug(f"{symbol}: not enough candles ({len(df)})")
            return None

        # ── indicators ────────────────────────────────────────────
        df.ta.supertrend(length=ST_LENGTH, multiplier=ST_MULTIPLIER, append=True)
        df["ema200"] = df.ta.ema(length=EMA_TREND_PERIOD)

        # Locate the dynamically-named Supertrend columns
        dir_cols = [c for c in df.columns if c.startswith("SUPERTd_")]
        val_cols = [c for c in df.columns if c.startswith("SUPERT_") and not c.startswith(("SUPERTd_", "SUPERTl_", "SUPERTs_"))]

        if not dir_cols or not val_cols:
            logger.warning(f"{symbol}: Supertrend columns missing. Got: {list(df.columns)}")
            return None

        dir_col = dir_cols[0]
        val_col = val_cols[0]

        # ── last *completed* candle (iloc[-2]); [-1] may still be forming ──
        idx = -2

        st_dir_cur  = df[dir_col].iloc[idx]
        st_dir_prev = df[dir_col].iloc[idx - 1]
        st_val      = df[val_col].iloc[idx]
        close       = float(df["close"].iloc[idx])
        ema200      = float(df["ema200"].iloc[idx])

        if pd.isna(st_dir_cur) or pd.isna(st_dir_prev) or pd.isna(st_val) or pd.isna(ema200):
            return None

        st_dir_cur  = int(st_dir_cur)
        st_dir_prev = int(st_dir_prev)
        st_val      = float(st_val)

        # ── direction flip ─────────────────────────────────────────
        flipped_bullish = (st_dir_prev == -1) and (st_dir_cur == 1)
        flipped_bearish = (st_dir_prev ==  1) and (st_dir_cur == -1)

        if flipped_bullish and close > ema200:
            direction = "LONG"
        elif flipped_bearish and close < ema200:
            direction = "SHORT"
        else:
            logger.debug(
                f"{symbol} | dir={st_dir_cur} prev={st_dir_prev} "
                f"flip_bull={flipped_bullish} flip_bear={flipped_bearish} "
                f"close={close:.4f} ema200={ema200:.4f}"
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
            f"[SIGNAL] {direction} {symbol} | entry={entry} "
            f"SL={sl_price} TP={tp_price} | "
            f"risk={risk_pct*100:.2f}% sl_roi={sl_roi:.1f}% tp_roi={tp_roi:.1f}%"
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
                f"Supertrend({ST_LENGTH}, {ST_MULTIPLIER}) + EMA{EMA_TREND_PERIOD} | {TIMEFRAME}"
            ),
            generated_at      = datetime.now(timezone.utc),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
