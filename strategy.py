"""
EMA 10/20 + CCI(20) strategy on 1H candles with multi-pattern scoring.

Entry conditions (all must pass):
  1. EMA10 crosses EMA20 on the last closed candle
  2. |CCI(20)| >= CCI_MIN_ABS  (filters near-zero / weak momentum crosses)
  3. Freshness: candle closed within CANDLE_MINUTES of now
  4. SL distance <= MAX_SL_PCT of entry  (filters micro-caps with huge wicks)
  5. Pattern score >= PATTERN_MIN_SCORE

Pattern scoring:
  BOS  (Break of Structure)   +1  price closes beyond recent swing high/low
  Flag (Bull/Bear Flag)       +1  impulse pole + tight consolidation breakout
  IB   (Inside Bar Breakout)  +1  prev candle inside mother bar, signal breaks out
  Dbl  (Double Bottom/Top)    +2  two equal swing lows/highs within tolerance
  Tri  (Triple Bottom/Top)    +3  three equal swing lows/highs within tolerance

SL: recent lowest low (LONG) / highest high (SHORT) over SL_LOOKBACK bars
TP: entry +/- risk * REWARD_RATIO  (fixed 1:1.5, no floor override)
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

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
    CCI_MIN_ABS,
    MAX_SL_PCT,
    CANDLE_MINUTES,
    BOS_LOOKBACK,
    DOUBLE_LOOKBACK,
    DOUBLE_TOLERANCE_PCT,
    PATTERN_MIN_SCORE,
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


# ── pattern detection ─────────────────────────────────────────────

def _bos(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    """Break of Structure: signal candle closes beyond the prior swing structure."""
    start = max(0, end_idx - BOS_LOOKBACK)
    window = df.iloc[start : end_idx - 1]  # exclude last 2 candles
    if len(window) < 3:
        return False
    close = float(df.iloc[end_idx]["close"])
    if direction == "LONG":
        return close > float(window["high"].astype(float).max())
    else:
        return close < float(window["low"].astype(float).min())


def _flag(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    """
    Bull/Bear Flag: strong impulse pole followed by tight consolidation,
    signal candle breaks out of the consolidation.
    """
    if end_idx < 10:
        return False

    pole = df.iloc[max(0, end_idx - 10) : end_idx - 3]
    flag_bars = df.iloc[end_idx - 3 : end_idx]

    if len(pole) < 3 or len(flag_bars) < 2:
        return False

    pole_start_close = float(pole.iloc[0]["close"])
    flag_high = float(flag_bars["high"].astype(float).max())
    flag_low  = float(flag_bars["low"].astype(float).min())
    flag_range = flag_high - flag_low
    signal_close = float(df.iloc[end_idx]["close"])

    if direction == "LONG":
        pole_move = float(pole["close"].astype(float).max()) - pole_start_close
        if pole_move / max(pole_start_close, 1e-12) < 0.005:  # pole must move >= 0.5%
            return False
        if flag_range > pole_move * 0.5:                       # flag must be tight
            return False
        return signal_close > flag_high
    else:
        pole_move = pole_start_close - float(pole["close"].astype(float).min())
        if pole_move / max(pole_start_close, 1e-12) < 0.005:
            return False
        if flag_range > pole_move * 0.5:
            return False
        return signal_close < flag_low


def _inside_bar_breakout(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    """
    Inside Bar Breakout: the candle before the signal is fully inside
    the mother candle, and the signal candle breaks out.
    """
    if end_idx < 2:
        return False

    mother = df.iloc[end_idx - 2]
    inside = df.iloc[end_idx - 1]
    signal = df.iloc[end_idx]

    mother_high = float(mother["high"])
    mother_low  = float(mother["low"])
    inside_high = float(inside["high"])
    inside_low  = float(inside["low"])

    if not (inside_high < mother_high and inside_low > mother_low):
        return False

    signal_close = float(signal["close"])
    if direction == "LONG":
        return signal_close > inside_high
    else:
        return signal_close < inside_low


def _swing_lows(df: pd.DataFrame, end_idx: int, lookback: int) -> list[float]:
    window = df.iloc[max(0, end_idx - lookback) : end_idx]
    lows = window["low"].astype(float).values
    return [
        lows[i] for i in range(1, len(lows) - 1)
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]
    ]


def _swing_highs(df: pd.DataFrame, end_idx: int, lookback: int) -> list[float]:
    window = df.iloc[max(0, end_idx - lookback) : end_idx]
    highs = window["high"].astype(float).values
    return [
        highs[i] for i in range(1, len(highs) - 1)
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]
    ]


def _double_pattern(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    """Double Bottom (LONG) / Double Top (SHORT): two equal swing extremes."""
    tol = DOUBLE_TOLERANCE_PCT / 100.0
    if direction == "LONG":
        pts = _swing_lows(df, end_idx, DOUBLE_LOOKBACK)
        if len(pts) < 2:
            return False
        return abs(pts[-2] - pts[-1]) / max(pts[-2], 1e-12) <= tol
    else:
        pts = _swing_highs(df, end_idx, DOUBLE_LOOKBACK)
        if len(pts) < 2:
            return False
        return abs(pts[-2] - pts[-1]) / max(pts[-2], 1e-12) <= tol


def _triple_pattern(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    """Triple Bottom (LONG) / Triple Top (SHORT): three equal swing extremes."""
    tol = DOUBLE_TOLERANCE_PCT / 100.0
    if direction == "LONG":
        pts = _swing_lows(df, end_idx, DOUBLE_LOOKBACK)
        if len(pts) < 3:
            return False
        return (abs(pts[-3] - pts[-2]) / max(pts[-3], 1e-12) <= tol and
                abs(pts[-2] - pts[-1]) / max(pts[-2], 1e-12) <= tol)
    else:
        pts = _swing_highs(df, end_idx, DOUBLE_LOOKBACK)
        if len(pts) < 3:
            return False
        return (abs(pts[-3] - pts[-2]) / max(pts[-3], 1e-12) <= tol and
                abs(pts[-2] - pts[-1]) / max(pts[-2], 1e-12) <= tol)


def _pattern_score(df: pd.DataFrame, end_idx: int, direction: str) -> tuple[int, list[str]]:
    """Return total pattern score and list of matched pattern names."""
    score = 0
    matched = []

    if _bos(df, end_idx, direction):
        score += 1
        matched.append("BOS")

    if _flag(df, end_idx, direction):
        score += 1
        matched.append("Flag")

    if _inside_bar_breakout(df, end_idx, direction):
        score += 1
        matched.append("IB")

    # Triple scores higher than Double — check triple first to avoid double-counting
    if _triple_pattern(df, end_idx, direction):
        score += 3
        matched.append("Triple")
    elif _double_pattern(df, end_idx, direction):
        score += 2
        matched.append("Double")

    return score, matched


# ── public API used by main.py ────────────────────────────────────

def analyze_coin(symbol: str) -> Signal | None:
    try:
        raw = get_klines(symbol, STRATEGY_TF, count=STRATEGY_KLINE_COUNT)
        if raw is None or raw.empty:
            return None

        df = _add_indicators(raw)
        completed = df.iloc[:-1].copy()  # exclude in-progress candle

        if len(completed) < max(EMA_SLOW, CCI_LENGTH, DOUBLE_LOOKBACK) + 5:
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

        # Filter 1: CCI must have meaningful momentum strength
        if abs(cci_now) < CCI_MIN_ABS:
            logger.info(f"[NO-SIGNAL] {symbol} {direction} CCI={cci_now:.2f} < ±{CCI_MIN_ABS}")
            return None

        # Filter 2: freshness — candle must have closed within the last CANDLE_MINUTES
        candle_open  = completed.iloc[last_idx].name.to_pydatetime().replace(tzinfo=timezone.utc)
        candle_close = candle_open + timedelta(minutes=CANDLE_MINUTES)
        age = datetime.now(timezone.utc) - candle_close
        if age > timedelta(minutes=CANDLE_MINUTES):
            logger.info(
                f"[NO-SIGNAL] {symbol} {direction} stale cross "
                f"(closed {int(age.total_seconds() / 60)}m ago)"
            )
            return None

        # SL and basic risk checks
        entry    = float(last["close"])
        sl_price = _recent_sl(completed, last_idx, direction)
        risk     = abs(entry - sl_price)

        if risk <= 0:
            return None

        if direction == "LONG" and sl_price >= entry:
            return None
        if direction == "SHORT" and sl_price <= entry:
            return None

        # Filter 3: SL distance must not exceed MAX_SL_PCT
        sl_move_pct = risk / entry * 100.0
        if sl_move_pct > MAX_SL_PCT:
            logger.info(
                f"[NO-SIGNAL] {symbol} {direction} SL too wide "
                f"({sl_move_pct:.1f}% > {MAX_SL_PCT}%)"
            )
            return None

        # Filter 4: pattern score
        p_score, patterns = _pattern_score(completed, last_idx, direction)
        if p_score < PATTERN_MIN_SCORE:
            logger.info(
                f"[NO-SIGNAL] {symbol} {direction} pattern score={p_score} "
                f"< {PATTERN_MIN_SCORE} | {patterns}"
            )
            return None

        # Prices — fixed 1:1.5 RR, no floor override
        sign       = 1.0 if direction == "LONG" else -1.0
        tp_price   = entry + sign * risk * REWARD_RATIO
        tp_move_pct = risk * REWARD_RATIO / entry * 100.0
        tp_roi_pct  = round(tp_move_pct * LEVERAGE, 1)
        sl_roi_pct  = round(sl_move_pct * LEVERAGE, 1)

        pattern_str = "+".join(patterns)
        logger.info(
            f"[SIGNAL] {direction} {symbol} | TF={STRATEGY_TF} | "
            f"EMA{EMA_FAST}/EMA{EMA_SLOW} | CCI={cci_now:.1f} | "
            f"Patterns={pattern_str}(score={p_score}) | "
            f"Entry={entry:.6g} TP={round(tp_price,8):.6g} SL={round(sl_price,8):.6g} "
            f"RR=1:{REWARD_RATIO}"
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
                f"EMA{EMA_FAST}/EMA{EMA_SLOW} | CCI {cci_now:.0f} | "
                f"{pattern_str} | {STRATEGY_TF} | RR 1:{REWARD_RATIO}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=float(min(p_score * 20 + 40, 100)),
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None


def detect_setup(symbol: str) -> dict | None:
    return None


def evaluate_pending_setup(setup: dict):
    return "WAIT", None
