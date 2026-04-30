"""
EMA Crossover + MACD + RSI scalping strategy.

Entry conditions — all five must be true simultaneously:

  LONG:  EMA(9) crosses above EMA(21) on last completed candle
         close > EMA(200)              [uptrend filter]
         RSI(14) > 50                  [bullish momentum]
         MACD histogram > 0            [MACD above signal line]
         volume >= VOLUME_MIN_MULT × 20-bar MA

  SHORT: EMA(9) crosses below EMA(21) on last completed candle
         close < EMA(200)              [downtrend filter]
         RSI(14) < 50                  [bearish momentum]
         MACD histogram < 0            [MACD below signal line]
         volume >= VOLUME_MIN_MULT × 20-bar MA

Risk management (fixed ROI, leverage-adjusted):
  TP = entry × (1 ± TP_ROI_PCT / 100 / LEVERAGE)
  SL = entry × (1 ∓ SL_ROI_PCT / 100 / LEVERAGE)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta  # noqa: F401

from mexc_client import get_klines
from config import (
    LEVERAGE, TIMEFRAME,
    EMA_FAST, EMA_SLOW, EMA_TREND,
    RSI_PERIOD,
    MACD_FAST, MACD_SLOW, MACD_SIGNAL_PERIOD,
    VOLUME_MA_BARS, VOLUME_MIN_MULT,
    TP_ROI_PCT, SL_ROI_PCT,
)

logger = logging.getLogger(__name__)

KLINE_COUNT = EMA_TREND + 100  # 300 candles — covers EMA(200) warm-up

BTC_EMA_PROXIMITY_PCT: float = 0.005  # 0.5% band around EMA(200) — treated as ambiguous


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
    score:             float = 0.0


def analyze_coin(symbol: str) -> Signal | None:
    """Run EMA crossover + MACD + RSI analysis. Returns Signal or None."""
    try:
        df = get_klines(symbol, TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < EMA_TREND + 50:
            logger.debug(f"{symbol}: not enough candles ({len(df)})")
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

        # Use iloc[-2]: last completed candle; iloc[-1] is still forming
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

        if   crossed_up   and entry > et and rsi_cur > 50 and hist > 0:
            direction = "LONG"
        elif crossed_down and entry < et and rsi_cur < 50 and hist < 0:
            direction = "SHORT"
        else:
            return None

        if vol_ma > 0 and vol_cur < VOLUME_MIN_MULT * vol_ma:
            logger.debug(f"{symbol}: volume filter fail ({vol_cur:.0f} < {VOLUME_MIN_MULT}×{vol_ma:.0f})")
            return None

        # Fixed ROI TP/SL (leverage-adjusted price levels)
        roi_tp_frac = TP_ROI_PCT / 100 / LEVERAGE
        roi_sl_frac = SL_ROI_PCT / 100 / LEVERAGE
        if direction == "LONG":
            tp_price = round(entry * (1 + roi_tp_frac), 8)
            sl_price = round(entry * (1 - roi_sl_frac), 8)
        else:
            tp_price = round(entry * (1 - roi_tp_frac), 8)
            sl_price = round(entry * (1 + roi_sl_frac), 8)

        # ── Multi-factor quality score (0–100) ────────────────────
        # Factor 1 — MACD histogram strength (35%)
        hist_score = min(abs(hist) / (entry * 0.001), 1.0)

        # Factor 2 — RSI distance from 50 (25%)
        rsi_score = min(abs(rsi_cur - 50) / 30.0, 1.0)

        # Factor 3 — EMA(fast/slow) spread as % of price (20%)
        spread_score = min(abs(ef_cur - es_cur) / entry * 100 / 0.5, 1.0)

        # Factor 4 — Volume ratio vs MA (20%)
        vol_ratio  = (vol_cur / vol_ma) if vol_ma > 0 else 1.0
        vol_score  = min(vol_ratio / 3.0, 1.0)

        score = round(
            (0.35 * hist_score + 0.25 * rsi_score +
             0.20 * spread_score + 0.20 * vol_score) * 100,
            1,
        )

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry} | "
            f"SL={sl_price} (-{SL_ROI_PCT}% ROI) TP={tp_price} (+{TP_ROI_PCT}% ROI) | "
            f"score={score} RSI={rsi_cur:.1f} MACDh={hist:.6f} vol={vol_ratio:.1f}x"
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
                f"EMA({EMA_FAST}/{EMA_SLOW}/{EMA_TREND}) + "
                f"MACD({MACD_FAST},{MACD_SLOW},{MACD_SIGNAL_PERIOD}) + "
                f"RSI({RSI_PERIOD}) | {TIMEFRAME}"
            ),
            generated_at      = datetime.now(timezone.utc),
            score             = score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None


def get_btc_bias() -> str | None:
    """
    Return "LONG" if BTC is above its EMA(200), "SHORT" if below.
    Returns None when data is unavailable or BTC is within BTC_EMA_PROXIMITY_PCT
    of EMA(200) (transitional zone). None = fail-open: caller must not block signals.
    """
    try:
        df = get_klines("BTC_USDT", TIMEFRAME, count=KLINE_COUNT)
        if df.empty or len(df) < EMA_TREND + 50:
            logger.warning("get_btc_bias: insufficient BTC klines")
            return None

        btc_ema200 = ta.ema(df["close"], length=EMA_TREND)
        if pd.isna(btc_ema200.iloc[-2]):
            logger.warning("get_btc_bias: BTC EMA(200) is NaN")
            return None

        btc_close = float(df["close"].iloc[-2])
        ema_val   = float(btc_ema200.iloc[-2])

        proximity = abs(btc_close - ema_val) / ema_val
        if proximity < BTC_EMA_PROXIMITY_PCT:
            logger.debug(f"get_btc_bias: BTC within {BTC_EMA_PROXIMITY_PCT*100:.1f}% of EMA(200) — ambiguous")
            return None

        bias = "LONG" if btc_close > ema_val else "SHORT"
        logger.debug(f"get_btc_bias: {bias} (BTC={btc_close:.2f} EMA200={ema_val:.2f} gap={proximity*100:.2f}%)")
        return bias

    except Exception as e:
        logger.error(f"get_btc_bias error: {e}", exc_info=True)
        return None
