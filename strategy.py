"""
Hybrid SMC Pro — 1h Trend + 5m Sweep OB Retest + ATR/Volume Filter

Signal flow:
  1. 15m market structure bias (bullish/bearish swing structure)
  2. 5m liquidity sweep (stop hunt beyond recent swing)
  3. 5m displacement candle (strong move away from sweep)
  4. 5m order block (last opposing candle before displacement)
  5. detect_setup() builds a pending setup dict
  6. evaluate_pending_setup() fires when price retests the OB

Confirmation filters added on top of the SMC core:
  - 1h EMA50/200 trend alignment   (ENABLE_HTF_FILTER)
  - 5m EMA20/50 entry alignment     (ENABLE_ENTRY_EMA_FILTER)
  - ATR% band filter                (ENABLE_ATR_FILTER)
  - Displacement candle volume      (ENABLE_VOLUME_FILTER)
  - BTC market regime guard         (ENABLE_BTC_FILTER)
  - Composite score >= MIN_SETUP_SCORE

All indicators are computed manually with pandas/numpy — no external TA library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from mexc_client import get_klines
from config import (
    # timeframes
    TREND_TF, ENTRY_TF, HTF_TREND_TF,
    HTF_KLINE_COUNT,
    ENTRY_EMA_KLINE_COUNT,
    BTC_KLINE_COUNT,
    # strategy (legacy wrapper)
    STRATEGY_TF, STRATEGY_KLINE_COUNT,
    EMA_FAST, EMA_SLOW, CCI_LENGTH, SL_LOOKBACK,
    REWARD_RATIO, LEVERAGE,
    CCI_MIN_ABS, MAX_SL_PCT, MIN_SL_PCT,
    BOS_LOOKBACK, DOUBLE_LOOKBACK, DOUBLE_TOLERANCE_PCT, PATTERN_MIN_SCORE,
    CANDLE_MINUTES,
    # HTF filter
    ENABLE_HTF_FILTER, HTF_EMA_FAST, HTF_EMA_SLOW,
    # entry EMA filter
    ENABLE_ENTRY_EMA_FILTER, EMA_FAST_FILTER, EMA_SLOW_FILTER,
    # ATR filter
    ENABLE_ATR_FILTER, ATR_PERIOD, MIN_ATR_PCT, MAX_ATR_PCT, ATR_SL_MULTIPLIER,
    # volume filter
    ENABLE_VOLUME_FILTER, VOLUME_LOOKBACK, MIN_VOLUME_MULTIPLIER,
    # BTC regime filter
    ENABLE_BTC_FILTER, BTC_SYMBOL, BTC_TF, BTC_EMA_PERIOD,
    # scoring
    MIN_SETUP_SCORE,
)

logger = logging.getLogger(__name__)


# ── dataclass returned by analyze_coin (legacy path) ─────────────

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


# ══════════════════════════════════════════════════════════════════
# Shared indicator helpers (pure pandas/numpy, no TA library)
# ══════════════════════════════════════════════════════════════════

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Average True Range computed manually."""
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _cci(df: pd.DataFrame, period: int) -> pd.Series:
    hlc3 = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3.0
    ma   = hlc3.rolling(period).mean()
    mad  = hlc3.rolling(period).apply(lambda x: (abs(x - x.mean())).mean(), raw=True)
    return (hlc3 - ma) / (0.015 * mad)


def _ema_slope(series: pd.Series, lookback: int = 3) -> float:
    """Positive slope = rising EMA over last `lookback` bars."""
    if len(series) < lookback + 1:
        return 0.0
    return float(series.iloc[-1]) - float(series.iloc[-1 - lookback])


# ══════════════════════════════════════════════════════════════════
# HTF trend filter  (1h)
# ══════════════════════════════════════════════════════════════════

def _htf_trend_ok(symbol: str, direction: str) -> tuple[bool, bool]:
    """
    Returns (allowed, strong_agreement).

    allowed          — setup is not blocked by HTF filter
    strong_agreement — EMA50 > EMA200 (LONG) or EMA50 < EMA200 (SHORT),
                       used for bonus scoring
    """
    if not ENABLE_HTF_FILTER:
        return True, False

    try:
        df = get_klines(symbol, HTF_TREND_TF, count=HTF_KLINE_COUNT)
        if df is None or df.empty or len(df) < HTF_EMA_SLOW + 5:
            logger.warning("[HTF] %s — not enough candles, skipping filter", symbol)
            return True, False

        close   = df["close"].astype(float)
        ema_fast = _ema(close, HTF_EMA_FAST)
        ema_slow = _ema(close, HTF_EMA_SLOW)

        last_close    = float(close.iloc[-1])
        last_ema_fast = float(ema_fast.iloc[-1])
        last_ema_slow = float(ema_slow.iloc[-1])
        slope_fast    = _ema_slope(ema_fast)

        if direction == "LONG":
            strong = last_ema_fast > last_ema_slow
            # allow if strong alignment OR: close > EMA200 with rising EMA50
            allowed = strong or (last_close > last_ema_slow and slope_fast > 0)
        else:
            strong = last_ema_fast < last_ema_slow
            allowed = strong or (last_close < last_ema_slow and slope_fast < 0)

        return allowed, strong

    except Exception as e:
        logger.warning("[HTF] %s fetch error: %s — filter skipped", symbol, e)
        return True, False


# ══════════════════════════════════════════════════════════════════
# Entry EMA alignment filter  (5m)
# ══════════════════════════════════════════════════════════════════

def _entry_ema_ok(df5: pd.DataFrame, direction: str) -> bool:
    """
    Checks EMA20/50 alignment on the entry timeframe dataframe.
    df5 must already be fetched and have enough rows.
    """
    if not ENABLE_ENTRY_EMA_FILTER:
        return True

    if len(df5) < EMA_SLOW_FILTER + 5:
        return True  # not enough data — don't block

    close    = df5["close"].astype(float)
    ema_fast = _ema(close, EMA_FAST_FILTER)
    ema_slow = _ema(close, EMA_SLOW_FILTER)

    last_close    = float(close.iloc[-1])
    last_ema_fast = float(ema_fast.iloc[-1])
    last_ema_slow = float(ema_slow.iloc[-1])

    if direction == "LONG":
        return last_close > last_ema_fast and last_ema_fast >= last_ema_slow
    else:
        return last_close < last_ema_fast and last_ema_fast <= last_ema_slow


# ══════════════════════════════════════════════════════════════════
# ATR filter  (5m)
# ══════════════════════════════════════════════════════════════════

def _atr_check(df5: pd.DataFrame) -> tuple[bool, float]:
    """
    Returns (in_range, atr_pct).
    in_range — True when ATR% is within [MIN_ATR_PCT, MAX_ATR_PCT].
    """
    if not ENABLE_ATR_FILTER:
        return True, 0.0

    if len(df5) < ATR_PERIOD + 2:
        return True, 0.0

    atr_series = _atr(df5, ATR_PERIOD)
    last_atr   = float(atr_series.iloc[-1])
    last_close = float(df5["close"].astype(float).iloc[-1])

    if last_close <= 0:
        return True, 0.0

    atr_pct = last_atr / last_close * 100.0
    in_range = MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT
    return in_range, round(atr_pct, 3)


def _current_atr(df5: pd.DataFrame) -> float:
    """Return raw ATR value for SL buffer calculation."""
    if len(df5) < ATR_PERIOD + 2:
        return 0.0
    return float(_atr(df5, ATR_PERIOD).iloc[-1])


# ══════════════════════════════════════════════════════════════════
# Volume confirmation  (5m)
# ══════════════════════════════════════════════════════════════════

def _volume_ok(df5: pd.DataFrame, disp_idx: int) -> bool:
    """
    Checks that the displacement candle volume >= avg * MIN_VOLUME_MULTIPLIER.
    disp_idx is the positional index inside df5 of the displacement candle.
    """
    if not ENABLE_VOLUME_FILTER:
        return True

    try:
        vol_col = df5["volume"].astype(float)

        if vol_col.iloc[disp_idx] <= 0:
            return True  # no volume data — skip gracefully

        start = max(0, disp_idx - VOLUME_LOOKBACK)
        avg_vol = float(vol_col.iloc[start:disp_idx].mean())

        if avg_vol <= 0:
            return True  # can't compute average — skip

        return float(vol_col.iloc[disp_idx]) >= avg_vol * MIN_VOLUME_MULTIPLIER

    except Exception:
        return True  # never crash on volume


# ══════════════════════════════════════════════════════════════════
# BTC market regime filter
# ══════════════════════════════════════════════════════════════════

def _btc_regime_ok(direction: str) -> bool:
    """
    Returns False only when BTC is strongly trending against the signal direction.
    Fails open (returns True) on any fetch/parse error.
    """
    if not ENABLE_BTC_FILTER:
        return True

    try:
        df = get_klines(BTC_SYMBOL, BTC_TF, count=BTC_KLINE_COUNT)
        if df is None or df.empty or len(df) < BTC_EMA_PERIOD + 5:
            logger.warning("[BTC-REGIME] fetch returned insufficient data — filter skipped")
            return True

        close    = df["close"].astype(float)
        ema50    = _ema(close, BTC_EMA_PERIOD)
        last_close = float(close.iloc[-1])
        last_ema   = float(ema50.iloc[-1])
        slope      = _ema_slope(ema50)

        strongly_bullish = last_close > last_ema and slope > 0
        strongly_bearish = last_close < last_ema and slope < 0

        if direction == "LONG" and strongly_bearish:
            return False
        if direction == "SHORT" and strongly_bullish:
            return False

        return True

    except Exception as e:
        logger.warning("[BTC-REGIME] fetch error: %s — filter skipped", e)
        return True


# ══════════════════════════════════════════════════════════════════
# 15m market structure helpers
# ══════════════════════════════════════════════════════════════════

def _swing_high(series: pd.Series, i: int, radius: int = 2) -> bool:
    left  = series.iloc[max(0, i - radius): i]
    right = series.iloc[i + 1: i + 1 + radius]
    val   = series.iloc[i]
    return bool((left < val).all() and (right < val).all())


def _swing_low(series: pd.Series, i: int, radius: int = 2) -> bool:
    left  = series.iloc[max(0, i - radius): i]
    right = series.iloc[i + 1: i + 1 + radius]
    val   = series.iloc[i]
    return bool((left > val).all() and (right > val).all())


def _get_15m_bias(symbol: str) -> str | None:
    """
    Returns 'bullish', 'bearish', or None (no clear structure).

    Bullish = most recent confirmed swing low is higher than the prior swing low
              AND most recent swing high is higher than the prior swing high.
    Bearish = opposite.
    """
    try:
        df = get_klines(symbol, TREND_TF, count=120)
        if df is None or df.empty or len(df) < 20:
            return None

        highs = df["high"].astype(float)
        lows  = df["low"].astype(float)

        swing_highs = [i for i in range(2, len(df) - 2) if _swing_high(highs, i)]
        swing_lows  = [i for i in range(2, len(df) - 2) if _swing_low(lows,  i)]

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return None

        hh = highs.iloc[swing_highs[-1]] > highs.iloc[swing_highs[-2]]
        hl = lows.iloc[swing_lows[-1]]   > lows.iloc[swing_lows[-2]]
        lh = highs.iloc[swing_highs[-1]] < highs.iloc[swing_highs[-2]]
        ll = lows.iloc[swing_lows[-1]]   < lows.iloc[swing_lows[-2]]

        if hh and hl:
            return "bullish"
        if lh and ll:
            return "bearish"
        return None

    except Exception as e:
        logger.warning("[BIAS] %s: %s", symbol, e)
        return None


# ══════════════════════════════════════════════════════════════════
# 5m sweep + displacement + OB detection
# ══════════════════════════════════════════════════════════════════

_ENTRY_KLINE_COUNT = max(ENTRY_EMA_KLINE_COUNT, 150)


def _detect_5m_setup(
    symbol: str,
    bias: str,
) -> dict | None:
    """
    Returns a raw setup dict or None.

    Keys: direction, sweep_idx, sweep_extreme, disp_idx,
          ob_high, ob_low, ob_time, df5 (DataFrame)
    """
    try:
        df = get_klines(symbol, ENTRY_TF, count=_ENTRY_KLINE_COUNT)
        if df is None or df.empty or len(df) < 40:
            return None

        direction = "LONG" if bias == "bullish" else "SHORT"

        highs  = df["high"].astype(float)
        lows   = df["low"].astype(float)
        closes = df["close"].astype(float)

        # ── find recent swing high/low to sweep ──────────────────
        lookback = 30
        anchor_end = len(df) - 5  # leave room for sweep + displacement

        if direction == "LONG":
            # look for a recent swing low that got swept (price dipped below)
            candidates = [
                i for i in range(2, anchor_end - 2)
                if _swing_low(lows, i, radius=2)
            ]
            if not candidates:
                return None

            # most recent swing low
            swing_i   = candidates[-1]
            swing_lvl = float(lows.iloc[swing_i])

            # sweep candle: close or low pierces below swing_lvl
            sweep_i = None
            for j in range(swing_i + 1, len(df) - 3):
                if float(lows.iloc[j]) < swing_lvl:
                    sweep_i = j
                    break

            if sweep_i is None:
                return None

            sweep_extreme = float(lows.iloc[sweep_i])

            # displacement candle: bullish close after sweep, body > 50% of range
            disp_i = None
            for j in range(sweep_i + 1, min(sweep_i + 6, len(df) - 1)):
                o = float(df["open"].astype(float).iloc[j])
                c = float(closes.iloc[j])
                h = float(highs.iloc[j])
                l = float(lows.iloc[j])
                rng = h - l
                body = abs(c - o)
                if c > o and rng > 0 and body / rng >= 0.5:
                    disp_i = j
                    break

            if disp_i is None:
                return None

            # OB = last bearish candle before displacement
            ob_i = None
            for j in range(disp_i - 1, sweep_i - 1, -1):
                o = float(df["open"].astype(float).iloc[j])
                c = float(closes.iloc[j])
                if o > c:  # bearish
                    ob_i = j
                    break

            if ob_i is None:
                return None

            ob_high = float(highs.iloc[ob_i])
            ob_low  = float(lows.iloc[ob_i])

        else:  # SHORT
            candidates = [
                i for i in range(2, anchor_end - 2)
                if _swing_high(highs, i, radius=2)
            ]
            if not candidates:
                return None

            swing_i   = candidates[-1]
            swing_lvl = float(highs.iloc[swing_i])

            sweep_i = None
            for j in range(swing_i + 1, len(df) - 3):
                if float(highs.iloc[j]) > swing_lvl:
                    sweep_i = j
                    break

            if sweep_i is None:
                return None

            sweep_extreme = float(highs.iloc[sweep_i])

            disp_i = None
            for j in range(sweep_i + 1, min(sweep_i + 6, len(df) - 1)):
                o = float(df["open"].astype(float).iloc[j])
                c = float(closes.iloc[j])
                h = float(highs.iloc[j])
                l = float(lows.iloc[j])
                rng = h - l
                body = abs(c - o)
                if c < o and rng > 0 and body / rng >= 0.5:
                    disp_i = j
                    break

            if disp_i is None:
                return None

            ob_i = None
            for j in range(disp_i - 1, sweep_i - 1, -1):
                o = float(df["open"].astype(float).iloc[j])
                c = float(closes.iloc[j])
                if o < c:  # bullish
                    ob_i = j
                    break

            if ob_i is None:
                return None

            ob_high = float(highs.iloc[ob_i])
            ob_low  = float(lows.iloc[ob_i])

        ob_time = df.index[ob_i]

        return {
            "direction":     direction,
            "sweep_idx":     sweep_i,
            "sweep_extreme": sweep_extreme,
            "disp_idx":      disp_i,
            "ob_high":       ob_high,
            "ob_low":        ob_low,
            "ob_time":       ob_time,
            "df5":           df,
        }

    except Exception as e:
        logger.warning("[5M-DETECT] %s: %s", symbol, e)
        return None


# ══════════════════════════════════════════════════════════════════
# Score calculation
# ══════════════════════════════════════════════════════════════════

def _base_score(
    direction: str,
    ob_time: pd.Timestamp,
    sweep_time: pd.Timestamp,
    rr: float,
) -> float:
    """Replicate original RR + age scoring."""
    now = pd.Timestamp.now(tz="UTC")

    ob_age_min    = (now - ob_time.tz_localize("UTC") if ob_time.tzinfo is None
                     else now - ob_time).total_seconds() / 60
    sweep_age_min = (now - sweep_time.tz_localize("UTC") if sweep_time.tzinfo is None
                     else now - sweep_time).total_seconds() / 60

    # RR contribution (0-40 pts)
    rr_score = min(rr / 3.0, 1.0) * 40.0

    # Age penalty: fresher is better, max -20 pts
    ob_penalty    = min(ob_age_min    / 60.0, 1.0) * 10.0
    sweep_penalty = min(sweep_age_min / 60.0, 1.0) * 10.0

    return max(rr_score - ob_penalty - sweep_penalty, 20.0)


# ══════════════════════════════════════════════════════════════════
# Public: detect_setup
# ══════════════════════════════════════════════════════════════════

def detect_setup(symbol: str) -> dict | None:
    """
    Entry point called by main.py every 5 minutes.

    Returns a setup dict ready to be saved to pending_setups,
    or None if no valid setup was found.
    """
    # ── 1. 15m bias ──────────────────────────────────────────────
    bias = _get_15m_bias(symbol)
    if bias is None:
        logger.info("[NO-SETUP] %s | no clear 15m structure", symbol)
        return None

    direction = "LONG" if bias == "bullish" else "SHORT"

    # ── 2. HTF trend filter ───────────────────────────────────────
    htf_ok, htf_strong = _htf_trend_ok(symbol, direction)
    if not htf_ok:
        logger.info("[SETUP-REJECT] %s | HTF trend mismatch", symbol)
        return None

    # ── 3. BTC regime filter ──────────────────────────────────────
    if not _btc_regime_ok(direction):
        logger.info("[SETUP-REJECT] %s | BTC filter conflict", symbol)
        return None

    # ── 4. 5m sweep + displacement + OB ──────────────────────────
    raw = _detect_5m_setup(symbol, bias)
    if raw is None:
        logger.info("[NO-SETUP] %s | no sweep/displacement/OB found on %s", symbol, ENTRY_TF)
        return None

    df5           = raw["df5"]
    disp_idx      = raw["disp_idx"]
    ob_high       = raw["ob_high"]
    ob_low        = raw["ob_low"]
    ob_time       = raw["ob_time"]
    sweep_extreme = raw["sweep_extreme"]
    sweep_time    = df5.index[raw["sweep_idx"]]

    # ── 5. ATR filter & SL buffer ─────────────────────────────────
    atr_ok, atr_pct = _atr_check(df5)
    if not atr_ok:
        logger.info(
            "[SETUP-REJECT] %s | ATR %.2f%% outside %.2f-%.1f",
            symbol, atr_pct, MIN_ATR_PCT, MAX_ATR_PCT,
        )
        return None

    atr_val = _current_atr(df5)

    # ── 6. Entry EMA alignment ─────────────────────────────────────
    ema_ok = _entry_ema_ok(df5, direction)
    if not ema_ok:
        logger.info("[SETUP-REJECT] %s | entry EMA misaligned (%s)", symbol, direction)
        return None

    # ── 7. Volume on displacement candle ──────────────────────────
    vol_ok = _volume_ok(df5, disp_idx)
    if not vol_ok:
        logger.info("[SETUP-REJECT] %s | volume weak on displacement candle", symbol)
        return None

    # ── 8. SL with ATR buffer ─────────────────────────────────────
    entry = float(df5["close"].astype(float).iloc[-1])

    if direction == "LONG":
        sl_anchor = min(sweep_extreme, ob_low)
        sl_price  = sl_anchor - atr_val * ATR_SL_MULTIPLIER
    else:
        sl_anchor = max(sweep_extreme, ob_high)
        sl_price  = sl_anchor + atr_val * ATR_SL_MULTIPLIER

    risk = abs(entry - sl_price)
    if risk <= 0:
        logger.info("[SETUP-REJECT] %s | zero risk distance", symbol)
        return None

    sl_pct = risk / entry * 100.0

    if sl_pct < MIN_SL_PCT:
        logger.info("[SETUP-REJECT] %s | SL %.2f%% below min %.2f%%", symbol, sl_pct, MIN_SL_PCT)
        return None

    if sl_pct > MAX_SL_PCT:
        logger.info("[SETUP-REJECT] %s | SL %.2f%% above max %.2f%%", symbol, sl_pct, MAX_SL_PCT)
        return None

    sign     = 1.0 if direction == "LONG" else -1.0
    tp_price = entry + sign * risk * REWARD_RATIO
    rr       = REWARD_RATIO

    # ── 9. Composite score ────────────────────────────────────────
    score = _base_score(direction, ob_time, sweep_time, rr)

    if htf_strong:
        score += 10.0

    if vol_ok and ENABLE_VOLUME_FILTER:
        score += 10.0

    if atr_ok and ENABLE_ATR_FILTER:
        score += 10.0

    if ema_ok and ENABLE_ENTRY_EMA_FILTER:
        score += 5.0

    score = min(score, 100.0)

    if score < MIN_SETUP_SCORE:
        logger.info(
            "[SETUP-REJECT] %s | score %.1f < min %d", symbol, score, MIN_SETUP_SCORE
        )
        return None

    # ── 10. Build setup dict ──────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    tp_pct  = risk * REWARD_RATIO / entry * 100.0
    sl_msg  = f"{sl_pct:.1f}%"

    logger.info(
        "[SETUP] %s %s | score=%.1f | ATR=%.2f%% | SL=%.4g | TP=%.4g | RR=1:%.1f",
        direction, symbol, score, atr_pct, sl_price, tp_price, rr,
    )

    return {
        "symbol":        symbol,
        "direction":     direction,
        "status":        "waiting",
        "trend_tf":      TREND_TF,
        "entry_tf":      ENTRY_TF,
        "bias":          bias,
        "bias_break":    None,
        "sweep_type":    "low_sweep" if direction == "LONG" else "high_sweep",
        "sweep_level":   float(df5["low" if direction == "LONG" else "high"]
                               .astype(float).iloc[raw["sweep_idx"] - 1])
                         if raw["sweep_idx"] > 0 else entry,
        "sweep_extreme": sweep_extreme,
        "sweep_time":    sweep_time.isoformat(),
        "ob_type":       "bearish_ob" if direction == "LONG" else "bullish_ob",
        "ob_low":        ob_low,
        "ob_high":       ob_high,
        "ob_time":       ob_time.isoformat(),
        "target_price":  round(tp_price, 8),
        "sl_price":      round(sl_price, 8),
        "rr_estimate":   rr,
        "score":         round(score, 1),
        "setup_time":    now_utc.isoformat(),
        "expires_at":    (now_utc + timedelta(hours=4)).isoformat(),
        "created_at":    now_utc.isoformat(),
        # extra context fields (not in DB schema, used by signal message)
        "_entry_price":  entry,
        "_atr_pct":      atr_pct,
        "_htf_strong":   htf_strong,
    }


# ══════════════════════════════════════════════════════════════════
# Public: evaluate_pending_setup
# ══════════════════════════════════════════════════════════════════

def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    """
    Called every 1 minute by main.py for each waiting setup.

    Returns:
        ("WAIT",   None)          — price not yet in OB
        ("FIRE",   Signal)        — price retesting OB, signal ready to broadcast
        ("EXPIRE", None)          — setup is stale or invalidated
    """
    symbol    = setup["symbol"]
    direction = setup["direction"]
    ob_high   = float(setup["ob_high"])
    ob_low    = float(setup["ob_low"])
    sl_price  = float(setup["sl_price"])
    tp_price  = float(setup["target_price"])
    score     = float(setup.get("score", 0.0))

    try:
        df = get_klines(symbol, ENTRY_TF, count=10)
        if df is None or df.empty:
            return "WAIT", None

        last_close = float(df["close"].astype(float).iloc[-1])
        last_low   = float(df["low"].astype(float).iloc[-1])
        last_high  = float(df["high"].astype(float).iloc[-1])

        # Invalidation: price closed beyond SL before OB retest
        if direction == "LONG" and last_close < sl_price:
            logger.info("[SETUP-EXPIRE] %s LONG | price closed below SL before retest", symbol)
            return "EXPIRE", None

        if direction == "SHORT" and last_close > sl_price:
            logger.info("[SETUP-EXPIRE] %s SHORT | price closed above SL before retest", symbol)
            return "EXPIRE", None

        # OB retest check
        if direction == "LONG":
            in_ob = last_low <= ob_high and last_close >= ob_low
        else:
            in_ob = last_high >= ob_low and last_close <= ob_high

        if not in_ob:
            return "WAIT", None

        # Retest confirmed — build Signal
        entry    = last_close
        risk     = abs(entry - sl_price)
        if risk <= 0:
            return "WAIT", None

        sl_pct   = risk / entry * 100.0
        tp_pct   = risk * REWARD_RATIO / entry * 100.0
        tp_roi   = round(tp_pct * LEVERAGE, 1)
        sl_roi   = round(sl_pct * LEVERAGE, 1)

        atr_pct  = setup.get("_atr_pct", 0.0)
        htf_str  = "HTF✓" if setup.get("_htf_strong") else "HTF~"

        summary = (
            f"SMC Pro | {TREND_TF} bias + {ENTRY_TF} OB retest | "
            f"ATR {atr_pct:.2f}% | {htf_str} | RR 1:{REWARD_RATIO}"
        )

        sig = Signal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(entry + (1.0 if direction == "LONG" else -1.0) * risk * REWARD_RATIO, 8),
            sl_price=round(sl_price, 8),
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi,
            sl_roi_pct=sl_roi,
            timeframe_summary=summary,
            generated_at=datetime.now(timezone.utc),
            score=min(score, 100.0),
        )

        logger.info(
            "[FIRE] %s %s | entry=%.6g TP=%.6g SL=%.6g score=%.1f",
            direction, symbol, entry, sig.tp_price, sig.sl_price, score,
        )

        return "FIRE", sig

    except Exception as e:
        logger.error("[EVAL] %s: %s", symbol, e, exc_info=True)
        return "WAIT", None


# ══════════════════════════════════════════════════════════════════
# Legacy analyze_coin wrapper (kept for backward compatibility)
# ══════════════════════════════════════════════════════════════════

def _recent_sl(df: pd.DataFrame, end_idx: int, direction: str) -> float:
    start  = max(0, end_idx - SL_LOOKBACK + 1)
    window = df.iloc[start: end_idx + 1]
    if direction == "LONG":
        return float(window["low"].astype(float).min())
    else:
        return float(window["high"].astype(float).max())


def _bos(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    start  = max(0, end_idx - BOS_LOOKBACK)
    window = df.iloc[start: end_idx - 1]
    if len(window) < 3:
        return False
    close = float(df.iloc[end_idx]["close"])
    if direction == "LONG":
        return close > float(window["high"].astype(float).max())
    else:
        return close < float(window["low"].astype(float).min())


def _flag(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
    if end_idx < 10:
        return False
    pole      = df.iloc[max(0, end_idx - 10): end_idx - 3]
    flag_bars = df.iloc[end_idx - 3: end_idx]
    if len(pole) < 3 or len(flag_bars) < 2:
        return False
    pole_start_close = float(pole.iloc[0]["close"])
    flag_high  = float(flag_bars["high"].astype(float).max())
    flag_low   = float(flag_bars["low"].astype(float).min())
    flag_range = flag_high - flag_low
    signal_close = float(df.iloc[end_idx]["close"])
    if direction == "LONG":
        pole_move = float(pole["close"].astype(float).max()) - pole_start_close
        if pole_move / max(pole_start_close, 1e-12) < 0.005:
            return False
        if flag_range > pole_move * 0.5:
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
    if end_idx < 2:
        return False
    mother = df.iloc[end_idx - 2]
    inside = df.iloc[end_idx - 1]
    signal = df.iloc[end_idx]
    if not (float(inside["high"]) < float(mother["high"]) and
            float(inside["low"])  > float(mother["low"])):
        return False
    signal_close = float(signal["close"])
    if direction == "LONG":
        return signal_close > float(inside["high"])
    else:
        return signal_close < float(inside["low"])


def _swing_lows(df: pd.DataFrame, end_idx: int, lookback: int) -> list[float]:
    window = df.iloc[max(0, end_idx - lookback): end_idx]
    lows = window["low"].astype(float).values
    return [lows[i] for i in range(1, len(lows) - 1)
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]]


def _swing_highs(df: pd.DataFrame, end_idx: int, lookback: int) -> list[float]:
    window = df.iloc[max(0, end_idx - lookback): end_idx]
    highs = window["high"].astype(float).values
    return [highs[i] for i in range(1, len(highs) - 1)
            if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]]


def _double_pattern(df: pd.DataFrame, end_idx: int, direction: str) -> bool:
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
    score   = 0
    matched = []
    if _bos(df, end_idx, direction):
        score += 1; matched.append("BOS")
    if _flag(df, end_idx, direction):
        score += 1; matched.append("Flag")
    if _inside_bar_breakout(df, end_idx, direction):
        score += 1; matched.append("IB")
    if _triple_pattern(df, end_idx, direction):
        score += 3; matched.append("Triple")
    elif _double_pattern(df, end_idx, direction):
        score += 2; matched.append("Double")
    return score, matched


def analyze_coin(symbol: str) -> Signal | None:
    """
    Original EMA10/20 + CCI + pattern scoring strategy.
    Kept intact for backward compatibility with main.py scan_for_signals.
    """
    try:
        raw = get_klines(symbol, STRATEGY_TF, count=STRATEGY_KLINE_COUNT)
        if raw is None or raw.empty:
            return None

        close     = raw["close"].astype(float)
        ema_fast  = _ema(close, EMA_FAST)
        ema_slow  = _ema(close, EMA_SLOW)
        cci_vals  = _cci(raw, CCI_LENGTH)

        completed = raw.iloc[:-1].copy()
        ef_c      = ema_fast.iloc[:-1]
        es_c      = ema_slow.iloc[:-1]
        cci_c     = cci_vals.iloc[:-1]

        if len(completed) < max(EMA_SLOW, CCI_LENGTH, DOUBLE_LOOKBACK) + 5:
            return None

        last_idx = len(completed) - 1
        last     = completed.iloc[last_idx]
        prev     = completed.iloc[last_idx - 1]

        ef_now  = float(ef_c.iloc[-1])
        es_now  = float(es_c.iloc[-1])
        ef_prev = float(ef_c.iloc[-2])
        es_prev = float(es_c.iloc[-2])
        cci_now = float(cci_c.iloc[-1])

        cross_up   = ef_prev <= es_prev and ef_now > es_now
        cross_down = ef_prev >= es_prev and ef_now < es_now

        if cross_up and cci_now > 0:
            direction = "LONG"
        elif cross_down and cci_now < 0:
            direction = "SHORT"
        else:
            return None

        if abs(cci_now) < CCI_MIN_ABS:
            return None

        candle_open  = completed.iloc[last_idx].name.to_pydatetime().replace(tzinfo=timezone.utc)
        candle_close = candle_open + timedelta(minutes=CANDLE_MINUTES)
        age = datetime.now(timezone.utc) - candle_close
        if age > timedelta(minutes=CANDLE_MINUTES):
            return None

        entry    = float(last["close"])
        sl_price = _recent_sl(completed, last_idx, direction)
        risk     = abs(entry - sl_price)

        if risk <= 0:
            return None
        if direction == "LONG"  and sl_price >= entry:
            return None
        if direction == "SHORT" and sl_price <= entry:
            return None

        sl_pct = risk / entry * 100.0
        if sl_pct > MAX_SL_PCT:
            return None

        p_score, patterns = _pattern_score(completed, last_idx, direction)
        if p_score < PATTERN_MIN_SCORE:
            return None

        sign       = 1.0 if direction == "LONG" else -1.0
        tp_price   = entry + sign * risk * REWARD_RATIO
        tp_move_pct = risk * REWARD_RATIO / entry * 100.0
        tp_roi_pct  = round(tp_move_pct * LEVERAGE, 1)
        sl_roi_pct  = round(sl_pct * LEVERAGE, 1)

        pattern_str = "+".join(patterns)
        score_val   = float(min(p_score * 20 + 40, 100))

        logger.info(
            "[SIGNAL] %s %s | EMA%d/EMA%d | CCI=%.1f | %s(score=%d) | "
            "Entry=%.6g TP=%.6g SL=%.6g RR=1:%.1f",
            direction, symbol, EMA_FAST, EMA_SLOW, cci_now,
            pattern_str, p_score, entry, tp_price, sl_price, REWARD_RATIO,
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
            score=score_val,
        )

    except Exception as e:
        logger.error("Error analyzing %s: %s", symbol, e, exc_info=True)
        return None
