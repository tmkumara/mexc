"""
EMA 10/20 + CCI(20) strategy on 1H candles.

LONG:  EMA10 crosses above EMA20 AND CCI > 0 at candle close
SHORT: EMA10 crosses below EMA20 AND CCI < 0 at candle close
SL:    Recent lowest low (LONG) / highest high (SHORT) over SL_LOOKBACK bars
TP:    entry ± REWARD_RATIO × |entry − SL|
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from mexc_client import get_klines
from config import (
    STRATEGY_TF,
    STRATEGY_KLINE_COUNT,
    EMA_FAST,
    EMA_SLOW,
    CCI_LENGTH,
    SL_LOOKBACK,
    REWARD_RATIO,
    LEVERAGE,
    MIN_TP_ROI_PCT,
)

logger = logging.getLogger(__name__)


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


# ── indicators ────────────────────────────────────────────────────

def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.astype(float).ewm(span=length, adjust=False).mean()


def _cci(df: pd.DataFrame, length: int) -> pd.Series:
    hlc3 = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    ma = hlc3.rolling(length).mean()
    # mean absolute deviation — matches Pine Script ta.dev()
    mad = hlc3.rolling(length).apply(lambda x: (abs(x - x.mean())).mean(), raw=True)
    return (hlc3 - ma) / (0.015 * mad)


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"].astype(float)
    out["ema_fast"] = _ema(close, EMA_FAST)
    out["ema_slow"] = _ema(close, EMA_SLOW)
    out["cci"] = _cci(out, CCI_LENGTH)
    return out


def _recent_sl(df: pd.DataFrame, end_idx: int, direction: str) -> float:
    start = max(0, end_idx - SL_LOOKBACK + 1)
    window = df.iloc[start : end_idx + 1]
    if direction == "LONG":
        return float(window["low"].astype(float).min())
    else:
        return float(window["high"].astype(float).max())


# ── public API used by main.py ────────────────────────────────────

def analyze_coin(symbol: str) -> Signal | None:
    try:
        raw = get_klines(symbol, STRATEGY_TF, count=STRATEGY_KLINE_COUNT)
        if raw is None or raw.empty:
            return None

        df = _add_indicators(raw)
        completed = df.iloc[:-1].copy()  # exclude in-progress candle

        if len(completed) < max(EMA_SLOW, CCI_LENGTH) + 5:
            return None

        last_idx = len(completed) - 1
        last = completed.iloc[last_idx]
        prev = completed.iloc[last_idx - 1]

        ema_fast_now  = float(last["ema_fast"])
        ema_slow_now  = float(last["ema_slow"])
        ema_fast_prev = float(prev["ema_fast"])
        ema_slow_prev = float(prev["ema_slow"])
        cci_now       = float(last["cci"])

        ema_cross_up   = ema_fast_prev <= ema_slow_prev and ema_fast_now > ema_slow_now
        ema_cross_down = ema_fast_prev >= ema_slow_prev and ema_fast_now < ema_slow_now

        if ema_cross_up and cci_now > 0:
            direction = "LONG"
        elif ema_cross_down and cci_now < 0:
            direction = "SHORT"
        else:
            logger.info(
                f"[NO-SIGNAL] {symbol} cross_up={ema_cross_up} cross_down={ema_cross_down} "
                f"CCI={cci_now:.2f}"
            )
            return None

        entry = float(last["close"])
        sl_price = _recent_sl(completed, last_idx, direction)
        risk = abs(entry - sl_price)

        if risk <= 0:
            return None

        if direction == "LONG":
            if sl_price >= entry:
                return None
        else:
            if sl_price <= entry:
                return None

        # TP from RR, floored to MIN_TP_ROI_PCT (no upper cap)
        raw_tp_move_pct = risk * REWARD_RATIO / entry * 100.0
        tp_roi_final = max(MIN_TP_ROI_PCT, raw_tp_move_pct * LEVERAGE)
        tp_move_pct = tp_roi_final / LEVERAGE

        sign = 1.0 if direction == "LONG" else -1.0
        tp_price = entry * (1.0 + sign * tp_move_pct / 100.0)

        sl_move_pct = risk / entry * 100.0
        tp_roi_pct  = round(tp_roi_final, 1)
        sl_roi_pct  = round(sl_move_pct * LEVERAGE, 1)

        logger.info(
            f"[SIGNAL] {direction} {symbol} | TF={STRATEGY_TF} | "
            f"EMA{EMA_FAST}/EMA{EMA_SLOW} cross | CCI={cci_now:.2f} | "
            f"Entry={entry:.6g} TP={round(tp_price,8):.6g} SL={round(sl_price,8):.6g}"
        )

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            tp_price=round(tp_price, 8),
            sl_price=round(sl_price, 8),
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            timeframe_summary=(
                f"EMA{EMA_FAST}/EMA{EMA_SLOW} cross | CCI {cci_now:.1f} | "
                f"{STRATEGY_TF} | RR {REWARD_RATIO:g}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=75.0,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None


def detect_setup(symbol: str) -> dict | None:
    return None


def evaluate_pending_setup(setup: dict):
    return "WAIT", None
