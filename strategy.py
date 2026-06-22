"""
MTF Trend Pullback + Volume Confirmation strategy.

Signal flow:
    detect_armed_setup(symbol)
        1. 1D close vs EMA200   — macro direction
        2. 4H close vs EMA200   — main trend direction
        3. 1H close vs EMA50    — setup direction
        4. All three must agree; determines direction
        5. 15m: ATR, EMA20/EMA50, recent S/R
        6. Volume: latest completed 15m candle >= avg * VOLUME_MULTIPLIER
        7. Pullback check: price near EMA20/EMA50/support (within 1.5 ATR)
        8. Entry zone = [level - ATR*0.25, level + ATR*0.25]
        9. SL = swing low/high ± ATR buffer
       10. TP = trigger + risk * MIN_RR
       11. Score >= MIN_SETUP_SCORE → return ArmedSetup dict

    calculate_signal_from_setup(setup, live_price)
        Called by trigger_engine when WebSocket price enters entry zone.
        Recalculates TP from actual live entry price.
        Returns Signal ready for Telegram broadcast.

No external TA packages — pandas only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import pandas as pd

from mexc_client import get_klines
from config import (
    MACRO_TF,
    MAIN_TF,
    SETUP_TF,
    ENTRY_TF,
    MACRO_KLINE_COUNT,
    MAIN_KLINE_COUNT,
    SETUP_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    EMA_MACRO,
    EMA_MAIN,
    EMA_SETUP,
    EMA_ENTRY_FAST,
    EMA_ENTRY_SLOW,
    ATR_PERIOD,
    VOLUME_PERIOD,
    VOLUME_MULTIPLIER,
    SR_LOOKBACK_CANDLES,
    ENTRY_ZONE_ATR_MULTIPLIER,
    SL_ATR_MULTIPLIER,
    MIN_RR,
    MAX_RR,
    MIN_SETUP_SCORE,
    ARMED_SETUP_EXPIRE_MINUTES,
    LEVERAGE,
)

logger = logging.getLogger(__name__)


# ── Dataclasses ───────────────────────────────────────────────────

@dataclass
class Signal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    tp_roi_pct: float
    sl_roi_pct: float
    timeframe_summary: str
    generated_at: datetime
    score: float = 0.0
    entry_low: float = 0.0
    entry_high: float = 0.0
    rr: float = 0.0


@dataclass
class ArmedSetup:
    symbol: str
    direction: str
    trigger_price: float
    entry_low: float
    entry_high: float
    sl_price: float
    tp_price: float
    rr: float
    score: float
    setup_reason: str
    trend_summary: str
    expires_at: str


# ── Indicator helpers ─────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high      = df["high"].astype(float)
    low       = df["low"].astype(float)
    close     = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _atr_scalar(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    if df is None or df.empty or len(df) < period + 2:
        return 0.0
    val = float(_atr(df, period).iloc[-1])
    return val if val > 0 else 0.0


# ── Candle / swing helpers ────────────────────────────────────────

def _find_swings(df: pd.DataFrame, left: int = 3, right: int = 2) -> list[dict]:
    swings: list[dict] = []
    if len(df) < left + right + 5:
        return swings
    highs = df["high"].astype(float)
    lows  = df["low"].astype(float)
    for i in range(left, len(df) - right):
        h = float(highs.iloc[i])
        l = float(lows.iloc[i])
        if h > float(highs.iloc[i - left:i].max()) and h > float(highs.iloc[i + 1:i + right + 1].max()):
            swings.append({"type": "HIGH", "pos": i, "price": h})
        if l < float(lows.iloc[i - left:i].min()) and l < float(lows.iloc[i + 1:i + right + 1].min()):
            swings.append({"type": "LOW",  "pos": i, "price": l})
    swings.sort(key=lambda x: x["pos"])
    return swings


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


# ── Pullback detection ────────────────────────────────────────────

def _pullback_info(
    direction: str,
    current_close: float,
    ema20: float,
    ema50: float,
    atr: float,
    swings: list[dict],
) -> tuple[str | None, float, float]:
    """
    Returns (level_name, level_price, dist_to_level) for the closest qualifying
    pullback level, or (None, 0, inf) when no pullback is detected.

    LONG: price near EMA20/EMA50 from above, or near a swing LOW
    SHORT: price near EMA20/EMA50 from below, or near a swing HIGH
    """
    max_dist = atr * 1.5
    best_name: str | None = None
    best_price = 0.0
    best_dist  = float("inf")

    def _try(name: str, level: float, valid: bool):
        nonlocal best_name, best_price, best_dist
        if not valid:
            return
        dist = abs(current_close - level)
        if dist <= max_dist and dist < best_dist:
            best_name  = name
            best_price = level
            best_dist  = dist

    if direction == "LONG":
        _try("EMA20", ema20, current_close >= ema20 - atr * 0.5)
        _try("EMA50", ema50, current_close >= ema50 - atr * 0.5)
        sr_lows = sorted([s["price"] for s in swings if s["type"] == "LOW"], reverse=True)
        if sr_lows:
            _try("SUPPORT", sr_lows[0], current_close >= sr_lows[0] - atr * 0.5)
    else:
        _try("EMA20", ema20, current_close <= ema20 + atr * 0.5)
        _try("EMA50", ema50, current_close <= ema50 + atr * 0.5)
        sr_highs = sorted([s["price"] for s in swings if s["type"] == "HIGH"])
        if sr_highs:
            _try("RESISTANCE", sr_highs[-1], current_close <= sr_highs[-1] + atr * 0.5)

    return best_name, best_price, best_dist


# ── Scoring ───────────────────────────────────────────────────────

def _score_setup(
    level_name: str,
    dist_to_level: float,
    atr: float,
    vol_ratio: float,
    rr: float,
) -> float:
    score = 50.0

    # Pullback quality
    if dist_to_level <= 0:
        dist_to_level = 1e-10

    relative_dist = dist_to_level / atr if atr > 0 else 1.0

    if "EMA20" in level_name:
        if relative_dist <= 0.5:
            score += 20.0
        elif relative_dist <= 1.0:
            score += 15.0
        else:
            score += 10.0
    elif "EMA50" in level_name:
        if relative_dist <= 0.5:
            score += 15.0
        elif relative_dist <= 1.0:
            score += 12.0
        else:
            score += 8.0
    else:  # SUPPORT / RESISTANCE
        if relative_dist <= 0.5:
            score += 12.0
        elif relative_dist <= 1.0:
            score += 8.0
        else:
            score += 5.0

    # Volume bonus
    if vol_ratio >= 2.0:
        score += 15.0
    elif vol_ratio >= 1.5:
        score += 12.0
    else:
        score += 10.0

    # RR bonus
    if rr >= 3.0:
        score += 10.0
    elif rr >= 2.5:
        score += 5.0

    return round(min(score, 100.0), 1)


# ── Public: detect armed setup ────────────────────────────────────

def detect_armed_setup(symbol: str) -> dict | None:
    try:
        # 1. Fetch all timeframes
        macro_df  = get_klines(symbol, MACRO_TF, count=MACRO_KLINE_COUNT)
        main_df   = get_klines(symbol, MAIN_TF,  count=MAIN_KLINE_COUNT)
        setup_df  = get_klines(symbol, SETUP_TF, count=SETUP_KLINE_COUNT)
        entry_df  = get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if any(df is None or df.empty for df in [macro_df, main_df, setup_df, entry_df]):
            return None

        # 2. 1D: close vs EMA200
        macro_close  = float(macro_df["close"].astype(float).iloc[-1])
        macro_ema200 = float(_ema(macro_df["close"].astype(float), EMA_MACRO).iloc[-1])
        if macro_close > macro_ema200:
            macro_dir = "LONG"
        elif macro_close < macro_ema200:
            macro_dir = "SHORT"
        else:
            return None

        # 3. 4H: close vs EMA200
        main_close  = float(main_df["close"].astype(float).iloc[-1])
        main_ema200 = float(_ema(main_df["close"].astype(float), EMA_MAIN).iloc[-1])
        if main_close > main_ema200:
            main_dir = "LONG"
        elif main_close < main_ema200:
            main_dir = "SHORT"
        else:
            return None

        # 4. 1H: close vs EMA50
        setup_close = float(setup_df["close"].astype(float).iloc[-1])
        setup_ema50 = float(_ema(setup_df["close"].astype(float), EMA_SETUP).iloc[-1])
        if setup_close > setup_ema50:
            setup_dir = "LONG"
        elif setup_close < setup_ema50:
            setup_dir = "SHORT"
        else:
            return None

        # All 3 must agree
        if not (macro_dir == main_dir == setup_dir):
            logger.info(
                "[ARMED-REJECT] %s | MTF mismatch 1D=%s 4H=%s 1H=%s",
                symbol, macro_dir, main_dir, setup_dir,
            )
            return None

        direction = macro_dir

        # 5. 15m analysis — use only completed candles
        completed = entry_df.iloc[:-1].copy()
        if len(completed) < max(EMA_ENTRY_SLOW, VOLUME_PERIOD) + 10:
            return None

        current_close = float(completed["close"].astype(float).iloc[-1])
        atr_value     = _atr_scalar(completed)
        if atr_value <= 0:
            return None

        close_series = completed["close"].astype(float)
        ema20 = float(_ema(close_series, EMA_ENTRY_FAST).iloc[-1])
        ema50 = float(_ema(close_series, EMA_ENTRY_SLOW).iloc[-1])

        # 6. Volume: latest completed candle >= avg * multiplier
        volume = completed["volume"].astype(float)
        vol_window = volume.iloc[-(VOLUME_PERIOD + 1):-1]
        if len(vol_window) < 5:
            return None
        vol_avg = float(vol_window.mean())
        latest_vol = float(volume.iloc[-1])

        if vol_avg <= 0 or latest_vol < vol_avg * VOLUME_MULTIPLIER:
            logger.info(
                "[ARMED-REJECT] %s | volume %.0f < required %.0f (%.1fx avg)",
                symbol, latest_vol, vol_avg * VOLUME_MULTIPLIER, VOLUME_MULTIPLIER,
            )
            return None

        vol_ratio = latest_vol / vol_avg if vol_avg > 0 else 0.0

        # 7. Pullback check — price near EMA20/EMA50/S&R
        sr_candles = completed.tail(SR_LOOKBACK_CANDLES)
        swings = _find_swings(sr_candles)

        level_name, level_price, dist_to_level = _pullback_info(
            direction, current_close, ema20, ema50, atr_value, swings
        )

        if level_name is None:
            logger.info(
                "[ARMED-REJECT] %s %s | no pullback level near current close=%.6g"
                " ema20=%.6g ema50=%.6g atr=%.6g",
                symbol, direction, current_close, ema20, ema50, atr_value,
            )
            return None

        # 8. Entry zone
        zone_half  = atr_value * ENTRY_ZONE_ATR_MULTIPLIER
        entry_low  = level_price - zone_half
        entry_high = level_price + zone_half
        trigger_price = level_price

        # 9. SL
        if direction == "LONG":
            swing_lows = sorted([s["price"] for s in swings if s["type"] == "LOW"])
            if swing_lows:
                sl_price = swing_lows[0] - atr_value * 0.3
            else:
                sl_price = entry_low - atr_value * SL_ATR_MULTIPLIER
            sl_price = min(sl_price, entry_low - atr_value * 0.4)
        else:
            swing_highs = sorted([s["price"] for s in swings if s["type"] == "HIGH"], reverse=True)
            if swing_highs:
                sl_price = swing_highs[0] + atr_value * 0.3
            else:
                sl_price = entry_high + atr_value * SL_ATR_MULTIPLIER
            sl_price = max(sl_price, entry_high + atr_value * 0.4)

        if sl_price <= 0:
            return None

        # 10. TP
        if direction == "LONG":
            risk = trigger_price - sl_price
        else:
            risk = sl_price - trigger_price

        if risk <= 0:
            return None

        rr = min(MAX_RR, MIN_RR)  # start at MIN_RR
        if direction == "LONG":
            tp_price = trigger_price + risk * rr
        else:
            tp_price = trigger_price - risk * rr

        if not _valid_trade_geometry(direction, trigger_price, tp_price, sl_price):
            logger.info(
                "[ARMED-REJECT] %s %s | invalid geometry entry=%.6g tp=%.6g sl=%.6g",
                symbol, direction, trigger_price, tp_price, sl_price,
            )
            return None

        # 11. Score
        score = _score_setup(level_name, dist_to_level, atr_value, vol_ratio, rr)

        if score < MIN_SETUP_SCORE:
            logger.info(
                "[ARMED-REJECT] %s %s | score %.1f < min %g",
                symbol, direction, score, MIN_SETUP_SCORE,
            )
            return None

        setup_reason  = f"Pullback near {level_name} | vol {vol_ratio:.1f}x avg"
        trend_summary = f"1D/4H bullish | 1H > EMA50" if direction == "LONG" \
            else f"1D/4H bearish | 1H < EMA50"

        now        = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=ARMED_SETUP_EXPIRE_MINUTES)

        logger.info(
            "[ARMED] %s %s | level=%s trigger=%.6g zone=%.6g-%.6g"
            " SL=%.6g TP=%.6g RR=%.2f score=%.1f vol=%.1fx",
            direction, symbol,
            level_name, trigger_price, entry_low, entry_high,
            sl_price, tp_price, rr, score, vol_ratio,
        )

        return {
            "symbol":        symbol,
            "direction":     direction,
            "trigger_price": round(trigger_price, 8),
            "entry_low":     round(entry_low,     8),
            "entry_high":    round(entry_high,    8),
            "sl_price":      round(sl_price,      8),
            "tp_price":      round(tp_price,      8),
            "rr":            round(rr,            2),
            "score":         score,
            "setup_reason":  setup_reason,
            "trend_summary": trend_summary,
            "expires_at":    expires_at.isoformat(),
        }

    except Exception as e:
        logger.error("Error detecting armed setup for %s: %s", symbol, e, exc_info=True)
        return None


# ── Public: calculate signal from armed setup + live price ────────

def calculate_signal_from_setup(setup: dict, live_price: float) -> Signal | None:
    try:
        direction  = setup["direction"]
        sl_price   = float(setup["sl_price"])
        entry_low  = float(setup["entry_low"])
        entry_high = float(setup["entry_high"])
        score      = float(setup.get("score", 0.0))
        trend_summary = setup.get("trend_summary", "")

        entry = live_price

        if direction == "LONG":
            risk = entry - sl_price
        else:
            risk = sl_price - entry

        if risk <= 0:
            logger.warning(
                "[SIGNAL-REJECT] %s %s | risk <= 0 entry=%.6g sl=%.6g",
                setup.get("symbol"), direction, entry, sl_price,
            )
            return None

        rr = min(MAX_RR, MIN_RR)
        if direction == "LONG":
            tp_price = entry + risk * rr
        else:
            tp_price = entry - risk * rr

        if not _valid_trade_geometry(direction, entry, tp_price, sl_price):
            logger.warning(
                "[SIGNAL-REJECT] %s %s | invalid geometry entry=%.6g tp=%.6g sl=%.6g",
                setup.get("symbol"), direction, entry, tp_price, sl_price,
            )
            return None

        if direction == "LONG":
            tp_roi_pct = (tp_price - entry) / entry * 100.0 * LEVERAGE
            sl_roi_pct = (entry - sl_price) / entry * 100.0 * LEVERAGE
        else:
            tp_roi_pct = (entry - tp_price) / entry * 100.0 * LEVERAGE
            sl_roi_pct = (sl_price - entry) / entry * 100.0 * LEVERAGE

        return Signal(
            symbol=setup["symbol"],
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp_price, 8),
            sl_price=round(sl_price, 8),
            leverage=LEVERAGE,
            tp_roi_pct=round(tp_roi_pct, 1),
            sl_roi_pct=round(sl_roi_pct, 1),
            timeframe_summary=trend_summary,
            generated_at=datetime.now(timezone.utc),
            score=score,
            entry_low=round(entry_low, 8),
            entry_high=round(entry_high, 8),
            rr=round(rr, 2),
        )

    except Exception as e:
        logger.error(
            "Error calculating signal from setup %s: %s",
            setup.get("symbol"), e, exc_info=True,
        )
        return None
