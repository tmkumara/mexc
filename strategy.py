"""
Strategy — Squeeze Momentum + WaveTrend + Supertrend.

This replaces the previous VWAP / SMC / pending setup logic.

TradingView source logic converted from:
    - WaveTrend oscillator
    - Squeeze Momentum
    - Supertrend
    - ATR TP/SL model

Bot flow:
    main.py -> analyze_coin(symbol) -> Signal | None

No pending setup / OB retest logic is used.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from mexc_client import get_klines
from config import (
    ENTRY_TF,
    ENTRY_KLINE_COUNT,
    WT_CHANNEL_LENGTH,
    WT_AVERAGE_LENGTH,
    WT_SIGNAL_LENGTH,
    WT_OVERBOUGHT_LEVEL_2,
    WT_OVERSOLD_LEVEL_2,
    SUPERTREND_ATR_PERIOD,
    SUPERTREND_FACTOR,
    SQUEEZE_BB_LENGTH,
    SQUEEZE_BB_MULT,
    SQUEEZE_KC_LENGTH,
    SQUEEZE_KC_MULT,
    SQUEEZE_USE_TRUE_RANGE,
    SQUEEZE_SIGNAL_LENGTH,
    SQUEEZE_LOWER_THRESHOLD,
    SQUEEZE_UPPER_THRESHOLD,
    USE_RECENT_SQUEEZE_RELEASE,
    RECENT_SQUEEZE_RELEASE_BARS,
    USE_WAVETREND_CROSS_CONFIRMATION,
    RECENT_WT_CROSS_BARS,
    REQUIRE_SUPERTREND_ALIGNMENT,
    REQUIRE_SQUEEZE_RELEASE,
    REQUIRE_WAVETREND_ALIGNMENT,
    MIN_SIGNAL_SCORE,
    TARGET_ATR_MULTIPLIER,
    STOP_LOSS_ATR_MULTIPLIER,
    MIN_TP_ROI_PCT,
    MAX_TP_ROI_PCT,
    MIN_SL_ROI_PCT,
    MAX_SL_ROI_PCT,
    MAX_SIGNAL_CANDLE_BODY_PCT,
    MAX_RECENT_MOVE_PCT,
    RECENT_MOVE_LOOKBACK,
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


# ── basic helpers ─────────────────────────────────────────────────

def _ensure_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    required = {"open", "high", "low", "close"}

    if not required.issubset(set(df.columns)):
        return pd.DataFrame()

    out = df.copy()

    for col in ["open", "high", "low", "close", "volume"]:
        if col in out.columns:
            out[col] = out[col].astype(float)

    if "volume" not in out.columns:
        out["volume"] = 0.0

    return out


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).rolling(period, min_periods=period).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])
    if close <= 0:
        return 0.0
    return abs(float(row["close"]) - float(row["open"])) / close * 100.0


def _recent_move_pct(df: pd.DataFrame) -> float:
    recent = df.tail(RECENT_MOVE_LOOKBACK)

    if recent.empty:
        return 0.0

    high = float(recent["high"].max())
    low = float(recent["low"].min())
    mid = (high + low) / 2.0

    if mid <= 0:
        return 999.0

    return (high - low) / mid * 100.0


def _last_recent_true(series: pd.Series, bars: int) -> bool:
    if series.empty:
        return False

    recent = series.tail(max(1, bars))
    return bool(recent.fillna(False).any())


# ── indicator calculations ────────────────────────────────────────

def _wave_trend(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    ap = (out["high"] + out["low"] + out["close"]) / 3.0
    esa = _ema(ap, WT_CHANNEL_LENGTH)
    d = _ema((ap - esa).abs(), WT_CHANNEL_LENGTH)

    denominator = 0.015 * d.replace(0, np.nan)
    ci = (ap - esa) / denominator

    wt1 = _ema(ci, WT_AVERAGE_LENGTH)
    wt2 = _sma(wt1, WT_SIGNAL_LENGTH)

    out["wt1"] = wt1
    out["wt2"] = wt2
    out["wt_bull_cross"] = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
    out["wt_bear_cross"] = (wt1 < wt2) & (wt1.shift(1) >= wt2.shift(1))
    out["wt_bullish"] = wt1 > wt2
    out["wt_bearish"] = wt1 < wt2

    return out


def _rolling_linreg_last(series: pd.Series, period: int) -> pd.Series:
    x = np.arange(period, dtype=float)

    def calc(values: np.ndarray) -> float:
        if len(values) != period or np.isnan(values).any():
            return np.nan

        slope, intercept = np.polyfit(x, values, 1)
        return float(slope * (period - 1) + intercept)

    return series.rolling(period, min_periods=period).apply(calc, raw=True)


def _squeeze_momentum(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    source = out["close"].astype(float)

    basis = _sma(source, SQUEEZE_BB_LENGTH)
    dev = source.rolling(SQUEEZE_BB_LENGTH, min_periods=SQUEEZE_BB_LENGTH).std() * SQUEEZE_BB_MULT

    bb_upper = basis + dev
    bb_lower = basis - dev

    ma = _sma(source, SQUEEZE_KC_LENGTH)

    if SQUEEZE_USE_TRUE_RANGE:
        tr_range = _true_range(out)
    else:
        tr_range = out["high"] - out["low"]

    range_ma = _sma(tr_range, SQUEEZE_KC_LENGTH)

    kc_upper = ma + range_ma * SQUEEZE_KC_MULT
    kc_lower = ma - range_ma * SQUEEZE_KC_MULT

    sqz_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
    sqz_off = (bb_lower < kc_lower) & (bb_upper > kc_upper)

    highest_high = out["high"].rolling(SQUEEZE_KC_LENGTH, min_periods=SQUEEZE_KC_LENGTH).max()
    lowest_low = out["low"].rolling(SQUEEZE_KC_LENGTH, min_periods=SQUEEZE_KC_LENGTH).min()
    sma_source = _sma(source, SQUEEZE_KC_LENGTH)

    base = ((highest_high + lowest_low) / 2.0 + sma_source) / 2.0
    raw_momentum = source - base

    val = _rolling_linreg_last(raw_momentum, SQUEEZE_KC_LENGTH)
    signal = _sma(val, SQUEEZE_SIGNAL_LENGTH)

    out["sqz_on"] = sqz_on
    out["sqz_off"] = sqz_off
    out["sqz_val"] = val
    out["sqz_signal"] = signal

    out["squeeze_release_up"] = (
        sqz_off
        & (val > val.shift(1))
        & (val >= SQUEEZE_UPPER_THRESHOLD)
    )

    out["squeeze_release_down"] = (
        sqz_off
        & (val < val.shift(1))
        & (val <= SQUEEZE_LOWER_THRESHOLD)
    )

    out["squeeze_bullish"] = val > signal
    out["squeeze_bearish"] = val < signal

    return out


def _supertrend(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    hl2 = (out["high"] + out["low"]) / 2.0
    atr = _atr(out, SUPERTREND_ATR_PERIOD)

    basic_upper = hl2 + SUPERTREND_FACTOR * atr
    basic_lower = hl2 - SUPERTREND_FACTOR * atr

    final_upper = pd.Series(index=out.index, dtype=float)
    final_lower = pd.Series(index=out.index, dtype=float)
    trend = pd.Series(index=out.index, dtype=int)
    supertrend = pd.Series(index=out.index, dtype=float)

    for i in range(len(out)):
        if i == 0:
            final_upper.iloc[i] = basic_upper.iloc[i]
            final_lower.iloc[i] = basic_lower.iloc[i]
            trend.iloc[i] = 1
            supertrend.iloc[i] = final_lower.iloc[i]
            continue

        prev_close = float(out["close"].iloc[i - 1])

        final_upper.iloc[i] = (
            basic_upper.iloc[i]
            if basic_upper.iloc[i] < final_upper.iloc[i - 1] or prev_close > final_upper.iloc[i - 1]
            else final_upper.iloc[i - 1]
        )

        final_lower.iloc[i] = (
            basic_lower.iloc[i]
            if basic_lower.iloc[i] > final_lower.iloc[i - 1] or prev_close < final_lower.iloc[i - 1]
            else final_lower.iloc[i - 1]
        )

        close = float(out["close"].iloc[i])

        if trend.iloc[i - 1] == -1 and close > final_upper.iloc[i]:
            trend.iloc[i] = 1
        elif trend.iloc[i - 1] == 1 and close < final_lower.iloc[i]:
            trend.iloc[i] = -1
        else:
            trend.iloc[i] = trend.iloc[i - 1]

        supertrend.iloc[i] = final_lower.iloc[i] if trend.iloc[i] == 1 else final_upper.iloc[i]

    out["atr"] = atr
    out["supertrend"] = supertrend
    out["supertrend_trend"] = trend
    out["up_trend"] = trend == 1
    out["down_trend"] = trend == -1
    out["trend_changed_up"] = (trend == 1) & (trend.shift(1) == -1)
    out["trend_changed_down"] = (trend == -1) & (trend.shift(1) == 1)

    return out


def _prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = _supertrend(df)
    out = _wave_trend(out)
    out = _squeeze_momentum(out)
    return out


# ── signal model ──────────────────────────────────────────────────

def _calculate_prices(direction: str, entry: float, atr_value: float) -> tuple[float, float, float, float] | None:
    if entry <= 0 or atr_value <= 0:
        return None

    if direction == "LONG":
        tp_price = entry + atr_value * TARGET_ATR_MULTIPLIER
        sl_price = entry - atr_value * STOP_LOSS_ATR_MULTIPLIER
        tp_move_pct = (tp_price - entry) / entry * 100.0
        sl_move_pct = (entry - sl_price) / entry * 100.0
    else:
        tp_price = entry - atr_value * TARGET_ATR_MULTIPLIER
        sl_price = entry + atr_value * STOP_LOSS_ATR_MULTIPLIER
        tp_move_pct = (entry - tp_price) / entry * 100.0
        sl_move_pct = (sl_price - entry) / entry * 100.0

    tp_roi = tp_move_pct * LEVERAGE
    sl_roi = sl_move_pct * LEVERAGE

    if tp_roi < MIN_TP_ROI_PCT or tp_roi > MAX_TP_ROI_PCT:
        return None

    if sl_roi < MIN_SL_ROI_PCT or sl_roi > MAX_SL_ROI_PCT:
        return None

    return (
        round(tp_price, 8),
        round(sl_price, 8),
        round(tp_roi, 1),
        round(sl_roi, 1),
    )


def _score_long(row: pd.Series, recent: pd.DataFrame) -> float:
    score = 45.0

    if bool(row["up_trend"]):
        score += 20.0

    if bool(row["squeeze_release_up"]):
        score += 18.0
    elif USE_RECENT_SQUEEZE_RELEASE and _last_recent_true(recent["squeeze_release_up"], RECENT_SQUEEZE_RELEASE_BARS):
        score += 12.0

    if bool(row["wt_bullish"]):
        score += 10.0

    if bool(row["wt_bull_cross"]):
        score += 10.0
    elif USE_WAVETREND_CROSS_CONFIRMATION and _last_recent_true(recent["wt_bull_cross"], RECENT_WT_CROSS_BARS):
        score += 6.0

    wt1 = float(row.get("wt1", 0.0))
    if wt1 <= WT_OVERSOLD_LEVEL_2:
        score += 5.0
    elif wt1 < 0:
        score += 3.0

    if bool(row["squeeze_bullish"]):
        score += 5.0

    return round(min(score, 100.0), 1)


def _score_short(row: pd.Series, recent: pd.DataFrame) -> float:
    score = 45.0

    if bool(row["down_trend"]):
        score += 20.0

    if bool(row["squeeze_release_down"]):
        score += 18.0
    elif USE_RECENT_SQUEEZE_RELEASE and _last_recent_true(recent["squeeze_release_down"], RECENT_SQUEEZE_RELEASE_BARS):
        score += 12.0

    if bool(row["wt_bearish"]):
        score += 10.0

    if bool(row["wt_bear_cross"]):
        score += 10.0
    elif USE_WAVETREND_CROSS_CONFIRMATION and _last_recent_true(recent["wt_bear_cross"], RECENT_WT_CROSS_BARS):
        score += 6.0

    wt1 = float(row.get("wt1", 0.0))
    if wt1 >= WT_OVERBOUGHT_LEVEL_2:
        score += 5.0
    elif wt1 > 0:
        score += 3.0

    if bool(row["squeeze_bearish"]):
        score += 5.0

    return round(min(score, 100.0), 1)


def _long_conditions(row: pd.Series, recent: pd.DataFrame) -> tuple[bool, str]:
    if REQUIRE_SUPERTREND_ALIGNMENT and not bool(row["up_trend"]):
        return False, "supertrend_not_bullish"

    if REQUIRE_SQUEEZE_RELEASE:
        current_release = bool(row["squeeze_release_up"])
        recent_release = USE_RECENT_SQUEEZE_RELEASE and _last_recent_true(
            recent["squeeze_release_up"],
            RECENT_SQUEEZE_RELEASE_BARS,
        )

        if not current_release and not recent_release:
            return False, "no_bullish_squeeze_release"

    if REQUIRE_WAVETREND_ALIGNMENT and not bool(row["wt_bullish"]):
        return False, "wavetrend_not_bullish"

    if USE_WAVETREND_CROSS_CONFIRMATION:
        current_cross = bool(row["wt_bull_cross"])
        recent_cross = _last_recent_true(recent["wt_bull_cross"], RECENT_WT_CROSS_BARS)

        if not current_cross and not recent_cross:
            return False, "no_recent_wt_bull_cross"

    if _body_pct(row) > MAX_SIGNAL_CANDLE_BODY_PCT:
        return False, "signal_candle_too_large"

    return True, "long_confirmed"


def _short_conditions(row: pd.Series, recent: pd.DataFrame) -> tuple[bool, str]:
    if REQUIRE_SUPERTREND_ALIGNMENT and not bool(row["down_trend"]):
        return False, "supertrend_not_bearish"

    if REQUIRE_SQUEEZE_RELEASE:
        current_release = bool(row["squeeze_release_down"])
        recent_release = USE_RECENT_SQUEEZE_RELEASE and _last_recent_true(
            recent["squeeze_release_down"],
            RECENT_SQUEEZE_RELEASE_BARS,
        )

        if not current_release and not recent_release:
            return False, "no_bearish_squeeze_release"

    if REQUIRE_WAVETREND_ALIGNMENT and not bool(row["wt_bearish"]):
        return False, "wavetrend_not_bearish"

    if USE_WAVETREND_CROSS_CONFIRMATION:
        current_cross = bool(row["wt_bear_cross"])
        recent_cross = _last_recent_true(recent["wt_bear_cross"], RECENT_WT_CROSS_BARS)

        if not current_cross and not recent_cross:
            return False, "no_recent_wt_bear_cross"

    if _body_pct(row) > MAX_SIGNAL_CANDLE_BODY_PCT:
        return False, "signal_candle_too_large"

    return True, "short_confirmed"


def analyze_coin(symbol: str) -> Signal | None:
    try:
        raw_df = _ensure_df(get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT))

        if raw_df.empty or len(raw_df) < 80:
            return None

        # Use completed candles only.
        completed = raw_df.iloc[:-1].copy()

        if len(completed) < 80:
            return None

        df = _prepare_indicators(completed)

        if df.empty or len(df) < 50:
            return None

        row = df.iloc[-1]
        recent = df.tail(max(RECENT_SQUEEZE_RELEASE_BARS, RECENT_WT_CROSS_BARS, 5))

        recent_move = _recent_move_pct(df)

        if recent_move > MAX_RECENT_MOVE_PCT:
            logger.info(
                f"[SIGNAL-REASON] {symbol} rejected | recent_move={recent_move:.2f}% > {MAX_RECENT_MOVE_PCT:.2f}%"
            )
            return None

        long_ok, long_reason = _long_conditions(row, recent)
        short_ok, short_reason = _short_conditions(row, recent)

        direction = None
        score = 0.0
        reason = ""

        if long_ok:
            direction = "LONG"
            score = _score_long(row, recent)
            reason = long_reason

        elif short_ok:
            direction = "SHORT"
            score = _score_short(row, recent)
            reason = short_reason

        else:
            logger.info(
                f"[SIGNAL-REASON] {symbol} no signal | long={long_reason} short={short_reason}"
            )
            return None

        if score < MIN_SIGNAL_SCORE:
            logger.info(
                f"[SIGNAL-REASON] {symbol} {direction} rejected | score={score} < {MIN_SIGNAL_SCORE}"
            )
            return None

        entry = float(row["close"])
        atr_value = float(row["atr"])

        prices = _calculate_prices(direction, entry, atr_value)

        if not prices:
            logger.info(
                f"[SIGNAL-REASON] {symbol} {direction} rejected | price model failed"
            )
            return None

        tp_price, sl_price, tp_roi_pct, sl_roi_pct = prices

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} SL={sl_price:.6g} "
            f"score={score} reason={reason} strategy=Squeeze-WT-Supertrend"
        )

        wt1 = float(row.get("wt1", 0.0))
        wt2 = float(row.get("wt2", 0.0))
        sqz_val = float(row.get("sqz_val", 0.0))

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
                f"Squeeze WT Supertrend | {ENTRY_TF} | "
                f"WT {wt1:.1f}/{wt2:.1f} | SQZ {sqz_val:.2f}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None


# Old architecture compatibility.
# The new strategy fires direct signals from analyze_coin().
def detect_setup(symbol: str) -> dict | None:
    return None


def evaluate_pending_setup(setup: dict):
    return "EXPIRED", None