"""
VP-OB Confluence strategy.

Two-phase workflow, persisted via the `armed_setups` DB table:

Phase 1 (arm): compute a 4H Volume Profile bias (close above VAH -> LONG,
below VAL -> SHORT), detect 1H Order Blocks (BOS/CHoCH + displacement) in
that direction, keep only ones near a VP level (POC/VAH/VAL/HVN), and arm
the best one if it clears a minimum confluence score.

Phase 2 (monitor): each cycle, check any already-armed setup for structural
invalidation, expiry, or a valid retest (wick into the zone, close back out
in the trade direction, body ratio + volume confirmed). A valid retest
recomputes SL/TP against fresh VP levels and applies the RR/ROI gates
before firing a Signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import database as db
from mexc_client import get_klines
from order_blocks import OrderBlock, detect_bos_choch, find_order_blocks, find_swings
from volume_profile import VolumeProfile, compute_volume_profile, next_target, vp_bias
from config import (
    VP_TF,
    VP_KLINE_COUNT,
    VP_LOOKBACK_BARS,
    VP_BINS,
    VP_VALUE_AREA_PCT,
    VP_HVN_MULT,
    VP_LVN_MULT,
    OB_TF,
    OB_KLINE_COUNT,
    OB_SWING_LENGTH,
    OB_DISPLACEMENT_ATR_MULT,
    OB_MAX_AGE_BARS,
    OB_CONFLUENCE_ATR_MULT,
    OB_BODY_RATIO_MIN,
    OB_VOLUME_MIN_MULT,
    OB_VOLUME_MA_BARS,
    ATR_PERIOD,
    SL_ATR_BUFFER_MULT,
    DYN_EMA_MAX_LENGTH,
    DYN_EMA_ACCEL_MULT,
    LEVERAGE,
    MIN_STRUCTURE_RR,
    MIN_TP_ROI_PCT,
    MAX_SL_ROI_PCT,
    MIN_SETUP_SCORE,
    BTC_SYMBOL,
    BTC_TF,
    BTC_KLINE_COUNT,
    BTC_GATE_ENABLED,
    BTC_RANGING_PCT,
)

logger = logging.getLogger(__name__)

# BTC DynEMA cache — refreshed once per scan cycle (14-min TTL)
_btc_cache: dict = {"dema": None, "close": None, "ts": 0.0}
_BTC_CACHE_TTL = 14 * 60


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
    rr: float = 0.0
    score: float = 0.0
    entry_low: float = 0.0
    entry_high: float = 0.0
    armed_setup_id: int | None = None


# ── Low-level indicator helpers (kept from the previous strategy) ──

def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    high       = df["high"].astype(float)
    low        = df["low"].astype(float)
    prev_close = df["close"].astype(float).shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _compute_dyn_ema(
    close: pd.Series,
    max_length: int = 50,
    accel_mult: float = 5.0,
) -> pd.Series:
    """Dynamic EMA with accelerating alpha — used only by the BTC macro gate."""
    c = close.to_numpy(dtype=float)
    n = len(c)

    abs_c = np.abs(c)
    max_abs = np.array([
        np.nanmax(abs_c[max(0, i - 199): i + 1]) for i in range(n)
    ], dtype=float)
    max_abs[max_abs == 0] = 1e-10

    counts_diff_norm = (c + max_abs) / (2.0 * max_abs)
    dyn_length = 5.0 + counts_diff_norm * (max_length - 5)

    delta = np.abs(np.diff(c, prepend=c[0]))
    max_delta = np.array([
        np.nanmax(delta[max(0, i - 199): i + 1]) for i in range(n)
    ], dtype=float)
    max_delta[max_delta == 0] = 1.0
    accel_factor = delta / max_delta

    alpha_base = 2.0 / (dyn_length + 1.0)
    alpha = np.minimum(1.0, alpha_base * (1.0 + accel_factor * accel_mult))

    dyn_ema = np.empty(n, dtype=float)
    dyn_ema[0] = c[0]
    for i in range(1, n):
        dyn_ema[i] = alpha[i] * c[i] + (1.0 - alpha[i]) * dyn_ema[i - 1]

    return pd.Series(dyn_ema, index=close.index)


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


def _get_btc_dema() -> tuple[float | None, float | None]:
    """
    Return (btc_close, btc_dema) using a 14-min module-level cache.
    Returns (None, None) on any failure — gate is fail-open.
    """
    global _btc_cache
    now_ts = datetime.now(timezone.utc).timestamp()
    if _btc_cache["dema"] is not None and now_ts - _btc_cache["ts"] < _BTC_CACHE_TTL:
        return _btc_cache["close"], _btc_cache["dema"]
    try:
        df = get_klines(BTC_SYMBOL, BTC_TF, count=BTC_KLINE_COUNT)
        if df is None or df.empty or len(df) < 210:
            logger.warning("[BTC-GATE] Insufficient BTC klines (%d) — gate open", 0 if df is None else len(df))
            return None, None
        close    = df["close"].astype(float)
        dema     = _compute_dyn_ema(close, DYN_EMA_MAX_LENGTH, DYN_EMA_ACCEL_MULT)
        btc_close = float(close.iloc[-1])
        btc_dema  = float(dema.iloc[-1])
        _btc_cache = {"dema": btc_dema, "close": btc_close, "ts": now_ts}
        logger.debug("[BTC-GATE] Cache refreshed close=%.6g dema=%.6g", btc_close, btc_dema)
        return btc_close, btc_dema
    except Exception as e:
        logger.warning("[BTC-GATE] Fetch failed: %s — gate open", e)
        return None, None


# ── Scoring ──────────────────────────────────────────────────────────

def _compute_setup_score(displacement_atr_ratio: float, confluence_distance_atr: float) -> float:
    """
    0-100 composite score. Displacement component rewards moves well beyond
    the minimum threshold (maxes out at 2x OB_DISPLACEMENT_ATR_MULT);
    confluence component rewards proximity to a VP level (maxes out at zero
    distance, zero at the max allowed confluence distance).
    """
    disp_component = min(50.0, 50.0 * displacement_atr_ratio / (2.0 * OB_DISPLACEMENT_ATR_MULT))
    conf_component = 50.0 * max(0.0, 1.0 - confluence_distance_atr / OB_CONFLUENCE_ATR_MULT)
    return round(disp_component + conf_component, 1)


# ── Phase 1: arm ──────────────────────────────────────────────────────

def _try_arm_setup(symbol: str) -> None:
    if db.armed_setup_exists(symbol):
        return

    vp_df = get_klines(symbol, VP_TF, count=VP_KLINE_COUNT)
    if vp_df is None or vp_df.empty or len(vp_df) < VP_LOOKBACK_BARS + 1:
        return
    vp_window = vp_df.iloc[:-1].tail(VP_LOOKBACK_BARS)

    vp = compute_volume_profile(
        vp_window, bins=VP_BINS, value_area_pct=VP_VALUE_AREA_PCT,
        hvn_mult=VP_HVN_MULT, lvn_mult=VP_LVN_MULT,
    )
    if vp is None:
        return

    bias = vp_bias(float(vp_window["close"].iloc[-1]), vp)
    if bias is None:
        return

    ob_df = get_klines(symbol, OB_TF, count=OB_KLINE_COUNT)
    min_ob_bars = ATR_PERIOD + OB_SWING_LENGTH * 2 + 10
    if ob_df is None or ob_df.empty or len(ob_df) < min_ob_bars:
        return
    ob_window = ob_df.iloc[:-1].reset_index(drop=True)

    atr = _atr_series(ob_window, ATR_PERIOD)
    swings = find_swings(ob_window, length=OB_SWING_LENGTH)
    events = detect_bos_choch(ob_window, swings)
    obs = find_order_blocks(ob_window, events, atr, displacement_atr_mult=OB_DISPLACEMENT_ATR_MULT)

    matching = [ob for ob in obs if ob.direction == bias]
    if not matching:
        return

    latest_atr = float(atr.iloc[-1])
    if latest_atr <= 0:
        return

    vp_levels = [vp.poc, vp.vah, vp.val] + vp.hvns
    best_ob: OrderBlock | None = None
    best_distance_atr: float | None = None
    for ob in matching:
        mid = (ob.low + ob.high) / 2.0
        distance_atr = min(abs(mid - level) for level in vp_levels) / latest_atr
        if distance_atr > OB_CONFLUENCE_ATR_MULT:
            continue
        if best_distance_atr is None or distance_atr < best_distance_atr:
            best_ob = ob
            best_distance_atr = distance_atr

    if best_ob is None:
        return

    displacement_move = abs(
        float(ob_window["close"].iloc[best_ob.event_bar_index])
        - float(ob_window["close"].iloc[best_ob.formed_at_bar])
    )
    displacement_atr_ratio = displacement_move / latest_atr

    score = _compute_setup_score(displacement_atr_ratio, best_distance_atr)
    if score < MIN_SETUP_SCORE:
        logger.info(
            "[OB-REJECT] %s %s score %.1f below min %.1f (disp=%.2fx conf=%.2fx)",
            symbol, best_ob.direction, score, MIN_SETUP_SCORE, displacement_atr_ratio, best_distance_atr,
        )
        return

    buffer = latest_atr * SL_ATR_BUFFER_MULT
    provisional_entry = (best_ob.low + best_ob.high) / 2.0
    if best_ob.direction == "LONG":
        provisional_sl = best_ob.low - buffer
    else:
        provisional_sl = best_ob.high + buffer
    provisional_tp = next_target(best_ob.direction, provisional_entry, vp)

    risk = abs(provisional_entry - provisional_sl)
    rr = abs(provisional_tp - provisional_entry) / risk if risk > 0 else 0.0

    age_bars = (len(ob_window) - 1) - best_ob.formed_at_bar
    expires_in_bars = max(OB_MAX_AGE_BARS - age_bars, 1)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_bars)

    reason = f"{best_ob.structure_event} disp={displacement_atr_ratio:.2f}xATR conf={best_distance_atr:.2f}xATR"
    db.save_armed_setup({
        "symbol": symbol,
        "direction": best_ob.direction,
        "trigger_price": provisional_entry,
        "entry_low": best_ob.low,
        "entry_high": best_ob.high,
        "sl_price": provisional_sl,
        "tp_price": provisional_tp,
        "rr": round(rr, 2),
        "score": score,
        "setup_reason": reason,
        "trend_summary": f"VP({VP_TF}) POC={vp.poc:.6g} VAH={vp.vah:.6g} VAL={vp.val:.6g}",
        "expires_at": expires_at.isoformat(),
    })
    logger.info(
        "[OB-ARM] %s %s zone=[%.6g,%.6g] score=%.1f reason=%s",
        symbol, best_ob.direction, best_ob.low, best_ob.high, score, reason,
    )


# ── Phase 2: monitor ──────────────────────────────────────────────────

def _monitor_setup(symbol: str, setup: dict) -> Signal | None:
    direction = setup["direction"]
    ob_low = setup["entry_low"]
    ob_high = setup["entry_high"]

    ob_df = get_klines(symbol, OB_TF, count=OB_KLINE_COUNT)
    if ob_df is None or ob_df.empty or len(ob_df) < 2:
        return None
    ob_window = ob_df.iloc[:-1].reset_index(drop=True)
    if ob_window.empty:
        return None

    last = ob_window.iloc[-1]
    last_open  = float(last["open"])
    last_high  = float(last["high"])
    last_low   = float(last["low"])
    last_close = float(last["close"])
    last_vol   = float(last["volume"])

    mid = (ob_low + ob_high) / 2.0

    if direction == "LONG" and last_close < mid:
        db.mark_armed_setup_invalidated(setup["id"], f"close {last_close:.6g} < midpoint {mid:.6g}")
        logger.info("[OB-INVALIDATE] %s LONG closed below midpoint", symbol)
        return None
    if direction == "SHORT" and last_close > mid:
        db.mark_armed_setup_invalidated(setup["id"], f"close {last_close:.6g} > midpoint {mid:.6g}")
        logger.info("[OB-INVALIDATE] %s SHORT closed above midpoint", symbol)
        return None

    expires_at = datetime.fromisoformat(setup["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        db.mark_armed_setup_expired(setup["id"])
        logger.info("[OB-EXPIRE] %s setup #%s aged out", symbol, setup["id"])
        return None

    touched = (last_low <= ob_high) and (last_high >= ob_low)
    if not touched:
        return None

    confirmed = last_close > ob_high if direction == "LONG" else last_close < ob_low
    if not confirmed:
        return None

    candle_range = last_high - last_low
    if candle_range <= 0:
        return None
    body_ratio = abs(last_close - last_open) / candle_range
    if body_ratio < OB_BODY_RATIO_MIN:
        db.mark_armed_setup_missed(setup["id"], f"body_ratio {body_ratio:.2f} below {OB_BODY_RATIO_MIN}")
        logger.info("[OB-MISS] %s retest body ratio %.2f below min", symbol, body_ratio)
        return None
    if direction == "LONG" and last_close < last_open:
        db.mark_armed_setup_missed(setup["id"], "retest candle not bullish")
        return None
    if direction == "SHORT" and last_close > last_open:
        db.mark_armed_setup_missed(setup["id"], "retest candle not bearish")
        return None

    volume_ma = float(ob_window["volume"].astype(float).tail(OB_VOLUME_MA_BARS).mean())
    if volume_ma <= 0 or last_vol < OB_VOLUME_MIN_MULT * volume_ma:
        db.mark_armed_setup_missed(setup["id"], f"volume {last_vol:.0f} below {OB_VOLUME_MIN_MULT}x MA {volume_ma:.0f}")
        logger.info("[OB-MISS] %s retest volume below threshold", symbol)
        return None

    if BTC_GATE_ENABLED:
        btc_close, btc_dema = _get_btc_dema()
        if btc_close is not None and btc_dema is not None:
            ranging_margin = btc_dema * (BTC_RANGING_PCT / 100.0)
            btc_bullish = btc_close > btc_dema + ranging_margin
            btc_bearish = btc_close < btc_dema - ranging_margin
            if direction == "LONG" and btc_bearish:
                db.mark_armed_setup_missed(setup["id"], "BTC macro gate: bearish")
                logger.info("[OB-MISS] %s LONG blocked by BTC macro gate", symbol)
                return None
            if direction == "SHORT" and btc_bullish:
                db.mark_armed_setup_missed(setup["id"], "BTC macro gate: bullish")
                logger.info("[OB-MISS] %s SHORT blocked by BTC macro gate", symbol)
                return None

    entry = last_close
    atr = float(_atr_series(ob_window, ATR_PERIOD).iloc[-1])
    if atr <= 0:
        db.mark_armed_setup_missed(setup["id"], "non-positive ATR at retest")
        return None
    buffer = atr * SL_ATR_BUFFER_MULT

    vp_df = get_klines(symbol, VP_TF, count=VP_KLINE_COUNT)
    if vp_df is None or vp_df.empty or len(vp_df) < VP_LOOKBACK_BARS + 1:
        db.mark_armed_setup_missed(setup["id"], "VP refetch failed")
        return None
    vp_window = vp_df.iloc[:-1].tail(VP_LOOKBACK_BARS)
    vp = compute_volume_profile(
        vp_window, bins=VP_BINS, value_area_pct=VP_VALUE_AREA_PCT,
        hvn_mult=VP_HVN_MULT, lvn_mult=VP_LVN_MULT,
    )
    if vp is None:
        db.mark_armed_setup_missed(setup["id"], "VP recompute degenerate")
        return None

    if direction == "LONG":
        sl_price = ob_low - buffer
        tp_price = next_target("LONG", entry, vp)
    else:
        sl_price = ob_high + buffer
        tp_price = next_target("SHORT", entry, vp)

    if not _valid_trade_geometry(direction, entry, tp_price, sl_price):
        db.mark_armed_setup_missed(setup["id"], "invalid trade geometry at retest")
        logger.info("[OB-MISS] %s invalid geometry at retest", symbol)
        return None

    if direction == "LONG":
        risk_pct = (entry - sl_price) / entry * 100.0
        reward_pct = (tp_price - entry) / entry * 100.0
    else:
        risk_pct = (sl_price - entry) / entry * 100.0
        reward_pct = (entry - tp_price) / entry * 100.0

    if risk_pct <= 0 or reward_pct <= 0:
        db.mark_armed_setup_missed(setup["id"], "non-positive risk/reward at retest")
        return None

    rr = reward_pct / risk_pct
    tp_roi_pct = reward_pct * LEVERAGE
    sl_roi_pct = risk_pct * LEVERAGE

    if rr < MIN_STRUCTURE_RR:
        db.mark_armed_setup_missed(setup["id"], f"RR {rr:.2f} below min {MIN_STRUCTURE_RR}")
        logger.info("[OB-MISS] %s RR %.2f below min", symbol, rr)
        return None
    if tp_roi_pct < MIN_TP_ROI_PCT:
        db.mark_armed_setup_missed(setup["id"], f"TP ROI {tp_roi_pct:.1f} below min {MIN_TP_ROI_PCT}")
        logger.info("[OB-MISS] %s TP ROI %.1f below min", symbol, tp_roi_pct)
        return None
    if sl_roi_pct > MAX_SL_ROI_PCT:
        db.mark_armed_setup_missed(setup["id"], f"SL ROI {sl_roi_pct:.1f} above max {MAX_SL_ROI_PCT}")
        logger.info("[OB-MISS] %s SL ROI %.1f above max", symbol, sl_roi_pct)
        return None

    logger.info(
        "[SIGNAL] %s %s entry=%.6g TP=%.6g SL=%.6g RR=%.2f score=%.1f",
        symbol, direction, entry, tp_price, sl_price, rr, setup["score"],
    )

    return Signal(
        symbol=symbol,
        direction=direction,
        entry_price=round(entry, 8),
        tp_price=round(tp_price, 8),
        sl_price=round(sl_price, 8),
        leverage=LEVERAGE,
        tp_roi_pct=round(tp_roi_pct, 1),
        sl_roi_pct=round(sl_roi_pct, 1),
        timeframe_summary=f"{OB_TF.upper()} OB retest | VP({VP_TF}) bias | score {setup['score']:.0f}",
        generated_at=datetime.now(timezone.utc),
        rr=round(rr, 2),
        score=setup["score"],
        entry_low=ob_low,
        entry_high=ob_high,
        armed_setup_id=setup["id"],
    )


# ── Public: scan one symbol ───────────────────────────────────────

def scan_symbol(symbol: str) -> Signal | None:
    """
    If a setup is already armed for this symbol, monitor it for retest/
    invalidation/expiry. Otherwise, try to arm a new one. Returns a Signal
    only when an armed setup fires this cycle.
    """
    try:
        existing = db.get_armed_setup_by_symbol(symbol)
        if existing is not None:
            return _monitor_setup(symbol, existing)
        _try_arm_setup(symbol)
        return None
    except Exception as e:
        logger.error("Error scanning %s: %s", symbol, e, exc_info=True)
        return None
