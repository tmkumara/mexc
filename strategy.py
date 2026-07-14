"""
Liquidation-Aware 1m Scalp strategy (v14).

Two-phase workflow, persisted via the `armed_setups` DB table (same table
the prior VP-OB strategy used -- schema unchanged):

Phase 1 (arm): on each pooled coin, compute the 1m EMA(9/21/50) stack +
rolling VWAP side + RSI zone + volume confirmation ("base signal"). If a
base signal fires, evaluate the liquidation-cluster filter immediately; if
it already clears, arm the setup with real levels. If it doesn't yet clear
(no magnet cluster ahead, magnet too close, a larger opposing pool behind
entry, funding extreme against direction, or no clean stop placement), arm
the setup anyway with provisional levels so the next few 1m closes can be
re-checked without re-deriving the base signal from scratch.

Phase 2 (monitor): each 1m close, re-run the base signal and (if still
active) the liquidity filter against the armed setup's direction using the
latest price/cluster state. Fires a Signal the moment the filter passes,
invalidates the setup once the base signal itself drops, and expires it
after SCALP_ARM_MAX_AGE_BARS minutes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import database as db
from liq_estimator import LiqEstimator
from mexc_client import get_klines
from typing import Callable

import nw_kernel
from config import (
    BASE_SIGNAL,
    SCALP_TF,
    SCALP_KLINE_COUNT,
    NW_KLINE_COUNT,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_PERIOD,
    RSI_LONG_MIN,
    RSI_LONG_MAX,
    RSI_SHORT_MIN,
    RSI_SHORT_MAX,
    SCALP_VOLUME_MIN_MULT,
    SCALP_VOLUME_MA_BARS,
    NW_H,
    NW_R,
    NW_LAG,
    NW_SMOOTH,
    EMA_RIBBON_FAST,
    EMA_RIBBON_MID,
    EMA_RIBBON_SLOW,
    EMA_RIBBON_TREND,
    TARGET_MARGIN_PROFIT,
    MIN_RR,
    MAX_SL_PRICE_PCT,
    LEVERAGE_TIERS,
    MMR_BUFFER,
    BUCKET_PCT,
    CLUSTER_DECAY,
    CLUSTER_LOOKAROUND,
    CLUSTER_MIN_PERCENTILE,
    FUNDING_EXTREME,
    SCALP_ARM_MAX_AGE_BARS,
    LEVERAGE,
)

logger = logging.getLogger(__name__)

TP_PRICE_PCT = TARGET_MARGIN_PROFIT / LEVERAGE

# One LiqEstimator + latest ticker snapshot per symbol, fed by main.py's OI poll loop.
_estimators: dict[str, LiqEstimator] = {}
_ticker_cache: dict[str, dict] = {}


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


def get_estimator(symbol: str) -> LiqEstimator:
    est = _estimators.get(symbol)
    if est is None:
        est = LiqEstimator(
            leverage_tiers=LEVERAGE_TIERS,
            mmr_buffer=MMR_BUFFER,
            bucket_pct=BUCKET_PCT,
            decay=CLUSTER_DECAY,
            lookaround_pct=CLUSTER_LOOKAROUND,
            min_percentile=CLUSTER_MIN_PERCENTILE,
            account_leverage=LEVERAGE,
        )
        _estimators[symbol] = est
    return est


def update_ticker_cache(symbol: str, fair_price: float, funding_rate: float) -> None:
    _ticker_cache[symbol] = {"fair_price": fair_price, "funding_rate": funding_rate}


def _latest_funding(symbol: str) -> float:
    return _ticker_cache.get(symbol, {}).get("funding_rate", 0.0)


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


# ── indicators ──────────────────────────────────────────────────────

def _ema(values: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi(closes: np.ndarray, n: int) -> np.ndarray:
    diffs = np.diff(closes)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = np.empty(len(diffs))
    avg_loss = np.empty(len(diffs))
    avg_gain[0] = gains[:n].mean()
    avg_loss[0] = losses[:n].mean()
    for i in range(1, len(diffs)):
        avg_gain[i] = (avg_gain[i - 1] * (n - 1) + gains[i]) / n
        avg_loss[i] = (avg_loss[i - 1] * (n - 1) + losses[i]) / n
    rs = avg_gain / np.where(avg_loss == 0, 1e-12, avg_loss)
    return np.concatenate(([50.0], 100 - 100 / (1 + rs)))


def _rolling_vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    typical = (high + low + close) / 3.0
    return np.cumsum(typical * volume) / np.maximum(np.cumsum(volume), 1e-12)


def _base_signal_ema_confluence(df: pd.DataFrame) -> str | None:
    """EMA 9/21/50 stack + rolling VWAP side + RSI zone + volume confirmation."""
    close = df["close"].astype(float).to_numpy()
    if len(close) < EMA_SLOW + 5:
        return None
    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    volume = df["volume"].astype(float).to_numpy()

    e_fast = _ema(close, EMA_FAST)[-1]
    e_mid = _ema(close, EMA_MID)[-1]
    e_slow = _ema(close, EMA_SLOW)[-1]
    r = _rsi(close, RSI_PERIOD)[-1]
    vwap = _rolling_vwap(high, low, close, volume)[-1]
    vol_ok = volume[-1] > SCALP_VOLUME_MIN_MULT * volume[-(SCALP_VOLUME_MA_BARS + 1): -1].mean()
    price = close[-1]

    if e_fast > e_mid > e_slow and price > vwap and RSI_LONG_MIN < r < RSI_LONG_MAX and vol_ok:
        return "LONG"
    if e_fast < e_mid < e_slow and price < vwap and RSI_SHORT_MIN < r < RSI_SHORT_MAX and vol_ok:
        return "SHORT"
    return None


def _base_signal_nw_ribbon(df: pd.DataFrame) -> str | None:
    """Nadaraya-Watson kernel slope-turn, gated by the EMA 20/50/100/200 ribbon."""
    closes = df["close"].astype(float).to_numpy()
    direction = nw_kernel.base_signal_nw(
        closes, h=NW_H, r=NW_R, lag=NW_LAG, smooth=NW_SMOOTH,
        fast=EMA_RIBBON_FAST, mid=EMA_RIBBON_MID, slow=EMA_RIBBON_SLOW, trend=EMA_RIBBON_TREND,
    )
    return direction.upper() if direction else None


_BASE_SIGNAL_FNS: dict[str, Callable[[pd.DataFrame], str | None]] = {
    "ema_confluence": _base_signal_ema_confluence,
    "nw_ribbon": _base_signal_nw_ribbon,
}
_base_signal = _BASE_SIGNAL_FNS[BASE_SIGNAL]
_KLINE_COUNT = SCALP_KLINE_COUNT if BASE_SIGNAL == "ema_confluence" else NW_KLINE_COUNT
_MIN_BARS = (EMA_SLOW + 6) if BASE_SIGNAL == "ema_confluence" else (EMA_RIBBON_TREND + 6)


# ── liquidity filter ─────────────────────────────────────────────────

def _stop_below(price: float, clusters: list[tuple[float, str, float]]) -> float | None:
    max_sl = price * (1 - MAX_SL_PRICE_PCT)
    blockers = [c[0] for c in clusters if max_sl <= c[0] < price]
    sl = (min(blockers) * 0.9985) if blockers else max_sl
    return sl if sl >= price * (1 - MAX_SL_PRICE_PCT * 1.5) else None


def _stop_above(price: float, clusters: list[tuple[float, str, float]]) -> float | None:
    max_sl = price * (1 + MAX_SL_PRICE_PCT)
    blockers = [c[0] for c in clusters if price < c[0] <= max_sl]
    sl = (max(blockers) * 1.0015) if blockers else max_sl
    return sl if sl <= price * (1 + MAX_SL_PRICE_PCT * 1.5) else None


def _evaluate_liquidity(
    direction: str, price: float, funding: float, estimator: LiqEstimator,
) -> tuple[bool, float | None, float | None, str]:
    """Returns (ok, tp, sl, reason)."""
    clusters = estimator.significant_clusters(price)
    tp_dist = price * TP_PRICE_PCT
    magnet = None

    if direction == "LONG":
        above = [c for c in clusters if c[0] > price and c[1] == "short"]
        if not above:
            return False, None, None, "no short-liq cluster above (no magnet)"
        magnet = min(above, key=lambda c: c[0])
        if magnet[0] < price * (1 + TP_PRICE_PCT * 0.6):
            return False, None, None, "magnet too close - move likely exhausted"
        tp = min(magnet[0] * 0.999, price + tp_dist)
        danger = estimator.magnitude_between(price * 0.997, price, side="long")
        pull = estimator.magnitude_between(price, magnet[0], side="short")
        if danger > pull:
            return False, None, None, "larger liq pool just below entry"
        sl = _stop_below(price, clusters)
        if funding > FUNDING_EXTREME:
            return False, None, None, "crowded longs (funding extreme) - long veto"
    else:
        below = [c for c in clusters if c[0] < price and c[1] == "long"]
        if not below:
            return False, None, None, "no long-liq cluster below (no magnet)"
        magnet = max(below, key=lambda c: c[0])
        if magnet[0] > price * (1 - TP_PRICE_PCT * 0.6):
            return False, None, None, "magnet too close - move likely exhausted"
        tp = max(magnet[0] * 1.001, price - tp_dist)
        danger = estimator.magnitude_between(price, price * 1.003, side="short")
        pull = estimator.magnitude_between(magnet[0], price, side="long")
        if danger > pull:
            return False, None, None, "larger liq pool just above entry"
        sl = _stop_above(price, clusters)
        if funding < -FUNDING_EXTREME:
            return False, None, None, "crowded shorts (funding extreme) - short veto"

    if sl is None:
        return False, None, None, "no clean stop placement (dense cluster in the way)"
    if not _valid_trade_geometry(direction, price, tp, sl):
        return False, None, None, "invalid trade geometry"

    rr = abs(tp - price) / abs(price - sl)
    if rr < MIN_RR:
        return False, None, None, f"RR {rr:.2f} below minimum {MIN_RR}"

    return True, tp, sl, f"RR {rr:.2f}, magnet at {magnet[0]:.6g}, funding {funding * 100:.4f}%"


def _roi(direction: str, entry: float, tp: float, sl: float) -> tuple[float, float, float]:
    if direction == "LONG":
        risk_pct = (entry - sl) / entry * 100.0
        reward_pct = (tp - entry) / entry * 100.0
    else:
        risk_pct = (sl - entry) / entry * 100.0
        reward_pct = (entry - tp) / entry * 100.0
    rr = reward_pct / risk_pct if risk_pct > 0 else 0.0
    return round(reward_pct * LEVERAGE, 1), round(risk_pct * LEVERAGE, 1), round(rr, 2)


def _score_from_rr(rr: float) -> float:
    return round(min(100.0, max(0.0, rr) * 20.0), 1)


# ── Phase 1: arm ──────────────────────────────────────────────────────

def _try_arm_setup(symbol: str) -> None:
    if db.armed_setup_exists(symbol):
        return

    df = get_klines(symbol, SCALP_TF, count=_KLINE_COUNT)
    if df is None or df.empty or len(df) < _MIN_BARS:
        return
    window = df.iloc[:-1]   # last CLOSED 1m bar only

    direction = _base_signal(window)
    if direction is None:
        return

    price = float(window["close"].iloc[-1])
    funding = _latest_funding(symbol)
    estimator = get_estimator(symbol)
    ok, tp, sl, reason = _evaluate_liquidity(direction, price, funding, estimator)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=SCALP_ARM_MAX_AGE_BARS)

    if ok:
        tp_roi, sl_roi, rr = _roi(direction, price, tp, sl)
        db.save_armed_setup({
            "symbol": symbol,
            "direction": direction,
            "trigger_price": price,
            "entry_low": price,
            "entry_high": price,
            "sl_price": sl,
            "tp_price": tp,
            "rr": rr,
            "score": _score_from_rr(rr),
            "setup_reason": reason,
            "trend_summary": f"1m {BASE_SIGNAL} + liq filter passed | funding {funding * 100:.4f}%",
            "expires_at": expires_at.isoformat(),
        })
        logger.info("[SCALP-ARM] %s %s price=%.6g tp=%.6g sl=%.6g rr=%.2f (%s)",
                    symbol, direction, price, tp, sl, rr, reason)
        return

    # Base signal fired but the liquidity filter hasn't cleared yet -- arm
    # with provisional levels so main.py's monitor pass keeps re-checking
    # this symbol every cycle instead of re-deriving the base signal.
    provisional_sl = price * (1 - MAX_SL_PRICE_PCT) if direction == "LONG" else price * (1 + MAX_SL_PRICE_PCT)
    provisional_tp = price * (1 + TP_PRICE_PCT) if direction == "LONG" else price * (1 - TP_PRICE_PCT)
    provisional_rr = TP_PRICE_PCT / MAX_SL_PRICE_PCT
    db.save_armed_setup({
        "symbol": symbol,
        "direction": direction,
        "trigger_price": price,
        "entry_low": price,
        "entry_high": price,
        "sl_price": provisional_sl,
        "tp_price": provisional_tp,
        "rr": round(provisional_rr, 2),
        "score": 0.0,
        "setup_reason": reason,
        "trend_summary": f"1m {BASE_SIGNAL} (awaiting liq filter) | funding {funding * 100:.4f}%",
        "expires_at": expires_at.isoformat(),
    })
    logger.info("[SCALP-WAIT] %s %s price=%.6g veto=%s", symbol, direction, price, reason)


# ── Phase 2: monitor ──────────────────────────────────────────────────

def _monitor_setup(symbol: str, setup: dict) -> Signal | None:
    direction = setup["direction"]

    expires_at = datetime.fromisoformat(setup["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        db.mark_armed_setup_expired(setup["id"])
        logger.info("[SCALP-EXPIRE] %s setup #%s aged out", symbol, setup["id"])
        return None

    df = get_klines(symbol, SCALP_TF, count=_KLINE_COUNT)
    if df is None or df.empty or len(df) < _MIN_BARS:
        return None
    window = df.iloc[:-1]

    if _base_signal(window) != direction:
        db.mark_armed_setup_invalidated(setup["id"], "base signal no longer active")
        logger.info("[SCALP-INVALIDATE] %s %s base signal dropped", symbol, direction)
        return None

    price = float(window["close"].iloc[-1])
    funding = _latest_funding(symbol)
    estimator = get_estimator(symbol)
    ok, tp, sl, reason = _evaluate_liquidity(direction, price, funding, estimator)
    if not ok:
        logger.debug("[SCALP-WAIT] %s %s still vetoed: %s", symbol, direction, reason)
        return None

    tp_roi, sl_roi, rr = _roi(direction, price, tp, sl)

    logger.info("[SCALP-SIGNAL] %s %s entry=%.6g tp=%.6g sl=%.6g rr=%.2f (%s)",
                symbol, direction, price, tp, sl, rr, reason)

    return Signal(
        symbol=symbol,
        direction=direction,
        entry_price=round(price, 8),
        tp_price=round(tp, 8),
        sl_price=round(sl, 8),
        leverage=LEVERAGE,
        tp_roi_pct=tp_roi,
        sl_roi_pct=sl_roi,
        timeframe_summary=f"1m {BASE_SIGNAL} liq-scalp | {reason}",
        generated_at=datetime.now(timezone.utc),
        rr=rr,
        score=_score_from_rr(rr),
        entry_low=price,
        entry_high=price,
        armed_setup_id=setup["id"],
    )


# ── Public: scan one symbol ───────────────────────────────────────

def monitor_symbol(symbol: str) -> Signal | None:
    """
    Check only an already-armed setup for this symbol. Does not arm a new
    setup. Safe to call every cycle regardless of any firing-budget
    throttle in the caller.
    """
    try:
        existing = db.get_armed_setup_by_symbol(symbol)
        if existing is not None:
            return _monitor_setup(symbol, existing)
        return None
    except Exception as e:
        logger.error("Error monitoring %s: %s", symbol, e, exc_info=True)
        return None


def arm_symbol(symbol: str) -> None:
    """
    Try to arm a new setup for this symbol if none is currently armed. Does
    not check for or fire retests -- call monitor_symbol for that.
    """
    try:
        if db.get_armed_setup_by_symbol(symbol) is None:
            _try_arm_setup(symbol)
    except Exception as e:
        logger.error("Error arming %s: %s", symbol, e, exc_info=True)
