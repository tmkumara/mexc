"""
Multi-timeframe strategy (2-tier, fixed TP/SL).

Tier 1 — Daily (1D):
  Price above EMA(50) → LONG  |  below → SHORT
  (macro trend direction)

Tier 2 — Entry (15m):
  EMA(9) crosses above EMA(21)  OR  RSI(14) < 35   → LONG entry
  EMA(9) crosses below EMA(21)  OR  RSI(14) > 65   → SHORT entry

Fixed TP/SL at 20x leverage:
  TP = +0.25 % price move  →  +5 % ROI
  SL = -0.50 % price move  →  -10 % ROI

Quality score (0–100):
  15m RSI distance from 50  40 %
  EMA(9/21) spread          35 %
  Daily EMA distance        25 %
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from mexc_client import get_klines
from config import (
    LEVERAGE, ENTRY_TF, MTF_1D,
    EMA_FAST, EMA_SLOW, EMA_DAILY,
    RSI_PERIOD, RSI_ENTRY_OVERSOLD, RSI_ENTRY_OVERBOUGHT,
    TP_ROI_PCT, SL_ROI_PCT, TP_PRICE_PCT, SL_PRICE_PCT,
)

logger = logging.getLogger(__name__)

KLINE_1D_COUNT:    int = 60    # covers EMA(50) + buffer
KLINE_ENTRY_COUNT: int = 100   # covers EMA(21) + RSI(14) warm-up


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


def analyze_coin(symbol: str) -> Signal | None:
    """2-tier MTF analysis. Returns Signal or None."""
    try:
        # ── Tier 1: Daily macro trend ─────────────────────────────
        df_1d = get_klines(symbol, MTF_1D, count=KLINE_1D_COUNT)
        if df_1d.empty or len(df_1d) < EMA_DAILY + 5:
            logger.debug(f"{symbol}: insufficient daily candles ({len(df_1d)})")
            return None

        ema_daily = ta.ema(df_1d["close"], length=EMA_DAILY)
        if pd.isna(ema_daily.iloc[-2]):
            return None

        daily_close = float(df_1d["close"].iloc[-2])
        ema_d_val   = float(ema_daily.iloc[-2])

        if   daily_close > ema_d_val:
            direction = "LONG"
        elif daily_close < ema_d_val:
            direction = "SHORT"
        else:
            return None

        daily_ema_dist = abs(daily_close - ema_d_val) / ema_d_val   # fraction

        # ── Tier 2: 15m entry trigger ─────────────────────────────
        df = get_klines(symbol, ENTRY_TF, count=KLINE_ENTRY_COUNT)
        if df.empty or len(df) < EMA_SLOW + 10:
            logger.debug(f"{symbol}: insufficient 15m candles ({len(df)})")
            return None

        close = df["close"]

        ema_fast = ta.ema(close, length=EMA_FAST)
        ema_slow = ta.ema(close, length=EMA_SLOW)
        rsi_15m  = ta.rsi(close, length=RSI_PERIOD)

        required = [
            ema_fast.iloc[-2], ema_fast.iloc[-3],
            ema_slow.iloc[-2], ema_slow.iloc[-3],
            rsi_15m.iloc[-2],
        ]
        if any(pd.isna(v) for v in required):
            return None

        ef_cur  = float(ema_fast.iloc[-2])
        ef_prev = float(ema_fast.iloc[-3])
        es_cur  = float(ema_slow.iloc[-2])
        es_prev = float(ema_slow.iloc[-3])
        rsi_cur = float(rsi_15m.iloc[-2])
        entry   = float(close.iloc[-2])

        crossed_up   = (ef_prev <= es_prev) and (ef_cur > es_cur)
        crossed_down = (ef_prev >= es_prev) and (ef_cur < es_cur)
        oversold     = rsi_cur < RSI_ENTRY_OVERSOLD
        overbought   = rsi_cur > RSI_ENTRY_OVERBOUGHT

        if direction == "LONG"  and not (crossed_up  or oversold):
            logger.debug(
                f"{symbol}: no LONG trigger "
                f"(cross={crossed_up}, RSI={rsi_cur:.1f})"
            )
            return None
        if direction == "SHORT" and not (crossed_down or overbought):
            logger.debug(
                f"{symbol}: no SHORT trigger "
                f"(cross={crossed_down}, RSI={rsi_cur:.1f})"
            )
            return None

        # ── Fixed TP / SL ──────────────────────────────────────────
        if direction == "LONG":
            tp_price = round(entry * (1 + TP_PRICE_PCT), 8)
            sl_price = round(entry * (1 - SL_PRICE_PCT), 8)
        else:
            tp_price = round(entry * (1 - TP_PRICE_PCT), 8)
            sl_price = round(entry * (1 + SL_PRICE_PCT), 8)

        # ── Quality score (0–100) ──────────────────────────────────
        rsi_dist_score  = min(abs(rsi_cur - 50) / 50.0, 1.0)
        ema_spread_score = min(abs(ef_cur - es_cur) / entry / 0.005, 1.0)
        daily_dist_score = min(daily_ema_dist / 0.05, 1.0)

        score = round(
            (0.40 * rsi_dist_score +
             0.35 * ema_spread_score +
             0.25 * daily_dist_score) * 100,
            1,
        )

        trigger = (
            "EMAx" if (crossed_up if direction == "LONG" else crossed_down)
            else "RSIxt"
        )

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{TP_ROI_PCT}% ROI) "
            f"SL={sl_price:.6g} (-{SL_ROI_PCT}% ROI) | "
            f"score={score} trigger={trigger} "
            f"RSI={rsi_cur:.1f} dayDist={daily_ema_dist*100:.2f}%"
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
            timeframe_summary = (
                f"1D EMA{EMA_DAILY} + 15m EMA({EMA_FAST}/{EMA_SLOW})"
                f"+RSI{RSI_PERIOD} | {trigger}"
            ),
            generated_at      = datetime.now(timezone.utc),
            score             = score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None
