"""
Phase 3 Strategy — VWAP Liquidity Sweep Scalper.

Model:
    1. Use 1h EMA bias as a direction filter.
    2. On 5m, find a liquidity sweep of a recent high/low.
    3. Confirm that price rejects the sweep and reclaims VWAP/EMA direction.
    4. Save the setup as pending.
    5. Fire only when the next confirmation candle appears.

This strategy is designed to be less rare than strict AMD/FVG and less chasey
than pure momentum. It does not guarantee profit.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd

from mexc_client import get_klines
from config import (
    TREND_TF,
    ENTRY_TF,
    TREND_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    MONITOR_KLINE_COUNT,
    EMA_FAST_PERIOD,
    EMA_SLOW_PERIOD,
    TREND_SLOPE_LOOKBACK,
    REQUIRE_TREND_ALIGNMENT,
    VWAP_LOOKBACK_BARS,
    ENTRY_EMA_FAST_PERIOD,
    ENTRY_EMA_SLOW_PERIOD,
    MAX_ENTRY_DISTANCE_FROM_VWAP_PCT,
    MIN_DISTANCE_TO_VWAP_TP_PCT,
    ENTRY_LOOKBACK,
    LIQUIDITY_LOOKBACK,
    SWEEP_SCAN_LOOKBACK,
    MIN_SWEEP_PCT,
    MAX_SWEEP_PCT,
    SWEEP_CLOSE_BACK_INSIDE,
    MIN_REJECTION_WICK_RATIO,
    AVG_BODY_PERIOD,
    AVG_VOLUME_PERIOD,
    MIN_SWEEP_VOLUME_MULTIPLIER,
    CONFIRM_VOLUME_MULTIPLIER,
    CONFIRM_BREAK_PREVIOUS_CANDLE,
    MAX_CONFIRM_CANDLE_BODY_PCT,
    MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT,
    MAX_RECENT_MOVE_PCT,
    RECENT_MOVE_LOOKBACK,
    INVALIDATE_SWEEP_BUFFER_PCT,
    TAKE_PROFIT_PRICE_PCT,
    STOP_LOSS_PRICE_PCT,
    PENDING_SETUP_EXPIRE_CANDLES,
    MIN_TP_ROI_PCT,
    MAX_TP_ROI_PCT,
    MIN_SL_ROI_PCT,
    MAX_SL_ROI_PCT,
    MIN_SIGNAL_SCORE,
    LEVERAGE,
    CANDLE_MINUTES,
)

logger = logging.getLogger(__name__)

MONITOR_LOG_REPEAT_SECONDS = 180
_LAST_MONITOR_LOG: dict[str, tuple[str, datetime]] = {}


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


# ── Logging helpers ───────────────────────────────────────────────

def _setup_id(setup: dict) -> str:
    return str(setup.get("id", "?"))


def _setup_label(setup: dict) -> str:
    return f"#{_setup_id(setup)} {setup.get('symbol', '?')} {setup.get('direction', '?')}"


def _fmt(value, digits: int = 8) -> str:
    try:
        return f"{float(value):.{digits}g}"
    except Exception:
        return str(value)


def _log_monitor_reason(setup: dict, reason: str, details: str = "", *, force: bool = False) -> None:
    now = datetime.now(timezone.utc)
    setup_key = _setup_id(setup)
    last = _LAST_MONITOR_LOG.get(setup_key)

    if not force and last is not None:
        last_reason, last_time = last
        if last_reason == reason and (now - last_time).total_seconds() < MONITOR_LOG_REPEAT_SECONDS:
            return

    _LAST_MONITOR_LOG[setup_key] = (reason, now)

    msg = f"[SETUP-REASON] {_setup_label(setup)} | {reason}"
    if details:
        msg += f" | {details}"
    logger.info(msg)


# ── Data / candle helpers ─────────────────────────────────────────

def _ensure_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()
    return df.copy()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _vwap(df: pd.DataFrame, lookback: int) -> pd.Series:
    recent = df.tail(lookback).copy()
    typical = (
        recent["high"].astype(float)
        + recent["low"].astype(float)
        + recent["close"].astype(float)
    ) / 3.0

    if "volume" in recent.columns:
        volume = recent["volume"].astype(float).clip(lower=0.0)
    else:
        volume = pd.Series([1.0] * len(recent), index=recent.index)

    pv = typical * volume
    cumulative_volume = volume.cumsum().replace(0, pd.NA)
    values = (pv.cumsum() / cumulative_volume).ffill()

    out = pd.Series(index=df.index, dtype=float)
    out.loc[recent.index] = values
    return out.ffill()


def _body_size(row: pd.Series) -> float:
    return abs(float(row["close"]) - float(row["open"]))


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])
    return (_body_size(row) / close * 100.0) if close > 0 else 0.0


def _is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


def _close_position(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    rng = high - low
    if rng <= 0:
        return 0.5
    return (close - low) / rng


def _lower_wick_ratio(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])
    close = float(row["close"])
    rng = high - low
    if rng <= 0:
        return 0.0
    lower_wick = min(open_price, close) - low
    return max(lower_wick / rng, 0.0)


def _upper_wick_ratio(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])
    close = float(row["close"])
    rng = high - low
    if rng <= 0:
        return 0.0
    upper_wick = high - max(open_price, close)
    return max(upper_wick / rng, 0.0)


def _avg_body(df: pd.DataFrame, pos: int, period: int) -> float:
    start = max(0, pos - period)
    subset = df.iloc[start:pos]
    if subset.empty:
        return 0.0
    return float((subset["close"].astype(float) - subset["open"].astype(float)).abs().mean())


def _avg_volume(df: pd.DataFrame, pos: int, period: int) -> float:
    if "volume" not in df.columns:
        return 0.0
    start = max(0, pos - period)
    subset = df.iloc[start:pos]
    if subset.empty:
        return 0.0
    return float(subset["volume"].astype(float).mean())


def _distance_pct(price: float, reference: float) -> float:
    if reference <= 0:
        return 999.0
    return abs(price - reference) / reference * 100.0


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_iso(ts) -> str:
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


# ── Trend ─────────────────────────────────────────────────────────

def _get_trend_bias(trend_df: pd.DataFrame) -> tuple[str | None, dict]:
    df = _ensure_df(trend_df)
    completed = df.iloc[:-1].copy()

    min_len = EMA_SLOW_PERIOD + TREND_SLOPE_LOOKBACK + 5
    if len(completed) < min_len:
        return None, {"reason": "NOT_ENOUGH_TREND_CANDLES"}

    close = completed["close"].astype(float)
    completed["ema_fast"] = _ema(close, EMA_FAST_PERIOD)
    completed["ema_slow"] = _ema(close, EMA_SLOW_PERIOD)

    last = completed.iloc[-1]
    old = completed.iloc[-1 - TREND_SLOPE_LOOKBACK]

    last_close = float(last["close"])
    ema_fast = float(last["ema_fast"])
    ema_slow = float(last["ema_slow"])
    fast_slope = ema_fast - float(old["ema_fast"])
    slow_slope = ema_slow - float(old["ema_slow"])

    if last_close > ema_fast > ema_slow and fast_slope > 0 and slow_slope >= 0:
        return "LONG", {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "fast_slope": fast_slope,
            "slow_slope": slow_slope,
        }

    if last_close < ema_fast < ema_slow and fast_slope < 0 and slow_slope <= 0:
        return "SHORT", {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "fast_slope": fast_slope,
            "slow_slope": slow_slope,
        }

    return "NEUTRAL", {
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "fast_slope": fast_slope,
        "slow_slope": slow_slope,
    }


# ── Price model ───────────────────────────────────────────────────

def _calculate_fixed_prices(direction: str, entry: float) -> tuple[float, float, float, float] | None:
    if entry <= 0:
        return None

    if direction == "LONG":
        tp_price = entry * (1.0 + TAKE_PROFIT_PRICE_PCT / 100.0)
        sl_price = entry * (1.0 - STOP_LOSS_PRICE_PCT / 100.0)
    else:
        tp_price = entry * (1.0 - TAKE_PROFIT_PRICE_PCT / 100.0)
        sl_price = entry * (1.0 + STOP_LOSS_PRICE_PCT / 100.0)

    tp_roi = TAKE_PROFIT_PRICE_PCT * LEVERAGE
    sl_roi = STOP_LOSS_PRICE_PCT * LEVERAGE

    if tp_roi < MIN_TP_ROI_PCT or tp_roi > MAX_TP_ROI_PCT:
        return None
    if sl_roi < MIN_SL_ROI_PCT or sl_roi > MAX_SL_ROI_PCT:
        return None

    return round(tp_price, 8), round(sl_price, 8), round(tp_roi, 1), round(sl_roi, 1)


# ── Sweep detection ───────────────────────────────────────────────

def _prepare_entry_df(df: pd.DataFrame) -> pd.DataFrame:
    prepared = df.copy()
    close = prepared["close"].astype(float)
    prepared["ema_fast_entry"] = _ema(close, ENTRY_EMA_FAST_PERIOD)
    prepared["ema_slow_entry"] = _ema(close, ENTRY_EMA_SLOW_PERIOD)
    prepared["vwap"] = _vwap(prepared, VWAP_LOOKBACK_BARS)
    return prepared


def _recent_move_pct(df: pd.DataFrame, pos: int) -> float:
    start = max(0, pos - RECENT_MOVE_LOOKBACK)
    window = df.iloc[start:pos + 1]
    if window.empty:
        return 0.0
    high = float(window["high"].astype(float).max())
    low = float(window["low"].astype(float).min())
    mid = (high + low) / 2.0
    if mid <= 0:
        return 999.0
    return (high - low) / mid * 100.0


def _detect_long_sweep(df: pd.DataFrame, pos: int) -> dict | None:
    row = df.iloc[pos]
    start = max(0, pos - LIQUIDITY_LOOKBACK)
    prev = df.iloc[start:pos]

    if prev.empty:
        return None

    level = float(prev["low"].astype(float).min())
    low = float(row["low"])
    close = float(row["close"])

    if level <= 0 or low >= level:
        return None

    sweep_pct = (level - low) / level * 100.0
    if sweep_pct < MIN_SWEEP_PCT or sweep_pct > MAX_SWEEP_PCT:
        return None

    if SWEEP_CLOSE_BACK_INSIDE and close <= level:
        return None

    if _lower_wick_ratio(row) < MIN_REJECTION_WICK_RATIO:
        return None

    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)
    volume = float(row.get("volume", 0.0))
    if avg_vol > 0 and volume < avg_vol * MIN_SWEEP_VOLUME_MULTIPLIER:
        return None

    return {
        "direction": "LONG",
        "type": "SELL_SIDE_LIQUIDITY_SWEEP",
        "level": level,
        "extreme": low,
        "pos": pos,
        "time": df.index[pos],
        "sweep_pct": sweep_pct,
        "wick_ratio": _lower_wick_ratio(row),
    }


def _detect_short_sweep(df: pd.DataFrame, pos: int) -> dict | None:
    row = df.iloc[pos]
    start = max(0, pos - LIQUIDITY_LOOKBACK)
    prev = df.iloc[start:pos]

    if prev.empty:
        return None

    level = float(prev["high"].astype(float).max())
    high = float(row["high"])
    close = float(row["close"])

    if level <= 0 or high <= level:
        return None

    sweep_pct = (high - level) / level * 100.0
    if sweep_pct < MIN_SWEEP_PCT or sweep_pct > MAX_SWEEP_PCT:
        return None

    if SWEEP_CLOSE_BACK_INSIDE and close >= level:
        return None

    if _upper_wick_ratio(row) < MIN_REJECTION_WICK_RATIO:
        return None

    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)
    volume = float(row.get("volume", 0.0))
    if avg_vol > 0 and volume < avg_vol * MIN_SWEEP_VOLUME_MULTIPLIER:
        return None

    return {
        "direction": "SHORT",
        "type": "BUY_SIDE_LIQUIDITY_SWEEP",
        "level": level,
        "extreme": high,
        "pos": pos,
        "time": df.index[pos],
        "sweep_pct": sweep_pct,
        "wick_ratio": _upper_wick_ratio(row),
    }


def _score_setup(df: pd.DataFrame, pos: int, sweep: dict, trend_bias: str | None) -> float:
    row = df.iloc[pos]
    direction = sweep["direction"]

    score = 45.0

    # Trend alignment is valuable but not the whole strategy.
    if trend_bias == direction:
        score += 20.0
    elif trend_bias == "NEUTRAL":
        score += 8.0
    else:
        score -= 12.0

    # Sweep quality.
    sweep_pct = float(sweep.get("sweep_pct", 0.0))
    if 0.10 <= sweep_pct <= 0.80:
        score += 14.0
    else:
        score += 7.0

    wick_ratio = float(sweep.get("wick_ratio", 0.0))
    score += min(wick_ratio * 20.0, 12.0)

    # VWAP/EMA quality.
    close = float(row["close"])
    vwap = float(row.get("vwap", 0.0))
    ema_fast = float(row.get("ema_fast_entry", 0.0))
    ema_slow = float(row.get("ema_slow_entry", 0.0))

    if vwap > 0:
        dist_vwap = _distance_pct(close, vwap)
        if dist_vwap <= MAX_ENTRY_DISTANCE_FROM_VWAP_PCT:
            score += 10.0
        else:
            score -= 10.0

        distance_to_vwap = ((vwap - close) / close * 100.0) if direction == "LONG" else ((close - vwap) / close * 100.0)
        if distance_to_vwap >= MIN_DISTANCE_TO_VWAP_TP_PCT:
            score += 5.0

    if direction == "LONG" and ema_fast >= ema_slow:
        score += 5.0
    if direction == "SHORT" and ema_fast <= ema_slow:
        score += 5.0

    # Volume.
    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)
    vol = float(row.get("volume", 0.0))
    if avg_vol > 0:
        vol_ratio = vol / avg_vol
        score += min(max((vol_ratio - 1.0) * 10.0, 0.0), 8.0)

    # Anti-chase.
    recent_move = _recent_move_pct(df, pos)
    if recent_move > MAX_RECENT_MOVE_PCT:
        score -= 18.0

    return round(min(max(score, 0.0), 100.0), 1)


def detect_setup(symbol: str) -> dict | None:
    """
    Detects a liquidity sweep and saves a pending setup.
    The actual Telegram signal is fired by evaluate_pending_setup().
    """
    try:
        trend_df = _ensure_df(get_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT))
        entry_df = _ensure_df(get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT))

        if entry_df.empty:
            return None

        trend_bias, trend_details = _get_trend_bias(trend_df) if not trend_df.empty else ("NEUTRAL", {})

        completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)
        if len(completed) < LIQUIDITY_LOOKBACK + AVG_BODY_PERIOD + 10:
            return None

        completed = _prepare_entry_df(completed)

        scan_start = max(LIQUIDITY_LOOKBACK, len(completed) - SWEEP_SCAN_LOOKBACK)
        scan_end = len(completed) - 1

        best_setup = None
        best_score = -1.0

        for pos in range(scan_start, scan_end + 1):
            long_sweep = _detect_long_sweep(completed, pos)
            short_sweep = _detect_short_sweep(completed, pos)

            for sweep in (long_sweep, short_sweep):
                if not sweep:
                    continue

                direction = sweep["direction"]

                if REQUIRE_TREND_ALIGNMENT and trend_bias not in (direction, "NEUTRAL"):
                    continue

                row = completed.iloc[pos]
                close = float(row["close"])
                vwap = float(row.get("vwap", 0.0))
                ema_fast = float(row.get("ema_fast_entry", 0.0))

                # Avoid entries after price is too far away from VWAP already.
                if vwap > 0 and _distance_pct(close, vwap) > MAX_ENTRY_DISTANCE_FROM_VWAP_PCT:
                    continue

                # Basic rejection toward EMA/VWAP.
                if direction == "LONG":
                    if ema_fast > 0 and close < ema_fast:
                        continue
                else:
                    if ema_fast > 0 and close > ema_fast:
                        continue

                score = _score_setup(completed, pos, sweep, trend_bias)
                if score < MIN_SIGNAL_SCORE or score <= best_score:
                    continue

                prices = _calculate_fixed_prices(direction, close)
                if not prices:
                    continue

                target_price, sl_price, _, _ = prices
                rr_estimate = TAKE_PROFIT_PRICE_PCT / STOP_LOSS_PRICE_PCT

                # Use the swept liquidity level as a small retest/reclaim zone.
                level = float(sweep["level"])
                zone_pad = level * (MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT / 100.0)
                zone_low = level - zone_pad
                zone_high = level + zone_pad

                expires_at = datetime.now(timezone.utc) + timedelta(
                    minutes=PENDING_SETUP_EXPIRE_CANDLES * CANDLE_MINUTES
                )

                setup_time = _to_iso(sweep["time"])

                best_score = score
                best_setup = {
                    "symbol": symbol,
                    "direction": direction,
                    "trend_tf": TREND_TF,
                    "entry_tf": ENTRY_TF,
                    "bias": trend_bias or "NEUTRAL",
                    "bias_break": trend_details.get("ema_fast"),

                    "sweep_type": sweep["type"],
                    "sweep_level": level,
                    "sweep_extreme": float(sweep["extreme"]),
                    "sweep_time": setup_time,

                    # Reusing DB order-block columns for the reclaim/retest zone.
                    "ob_type": "VWAP_SWEEP_RECLAIM_ZONE",
                    "ob_low": round(zone_low, 8),
                    "ob_high": round(zone_high, 8),
                    "ob_time": setup_time,

                    "target_price": target_price,
                    "sl_price": sl_price,
                    "rr_estimate": round(rr_estimate, 2),
                    "score": score,
                    "setup_time": setup_time,
                    "expires_at": expires_at.isoformat(),
                }

        if best_setup:
            logger.info(
                f"[SETUP] {best_setup['direction']} {symbol} | "
                f"{best_setup['sweep_type']} level={best_setup['sweep_level']:.6g} "
                f"zone={best_setup['ob_low']:.6g}-{best_setup['ob_high']:.6g} "
                f"score={best_setup['score']} strategy=VWAP-Sweep-v3"
            )

        return best_setup

    except Exception as e:
        logger.error(f"Error detecting VWAP sweep setup for {symbol}: {e}", exc_info=True)
        return None


# ── Pending setup monitor ─────────────────────────────────────────

def _is_confirmation(row: pd.Series, prev: pd.Series, setup: dict, avg_volume: float) -> bool:
    direction = setup["direction"]
    level = float(setup["sweep_level"])
    zone_low = float(setup["ob_low"])
    zone_high = float(setup["ob_high"])

    if _body_pct(row) > MAX_CONFIRM_CANDLE_BODY_PCT:
        return False

    volume = float(row.get("volume", 0.0))
    if avg_volume > 0 and volume < avg_volume * CONFIRM_VOLUME_MULTIPLIER:
        return False

    close = float(row["close"])
    vwap = float(row.get("vwap", 0.0))
    ema_fast = float(row.get("ema_fast_entry", 0.0))

    if direction == "LONG":
        if not _is_bullish(row):
            return False
        if close < level:
            return False
        if ema_fast > 0 and close < ema_fast:
            return False
        if CONFIRM_BREAK_PREVIOUS_CANDLE and close <= float(prev["high"]):
            return False
        if _distance_pct(close, zone_high) > MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT + 0.25:
            return False
        if vwap > 0 and close > vwap * (1.0 + MAX_ENTRY_DISTANCE_FROM_VWAP_PCT / 100.0):
            return False
        return True

    if not _is_bearish(row):
        return False
    if close > level:
        return False
    if ema_fast > 0 and close > ema_fast:
        return False
    if CONFIRM_BREAK_PREVIOUS_CANDLE and close >= float(prev["low"]):
        return False
    if _distance_pct(close, zone_low) > MAX_CONFIRM_DISTANCE_FROM_SWEEP_LEVEL_PCT + 0.25:
        return False
    if vwap > 0 and close < vwap * (1.0 - MAX_ENTRY_DISTANCE_FROM_VWAP_PCT / 100.0):
        return False

    return True


def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    """
    Returns:
        ("WAIT", None)
        ("EXPIRED", None)
        ("INVALIDATED", None)
        ("FIRE", Signal)
    """
    try:
        now = datetime.now(timezone.utc)
        expires_at = _parse_utc(setup["expires_at"])

        if now >= expires_at:
            _log_monitor_reason(setup, "EXPIRED_TIMEOUT", force=True)
            return "EXPIRED", None

        symbol = setup["symbol"]
        direction = setup["direction"]
        sweep_extreme = float(setup["sweep_extreme"])

        df = _ensure_df(get_klines(symbol, ENTRY_TF, count=MONITOR_KLINE_COUNT))
        if df.empty or len(df) < 12:
            _log_monitor_reason(setup, "WAIT_DATA_NOT_READY")
            return "WAIT", None

        completed = _prepare_entry_df(df.iloc[:-1].copy())
        recent = completed.tail(6)

        # Invalidation: if the sweep extreme breaks again before entry, the trap likely failed.
        if direction == "LONG":
            invalid_level = sweep_extreme * (1.0 - INVALIDATE_SWEEP_BUFFER_PCT / 100.0)
            recent_low = float(recent["low"].astype(float).min())
            if recent_low <= invalid_level:
                _log_monitor_reason(
                    setup,
                    "INVALIDATED_SWEEP_EXTREME_BROKEN",
                    f"low={_fmt(recent_low)} invalid={_fmt(invalid_level)}",
                    force=True,
                )
                return "INVALIDATED", None
        else:
            invalid_level = sweep_extreme * (1.0 + INVALIDATE_SWEEP_BUFFER_PCT / 100.0)
            recent_high = float(recent["high"].astype(float).max())
            if recent_high >= invalid_level:
                _log_monitor_reason(
                    setup,
                    "INVALIDATED_SWEEP_EXTREME_BROKEN",
                    f"high={_fmt(recent_high)} invalid={_fmt(invalid_level)}",
                    force=True,
                )
                return "INVALIDATED", None

        if len(completed) < 2:
            return "WAIT", None

        prev = completed.iloc[-2]
        trigger = completed.iloc[-1]

        avg_vol = (
            float(completed["volume"].astype(float).tail(AVG_VOLUME_PERIOD + 1).iloc[:-1].mean())
            if "volume" in completed.columns and len(completed) > AVG_VOLUME_PERIOD
            else 0.0
        )

        if not _is_confirmation(trigger, prev, setup, avg_vol):
            _log_monitor_reason(
                setup,
                "WAIT_VWAP_SWEEP_CONFIRMATION",
                f"close={_fmt(trigger['close'])} level={_fmt(setup['sweep_level'])}",
            )
            return "WAIT", None

        entry = float(trigger["close"])
        prices = _calculate_fixed_prices(direction, entry)
        if not prices:
            _log_monitor_reason(setup, "WAIT_PRICE_MODEL_FAILED")
            return "WAIT", None

        tp_price, sl_price, tp_roi_pct, sl_roi_pct = prices
        score = min(float(setup["score"]) + 5.0, 100.0)

        _log_monitor_reason(
            setup,
            "FIRE_VWAP_SWEEP_ENTRY_CONFIRMED",
            f"entry={_fmt(entry)} tp={_fmt(tp_price)} sl={_fmt(sl_price)} score={score}",
            force=True,
        )

        return "FIRE", Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            tp_price=tp_price,
            sl_price=sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            timeframe_summary=(
                f"VWAP Liquidity Sweep v3 | {TREND_TF} bias {setup['bias']} | "
                f"{ENTRY_TF} sweep + EMA/VWAP reclaim"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error evaluating VWAP sweep setup {setup.get('id')}: {e}", exc_info=True)
        return "WAIT", None


def analyze_coin(symbol: str) -> "Signal | None":
    return None