"""
Fresh Trend Meter + Stoch MTM strategy.

Rules implemented:
    LONG
      1. Trend Meter must have all 3 lines green on STRATEGY_TF.
      2. Stoch MTM white line must first be below -40.
      3. Signal triggers only when the white line crosses back above -40.

    SHORT
      1. Trend Meter must have all 3 lines red on STRATEGY_TF.
      2. Stoch MTM white line must first be above +40.
      3. Signal triggers only when the white line crosses back below +40.

Important note:
    TradingView Trend Meter and Stoch MTM scripts are not available inside the bot.
    This file implements a transparent approximation:
      - Trend Meter line 1: EMA13 vs EMA21
      - Trend Meter line 2: EMA34 vs EMA55
      - Trend Meter line 3: Close vs EMA200
      - Stoch MTM white line: Stochastic Momentum Index style oscillator.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from mexc_client import get_klines
from config import (
    STRATEGY_TF,
    STRATEGY_KLINE_COUNT,
    TREND_EMA_FAST_1,
    TREND_EMA_SLOW_1,
    TREND_EMA_FAST_2,
    TREND_EMA_SLOW_2,
    TREND_EMA_FILTER,
    STOCH_MTM_LENGTH,
    STOCH_MTM_SMOOTH_1,
    STOCH_MTM_SMOOTH_2,
    STOCH_MTM_SIGNAL,
    STOCH_MTM_UPPER,
    STOCH_MTM_LOWER,
    MAX_CROSS_LOOKBACK_CANDLES,
    MIN_ABS_MTM_AFTER_CROSS,
    ATR_PERIOD,
    ATR_SL_MULTIPLIER,
    REWARD_RATIO,
    MIN_SL_PCT,
    MAX_SL_PCT,
    LEVERAGE,
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


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(alpha=1 / length, adjust=False).mean()


def _stoch_mtm_white_line(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """
    Stoch MTM approximation using Stochastic Momentum Index style logic.

    Returns:
        white_line, signal_line
    """
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    highest_high = high.rolling(STOCH_MTM_LENGTH).max()
    lowest_low = low.rolling(STOCH_MTM_LENGTH).min()

    midpoint = (highest_high + lowest_low) / 2.0
    distance = close - midpoint
    half_range = (highest_high - lowest_low) / 2.0

    distance_smoothed = (
        distance
        .ewm(span=STOCH_MTM_SMOOTH_1, adjust=False).mean()
        .ewm(span=STOCH_MTM_SMOOTH_2, adjust=False).mean()
    )
    range_smoothed = (
        half_range
        .ewm(span=STOCH_MTM_SMOOTH_1, adjust=False).mean()
        .ewm(span=STOCH_MTM_SMOOTH_2, adjust=False).mean()
    )

    white = 100.0 * distance_smoothed / range_smoothed.replace(0, pd.NA)
    white = white.clip(lower=-100.0, upper=100.0)
    signal = white.ewm(span=STOCH_MTM_SIGNAL, adjust=False).mean()

    return white, signal


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"].astype(float)

    out["ema_fast_1"] = _ema(close, TREND_EMA_FAST_1)
    out["ema_slow_1"] = _ema(close, TREND_EMA_SLOW_1)
    out["ema_fast_2"] = _ema(close, TREND_EMA_FAST_2)
    out["ema_slow_2"] = _ema(close, TREND_EMA_SLOW_2)
    out["ema_filter"] = _ema(close, TREND_EMA_FILTER)
    out["atr"] = _atr(out, ATR_PERIOD)

    white, signal = _stoch_mtm_white_line(out)
    out["stoch_mtm"] = white
    out["stoch_mtm_signal"] = signal

    return out


# ── strategy conditions ───────────────────────────────────────────

def _trend_meter_state(row: pd.Series) -> tuple[bool, bool, dict]:
    close = float(row["close"])

    line_1_green = float(row["ema_fast_1"]) > float(row["ema_slow_1"])
    line_2_green = float(row["ema_fast_2"]) > float(row["ema_slow_2"])
    line_3_green = close > float(row["ema_filter"])

    line_1_red = float(row["ema_fast_1"]) < float(row["ema_slow_1"])
    line_2_red = float(row["ema_fast_2"]) < float(row["ema_slow_2"])
    line_3_red = close < float(row["ema_filter"])

    details = {
        "line_1": "GREEN" if line_1_green else "RED",
        "line_2": "GREEN" if line_2_green else "RED",
        "line_3": "GREEN" if line_3_green else "RED",
    }

    all_green = line_1_green and line_2_green and line_3_green
    all_red = line_1_red and line_2_red and line_3_red

    return all_green, all_red, details


def _find_recent_cross(df: pd.DataFrame, direction: str) -> tuple[int, pd.Series, pd.Series] | None:
    completed = df.iloc[:-1].copy()

    if len(completed) < max(TREND_EMA_FILTER, STOCH_MTM_LENGTH) + 5:
        return None

    lookback = max(1, MAX_CROSS_LOOKBACK_CANDLES)
    start = len(completed) - lookback

    for pos in range(start, len(completed)):
        if pos <= 0:
            continue

        prev = completed.iloc[pos - 1]
        curr = completed.iloc[pos]

        prev_mtm = float(prev["stoch_mtm"])
        curr_mtm = float(curr["stoch_mtm"])

        if direction == "LONG":
            crossed = prev_mtm <= STOCH_MTM_LOWER and curr_mtm > STOCH_MTM_LOWER
            strong_enough = curr_mtm >= MIN_ABS_MTM_AFTER_CROSS * -1
        else:
            crossed = prev_mtm >= STOCH_MTM_UPPER and curr_mtm < STOCH_MTM_UPPER
            strong_enough = curr_mtm <= MIN_ABS_MTM_AFTER_CROSS

        if crossed and strong_enough:
            return pos, prev, curr

    return None


def _build_prices(direction: str, entry: float, atr: float) -> tuple[float, float, float, float, float] | None:
    if entry <= 0 or atr <= 0:
        return None

    raw_risk = atr * ATR_SL_MULTIPLIER
    raw_sl_pct = raw_risk / entry * 100.0

    sl_pct = min(max(raw_sl_pct, MIN_SL_PCT), MAX_SL_PCT)
    risk = entry * sl_pct / 100.0
    reward = risk * REWARD_RATIO

    if direction == "LONG":
        sl_price = entry - risk
        tp_price = entry + reward
    else:
        sl_price = entry + risk
        tp_price = entry - reward

    tp_move_pct = reward / entry * 100.0
    sl_move_pct = risk / entry * 100.0

    return (
        round(tp_price, 8),
        round(sl_price, 8),
        round(tp_move_pct * LEVERAGE, 1),
        round(sl_move_pct * LEVERAGE, 1),
        round(REWARD_RATIO, 2),
    )


def _score_signal(direction: str, row: pd.Series, trend_details: dict) -> float:
    mtm = float(row["stoch_mtm"])

    score = 70.0

    if direction == "LONG":
        if mtm > -25:
            score += 8
        if float(row["close"]) > float(row["ema_fast_1"]):
            score += 7
    else:
        if mtm < 25:
            score += 8
        if float(row["close"]) < float(row["ema_fast_1"]):
            score += 7

    if all(v == "GREEN" for v in trend_details.values()) or all(v == "RED" for v in trend_details.values()):
        score += 10

    return round(min(score, 100.0), 1)


# ── public API used by main.py ────────────────────────────────────

def analyze_coin(symbol: str) -> Signal | None:
    try:
        raw = get_klines(symbol, STRATEGY_TF, count=STRATEGY_KLINE_COUNT)

        if raw is None or raw.empty:
            return None

        df = _add_indicators(raw)
        completed = df.iloc[:-1].copy()

        if len(completed) < max(TREND_EMA_FILTER, STOCH_MTM_LENGTH) + 5:
            return None

        last = completed.iloc[-1]
        all_green, all_red, trend_details = _trend_meter_state(last)

        direction = None

        if all_green:
            direction = "LONG"
        elif all_red:
            direction = "SHORT"
        else:
            logger.info(
                f"[NO-SIGNAL] {symbol} trend meter not aligned: "
                f"{trend_details['line_1']}/{trend_details['line_2']}/{trend_details['line_3']}"
            )
            return None

        cross = _find_recent_cross(df, direction)

        if not cross:
            logger.info(
                f"[NO-SIGNAL] {symbol} {direction} trend ok but no Stoch MTM cross | "
                f"MTM={float(last['stoch_mtm']):.2f}"
            )
            return None

        _, prev, curr = cross
        entry = float(curr["close"])
        atr = float(curr["atr"])

        prices = _build_prices(direction, entry, atr)

        if not prices:
            return None

        tp_price, sl_price, tp_roi_pct, sl_roi_pct, rr = prices
        score = _score_signal(direction, curr, trend_details)

        logger.info(
            f"[SIGNAL] {direction} {symbol} | TF={STRATEGY_TF} | "
            f"Trend={trend_details['line_1']}/{trend_details['line_2']}/{trend_details['line_3']} | "
            f"StochMTM {float(prev['stoch_mtm']):.2f}->{float(curr['stoch_mtm']):.2f} | "
            f"Entry={entry:.6g} TP={tp_price:.6g} SL={sl_price:.6g} RR={rr:.2f}"
        )

        if direction == "LONG":
            stoch_text = f"Stoch MTM crossed above {STOCH_MTM_LOWER:g}"
        else:
            stoch_text = f"Stoch MTM crossed below +{STOCH_MTM_UPPER:g}"

        return Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            timeframe_summary=(
                f"Trend Meter 3/3 | {STRATEGY_TF} | {stoch_text} | RR {rr:g}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None


# Compatibility with the old stateful setup flow. New strategy fires direct signals.
def detect_setup(symbol: str) -> dict | None:
    return None


def evaluate_pending_setup(setup: dict):
    return "WAIT", None
