"""
Nadaraya-Watson Kernel Multi-Timeframe Strategy (backtest research only).

4H trend (EMA200) gates direction; a Nadaraya-Watson rational-quadratic
kernel slope-flip on 1H (see nw_kernel.py, ported from the "Nadaraya-Watson:
Rational Quadratic Kernel (Non-Repainting)" Pine Script indicator by
jdehorty) confirms entry, alongside RSI + volume. TP is a fixed ROI-target
distance; SL is structural (swing level + ATR buffer, capped). Only
completed candles are ever used. Fully standalone from strategy.py -- see
docs/superpowers/specs/2026-07-16-nw-kernel-strategy-design.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import nw_kernel
import config_nw
from strategy import calculate_ema, calculate_rsi, calculate_atr, calculate_supertrend
from market_data import get_market_klines

logger = logging.getLogger(__name__)


@dataclass
class NwSignal:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    rr: float
    score: float
    generated_at: datetime


@dataclass
class BtcContext4h:
    close: float
    ema_200: float
    one_candle_move_pct: float
    three_candle_move_pct: float


# ── trend filter ────────────────────────────────────────────────────────

def _ema_slope_ok(ema: pd.Series, direction: str, tolerance: float = 1e-9) -> bool:
    current = ema.iloc[-1]
    three_bars_ago = ema.iloc[-4]
    if direction == "LONG":
        return current >= three_bars_ago - tolerance
    return current <= three_bars_ago + tolerance


def _detect_trend_htf(df_4h: pd.DataFrame) -> str | None:
    if len(df_4h) < config_nw.NW_TREND_EMA_PERIOD + 5:
        return None
    ema200 = calculate_ema(df_4h["close"], config_nw.NW_TREND_EMA_PERIOD)
    close = float(df_4h["close"].iloc[-1])

    if close > float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "LONG"):
        return "LONG"
    if close < float(ema200.iloc[-1]) and _ema_slope_ok(ema200, "SHORT"):
        return "SHORT"
    return None


# ── structural swing detection (used to anchor the SL in Task 5) ────────

def find_swing_low(df: pd.DataFrame, end_idx: int, lookback: int, fractal_bars: int) -> float | None:
    """Most recent confirmed fractal swing low at or before `end_idx`,
    searching back `lookback` bars. A fractal low at position p requires
    low[p] <= every low in the `fractal_bars`-bar window on each side
    (tie-inclusive), and both sides of that window must already exist
    within [0, end_idx] -- i.e. only fully-confirmed pivots count."""
    lows = df["low"].to_numpy()
    hi = end_idx - fractal_bars
    lo = max(fractal_bars, end_idx - lookback)
    for p in range(hi, lo - 1, -1):
        if p - fractal_bars < 0:
            continue
        window = lows[p - fractal_bars: p + fractal_bars + 1]
        if lows[p] <= window.min():
            return float(lows[p])
    return None


def find_swing_high(df: pd.DataFrame, end_idx: int, lookback: int, fractal_bars: int) -> float | None:
    highs = df["high"].to_numpy()
    hi = end_idx - fractal_bars
    lo = max(fractal_bars, end_idx - lookback)
    for p in range(hi, lo - 1, -1):
        if p - fractal_bars < 0:
            continue
        window = highs[p - fractal_bars: p + fractal_bars + 1]
        if highs[p] >= window.max():
            return float(highs[p])
    return None


# ── kernel-flip entry signal + confirmation ──────────────────────────────

def _detect_nw_signal(df_1h: pd.DataFrame, direction: str) -> tuple[bool, str, dict]:
    close_arr = df_1h["close"].to_numpy(dtype=float)
    yhat = nw_kernel.nw_estimate(
        close_arr, config_nw.NW_LOOKBACK_WINDOW, config_nw.NW_RELATIVE_WEIGHTING,
        config_nw.NW_KERNEL_SUM_WINDOW,
    )
    flips = nw_kernel.detect_slope_flip(yhat)

    expected_flip = "bullish" if direction == "LONG" else "bearish"
    # The flip itself doesn't have to be the exact latest bar -- it just has
    # to have happened within the last NW_CONFIRMATION_LOOKBACK_BARS candles
    # (a genuine kernel turn is a rare, single-bar event; requiring it to be
    # today rejected ~91% of all evaluations empirically). The latest bar
    # must still be holding that direction, though -- if the kernel already
    # reversed again since the flip, this isn't a live setup anymore.
    recent_flips = flips.iloc[-config_nw.NW_CONFIRMATION_LOOKBACK_BARS:]
    if not bool((recent_flips == expected_flip).any()):
        return False, "no kernel slope flip within confirmation lookback", {}

    still_bullish = yhat[-1] > yhat[-2]
    still_bearish = yhat[-1] < yhat[-2]
    if direction == "LONG" and not still_bullish:
        return False, "kernel reversed before confirmation held", {}
    if direction == "SHORT" and not still_bearish:
        return False, "kernel reversed before confirmation held", {}

    # Supertrend confirmation on the entry timeframe -- an additional
    # trend-agreement gate, independent of the kernel, meant to filter out
    # weaker/noisier flips (see NW_SUPERTREND_ATR_PERIOD/MULTIPLIER).
    st = calculate_supertrend(df_1h, config_nw.NW_SUPERTREND_ATR_PERIOD, config_nw.NW_SUPERTREND_MULTIPLIER)
    st_direction = int(st["supertrend_direction"].iloc[-1])
    if direction == "LONG" and st_direction != 1:
        return False, "Supertrend not bullish on latest candle", {}
    if direction == "SHORT" and st_direction != -1:
        return False, "Supertrend not bearish on latest candle", {}

    rsi = calculate_rsi(df_1h["close"], config_nw.NW_RSI_PERIOD)
    atr = calculate_atr(df_1h, config_nw.NW_ATR_PERIOD)

    rsi_last = float(rsi.iloc[-1])
    atr_last = float(atr.iloc[-1])
    close = float(df_1h["close"].iloc[-1])
    vol_last = float(df_1h["volume"].iloc[-1])
    vol_avg = float(df_1h["volume"].iloc[-(config_nw.NW_VOLUME_MA_PERIOD + 1):-1].mean())

    rsi_min, rsi_max = (
        (config_nw.NW_RSI_LONG_MIN, config_nw.NW_RSI_LONG_MAX) if direction == "LONG"
        else (config_nw.NW_RSI_SHORT_MIN, config_nw.NW_RSI_SHORT_MAX)
    )
    if not (rsi_min <= rsi_last <= rsi_max):
        return False, f"RSI {rsi_last:.1f} outside {direction.lower()} range", {}

    if vol_avg <= 0 or not (vol_last >= config_nw.NW_MIN_VOLUME_MULTIPLIER * vol_avg):
        ratio = (vol_last / vol_avg) if vol_avg else 0.0
        return False, f"volume ratio {ratio:.2f} below {config_nw.NW_MIN_VOLUME_MULTIPLIER}", {}

    end_idx = len(df_1h) - 1
    if direction == "LONG":
        swing_level = find_swing_low(
            df_1h, end_idx, config_nw.NW_SWING_LOOKBACK_BARS, config_nw.NW_SWING_FRACTAL_BARS,
        )
    else:
        swing_level = find_swing_high(
            df_1h, end_idx, config_nw.NW_SWING_LOOKBACK_BARS, config_nw.NW_SWING_FRACTAL_BARS,
        )
    if swing_level is None:
        return False, "no structural swing found for stop placement", {}

    details = {
        "close": close,
        "rsi": rsi_last,
        "atr": atr_last,
        "volume_ratio": vol_last / vol_avg if vol_avg else 0.0,
        "swing_level": swing_level,
    }
    return True, "", details


# ── trade geometry, TP/SL, RR ────────────────────────────────────────────

def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    return tp < entry < sl


def _calc_rr(entry: float, tp: float, sl: float) -> float:
    reward = abs(tp - entry)
    risk = abs(entry - sl)
    return reward / risk if risk > 0 else 0.0


def _calculate_tp_sl_nw(direction: str, entry: float, details: dict) -> tuple[float, float] | None:
    """TP is a fixed ROI-target distance. SL is structural -- the swing
    level found by _detect_nw_signal, pushed out by an ATR buffer -- with
    its price distance from entry capped (not rejected) at
    MAX_SL_PRICE_PCT. This intentionally mirrors the original live bot's
    cap-not-reject SL model rather than the fib strategy's later
    reject-if-too-wide redesign (see this plan's Global Constraints)."""
    atr_last = details["atr"]
    swing_level = details["swing_level"]

    if direction == "LONG":
        tp = entry * (1.0 + config_nw.TP_PRICE_PCT)
        raw_sl_distance = entry - (swing_level - atr_last * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER)
        if raw_sl_distance <= 0:
            return None
        sl_distance = min(raw_sl_distance, entry * config_nw.MAX_SL_PRICE_PCT)
        sl = entry - sl_distance
    else:
        tp = entry * (1.0 - config_nw.TP_PRICE_PCT)
        raw_sl_distance = (swing_level + atr_last * config_nw.NW_SL_ATR_BUFFER_MULTIPLIER) - entry
        if raw_sl_distance <= 0:
            return None
        sl_distance = min(raw_sl_distance, entry * config_nw.MAX_SL_PRICE_PCT)
        sl = entry + sl_distance

    return tp, sl


# ── BTC 4H market-safety filter ──────────────────────────────────────────

def build_btc_context_4h() -> BtcContext4h | None:
    df = get_market_klines(config_nw.NW_BTC_FILTER_SYMBOL, config_nw.NW_BTC_FILTER_TF, count=config_nw.NW_TREND_KLINE_COUNT)
    if df is None or df.empty:
        return None
    closed = df.iloc[:-1].copy()
    if len(closed) < config_nw.NW_TREND_EMA_PERIOD + 5:
        return None

    ema200 = calculate_ema(closed["close"], config_nw.NW_TREND_EMA_PERIOD)

    latest_close = float(closed["close"].iloc[-1])
    previous_close = float(closed["close"].iloc[-2])
    close_three_bars_ago = float(closed["close"].iloc[-4])

    one_candle_move_pct = (latest_close - previous_close) / previous_close * 100.0
    three_candle_move_pct = (latest_close - close_three_bars_ago) / close_three_bars_ago * 100.0

    return BtcContext4h(
        close=latest_close,
        ema_200=float(ema200.iloc[-1]),
        one_candle_move_pct=one_candle_move_pct,
        three_candle_move_pct=three_candle_move_pct,
    )


def _btc_filter_ok_4h(direction: str, btc: BtcContext4h) -> tuple[bool, str]:
    if abs(btc.one_candle_move_pct) > config_nw.NW_BTC_MAX_SINGLE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"
    if abs(btc.three_candle_move_pct) > config_nw.NW_BTC_MAX_THREE_CANDLE_MOVE_PCT:
        return False, "blocked due to extreme BTC volatility"

    if direction == "LONG":
        if not (
            btc.close > btc.ema_200
            and btc.three_candle_move_pct >= -config_nw.NW_BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bearish trend"
    else:
        if not (
            btc.close < btc.ema_200
            and btc.three_candle_move_pct <= config_nw.NW_BTC_MAX_OPPOSING_MOVE_PCT
        ):
            return False, "blocked by BTC bullish trend"

    return True, ""


# ── scoring ───────────────────────────────────────────────────────────

def _score_candidate_nw(direction: str, details: dict, rr: float) -> float:
    score = 30.0  # 4H trend + kernel-flip alignment -- already gated true/false upstream

    rsi = details["rsi"]
    ideal_lo, ideal_hi = (55.0, 65.0) if direction == "LONG" else (35.0, 45.0)
    if ideal_lo <= rsi <= ideal_hi:
        rsi_quality = 1.0
    else:
        dist = min(abs(rsi - ideal_lo), abs(rsi - ideal_hi))
        rsi_quality = max(0.0, 1.0 - dist / 15.0)
    score += 25.0 * rsi_quality

    vol_ratio = details["volume_ratio"]
    if config_nw.NW_MIN_VOLUME_MULTIPLIER < 2.0:
        vol_quality = min(1.0, max(0.0, (vol_ratio - config_nw.NW_MIN_VOLUME_MULTIPLIER) / (2.0 - config_nw.NW_MIN_VOLUME_MULTIPLIER)))
    else:
        vol_quality = 1.0 if vol_ratio >= config_nw.NW_MIN_VOLUME_MULTIPLIER else 0.0
    score += 20.0 * vol_quality

    if config_nw.MIN_RR < 2.0:
        rr_quality = min(1.0, max(0.0, (rr - config_nw.MIN_RR) / (2.0 - config_nw.MIN_RR)))
    else:
        rr_quality = 1.0 if rr >= config_nw.MIN_RR else 0.0
    score += 25.0 * rr_quality

    return round(min(100.0, max(0.0, score)), 1)


# ── public entry point ─────────────────────────────────────────────────

def _reason_bucket(reason: str) -> str:
    if "no kernel slope flip" in reason:
        return "no_flip"
    if "reversed before confirmation held" in reason:
        return "confirmation_not_held"
    if "Supertrend not" in reason:
        return "supertrend_disagree"
    if "no structural swing" in reason:
        return "no_swing"
    if reason.startswith("RSI"):
        return "rsi_out_of_band"
    if reason.startswith("volume ratio"):
        return "low_volume"
    return "nw_other"


def _bump(reject_sink: dict | None, key: str) -> None:
    if reject_sink is not None:
        reject_sink[key] = reject_sink.get(key, 0) + 1


def evaluate_symbol_nw(
    symbol: str,
    btc_context: "BtcContext4h | None" = None,
    reject_sink: dict | None = None,
) -> NwSignal | None:
    try:
        raw_4h = get_market_klines(symbol, config_nw.NW_TREND_TF, count=config_nw.NW_TREND_KLINE_COUNT)
        raw_1h = get_market_klines(symbol, config_nw.NW_ENTRY_TF, count=config_nw.NW_ENTRY_KLINE_COUNT)

        if raw_4h is None or raw_4h.empty or raw_1h is None or raw_1h.empty:
            logger.debug("[NW-REJECT] %s missing candle data", symbol)
            _bump(reject_sink, "missing_data")
            return None

        closed_4h = raw_4h.iloc[:-1].copy()
        closed_1h = raw_1h.iloc[:-1].copy()

        if len(closed_4h) < config_nw.NW_TREND_EMA_PERIOD + 5:
            _bump(reject_sink, "insufficient_history")
            return None
        min_1h_needed = config_nw.NW_SWING_LOOKBACK_BARS + config_nw.NW_SWING_FRACTAL_BARS + config_nw.NW_VOLUME_MA_PERIOD + 10
        if len(closed_1h) < min_1h_needed:
            _bump(reject_sink, "insufficient_history")
            return None

        direction = _detect_trend_htf(closed_4h)
        if direction is None:
            logger.debug("[NW-REJECT] %s no 4H trend", symbol)
            _bump(reject_sink, "no_trend")
            return None

        ok, reason, details = _detect_nw_signal(closed_1h, direction)
        if not ok:
            logger.debug("[NW-REJECT] %s %s", symbol, reason)
            _bump(reject_sink, _reason_bucket(reason))
            return None

        if config_nw.NW_ENABLE_BTC_FILTER:
            ctx = btc_context if btc_context is not None else build_btc_context_4h()
            if ctx is None:
                _bump(reject_sink, "btc_context_unavailable")
                return None
            btc_ok, btc_reason = _btc_filter_ok_4h(direction, ctx)
            if not btc_ok:
                logger.debug("[NW-REJECT] %s %s %s", symbol, direction, btc_reason)
                _bump(reject_sink, "btc_filter")
                return None

        entry = details["close"]
        tp_sl = _calculate_tp_sl_nw(direction, entry, details)
        if tp_sl is None:
            _bump(reject_sink, "invalid_structural_stop")
            return None
        tp, sl = tp_sl

        if not _valid_trade_geometry(direction, entry, tp, sl):
            _bump(reject_sink, "invalid_geometry")
            return None

        rr = _calc_rr(entry, tp, sl)
        if rr < config_nw.MIN_RR:
            _bump(reject_sink, "rr_below_min")
            return None

        score = _score_candidate_nw(direction, details, rr)

        logger.info(
            "[NW-CANDIDATE] %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
            symbol, direction, score, entry, tp, sl, rr,
        )

        return NwSignal(
            symbol=symbol,
            direction=direction,
            entry_price=round(entry, 8),
            tp_price=round(tp, 8),
            sl_price=round(sl, 8),
            leverage=config_nw.LEVERAGE,
            rr=round(rr, 2),
            score=score,
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as e:
        logger.error("[NW-EVAL-ERROR] %s: %s", symbol, e, exc_info=True)
        _bump(reject_sink, "error")
        return None
