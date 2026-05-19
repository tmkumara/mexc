"""
Phase 1 Strategy — Momentum Pullback Scalper.

Goal:
    - More practical signal frequency than strict SMC/OB retest.
    - Target small moves with leverage.
    - Keep existing project flow:
        detect_setup(symbol) -> saved to pending_setups
        evaluate_pending_setup(setup) -> fires Signal after pullback confirmation

Strategy:
    1. 1H trend filter using EMA20/EMA50 and EMA slope.
    2. 5m momentum impulse candle:
        - direction matches 1H trend
        - body larger than average
        - volume above average
        - close breaks recent structure
    3. Save pullback zone as pending setup.
    4. Monitor waits for price to retrace into pullback zone.
    5. Fire only after confirmation candle in trend direction.

This is not financial advice. No strategy can guarantee profit.
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
    ENTRY_LOOKBACK,
    AVG_BODY_PERIOD,
    AVG_VOLUME_PERIOD,
    MOMENTUM_LOOKBACK,
    MOMENTUM_BREAKOUT_LOOKBACK,
    MOMENTUM_BODY_MULTIPLIER,
    MOMENTUM_VOLUME_MULTIPLIER,
    MOMENTUM_CLOSE_POSITION,
    PULLBACK_MIN_RETRACE,
    PULLBACK_MAX_RETRACE,
    CONFIRM_BREAK_PREVIOUS_CANDLE,
    CONFIRM_VOLUME_MULTIPLIER,
    MAX_CONFIRM_CANDLE_BODY_PCT,
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


def _log_monitor_reason(
    setup: dict,
    reason: str,
    details: str = "",
    *,
    force: bool = False,
) -> None:
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


# ── Candle helpers ────────────────────────────────────────────────

def _ensure_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    required = {"open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()
    return df.copy()


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


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


def _touches_zone(row: pd.Series, zone_low: float, zone_high: float) -> bool:
    high = float(row["high"])
    low = float(row["low"])
    return low <= zone_high and high >= zone_low


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Trend filter ──────────────────────────────────────────────────

def _get_trend_bias(trend_df: pd.DataFrame) -> tuple[str | None, dict]:
    df = _ensure_df(trend_df)

    # Use completed candles only.
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

    if (
        last_close > ema_fast > ema_slow
        and fast_slope > 0
        and slow_slope >= 0
    ):
        return "LONG", {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "fast_slope": fast_slope,
            "slow_slope": slow_slope,
        }

    if (
        last_close < ema_fast < ema_slow
        and fast_slope < 0
        and slow_slope <= 0
    ):
        return "SHORT", {
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "fast_slope": fast_slope,
            "slow_slope": slow_slope,
        }

    return None, {
        "reason": "NO_CLEAR_EMA_TREND",
        "close": last_close,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "fast_slope": fast_slope,
        "slow_slope": slow_slope,
    }


# ── Momentum setup detection ──────────────────────────────────────

def _is_long_momentum(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)
    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)

    if avg_body <= 0:
        return False

    prev_start = max(0, pos - MOMENTUM_BREAKOUT_LOOKBACK)
    prev = df.iloc[prev_start:pos]
    if prev.empty:
        return False

    close = float(row["close"])
    volume = float(row.get("volume", 0.0))

    body_ok = _body_size(row) >= avg_body * MOMENTUM_BODY_MULTIPLIER
    volume_ok = avg_vol <= 0 or volume >= avg_vol * MOMENTUM_VOLUME_MULTIPLIER
    close_ok = _close_position(row) >= MOMENTUM_CLOSE_POSITION
    breakout_ok = close > float(prev["high"].astype(float).max())

    return _is_bullish(row) and body_ok and volume_ok and close_ok and breakout_ok


def _is_short_momentum(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)
    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)

    if avg_body <= 0:
        return False

    prev_start = max(0, pos - MOMENTUM_BREAKOUT_LOOKBACK)
    prev = df.iloc[prev_start:pos]
    if prev.empty:
        return False

    close = float(row["close"])
    volume = float(row.get("volume", 0.0))

    body_ok = _body_size(row) >= avg_body * MOMENTUM_BODY_MULTIPLIER
    volume_ok = avg_vol <= 0 or volume >= avg_vol * MOMENTUM_VOLUME_MULTIPLIER
    close_ok = _close_position(row) <= (1.0 - MOMENTUM_CLOSE_POSITION)
    breakout_ok = close < float(prev["low"].astype(float).min())

    return _is_bearish(row) and body_ok and volume_ok and close_ok and breakout_ok


def _build_pullback_zone(direction: str, row: pd.Series) -> tuple[float, float]:
    high = float(row["high"])
    low = float(row["low"])
    rng = high - low

    if rng <= 0:
        return low, high

    if direction == "LONG":
        zone_high = high - rng * PULLBACK_MIN_RETRACE
        zone_low = high - rng * PULLBACK_MAX_RETRACE
    else:
        zone_low = low + rng * PULLBACK_MIN_RETRACE
        zone_high = low + rng * PULLBACK_MAX_RETRACE

    return round(min(zone_low, zone_high), 8), round(max(zone_low, zone_high), 8)


def _score_momentum_setup(df: pd.DataFrame, pos: int, direction: str) -> float:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)
    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)

    body_ratio = (_body_size(row) / avg_body) if avg_body > 0 else 1.0

    volume = float(row.get("volume", 0.0))
    volume_ratio = (volume / avg_vol) if avg_vol > 0 else 1.0

    close_pos = _close_position(row)
    if direction == "SHORT":
        close_pos = 1.0 - close_pos

    score = 50.0
    score += min(max((body_ratio - 1.0) * 18.0, 0.0), 25.0)
    score += min(max((volume_ratio - 1.0) * 12.0, 0.0), 15.0)
    score += min(max((close_pos - 0.5) * 50.0, 0.0), 15.0)

    # Fresh impulses are better. The scanner only looks at recent momentum,
    # but this keeps the newest candidate preferred.
    age = len(df) - 1 - pos
    if age <= 2:
        score += 10.0
    elif age <= 5:
        score += 6.0
    else:
        score += 3.0

    return round(min(score, 100.0), 1)


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


def detect_setup(symbol: str) -> dict | None:
    """
    Detects a momentum impulse and returns a pending pullback setup.

    This does NOT fire a signal.
    """
    try:
        trend_df = get_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        entry_df = get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        trend_df = _ensure_df(trend_df)
        entry_df = _ensure_df(entry_df)

        if trend_df.empty or entry_df.empty:
            return None

        bias, trend_details = _get_trend_bias(trend_df)
        if bias is None:
            return None

        completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)
        if len(completed) < max(80, AVG_BODY_PERIOD + MOMENTUM_BREAKOUT_LOOKBACK + 5):
            return None

        start = max(AVG_BODY_PERIOD + MOMENTUM_BREAKOUT_LOOKBACK, len(completed) - MOMENTUM_LOOKBACK)
        end = len(completed) - 1

        best_setup = None
        best_score = -1.0

        for pos in range(start, end + 1):
            if bias == "LONG":
                is_momentum = _is_long_momentum(completed, pos)
            else:
                is_momentum = _is_short_momentum(completed, pos)

            if not is_momentum:
                continue

            row = completed.iloc[pos]
            score = _score_momentum_setup(completed, pos, bias)

            if score < MIN_SIGNAL_SCORE or score <= best_score:
                continue

            zone_low, zone_high = _build_pullback_zone(bias, row)
            impulse_high = float(row["high"])
            impulse_low = float(row["low"])
            impulse_close = float(row["close"])

            # Placeholder TP/SL for database compatibility.
            # Final TP/SL is calculated from actual confirmed entry in evaluate_pending_setup().
            preview_prices = _calculate_fixed_prices(bias, impulse_close)
            if not preview_prices:
                continue

            target_price, sl_price, _, _ = preview_prices
            rr_estimate = TAKE_PROFIT_PRICE_PCT / STOP_LOSS_PRICE_PCT

            setup_time = completed.index[pos]
            setup_dt = (
                setup_time.to_pydatetime().replace(tzinfo=timezone.utc)
                if hasattr(setup_time, "to_pydatetime")
                else datetime.now(timezone.utc)
            )

            expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=PENDING_SETUP_EXPIRE_CANDLES * CANDLE_MINUTES
            )

            best_score = score
            best_setup = {
                "symbol": symbol,
                "direction": bias,
                "trend_tf": TREND_TF,
                "entry_tf": ENTRY_TF,
                "bias": bias,
                "bias_break": trend_details.get("ema_fast"),

                # Reusing old DB column names for new strategy metadata.
                "sweep_type": "MOMENTUM_IMPULSE",
                "sweep_level": impulse_close,
                "sweep_extreme": impulse_low if bias == "LONG" else impulse_high,
                "sweep_time": setup_dt.isoformat(),

                "ob_type": "PULLBACK_ZONE",
                "ob_low": zone_low,
                "ob_high": zone_high,
                "ob_time": setup_dt.isoformat(),

                "target_price": target_price,
                "sl_price": sl_price,
                "rr_estimate": round(rr_estimate, 2),
                "score": score,
                "setup_time": setup_dt.isoformat(),
                "expires_at": expires_at.isoformat(),
            }

        if best_setup:
            logger.info(
                f"[SETUP] {best_setup['direction']} {symbol} | "
                f"zone={best_setup['ob_low']:.6g}-{best_setup['ob_high']:.6g} "
                f"score={best_setup['score']} "
                f"strategy=MomentumPullback"
            )

        return best_setup

    except Exception as e:
        logger.error(f"Error detecting setup for {symbol}: {e}", exc_info=True)
        return None


# ── Pending setup monitor ─────────────────────────────────────────

def _is_confirmation(row: pd.Series, prev: pd.Series, direction: str, avg_volume: float) -> bool:
    if _body_pct(row) > MAX_CONFIRM_CANDLE_BODY_PCT:
        return False

    volume = float(row.get("volume", 0.0))
    volume_ok = avg_volume <= 0 or volume >= avg_volume * CONFIRM_VOLUME_MULTIPLIER

    if direction == "LONG":
        candle_ok = _is_bullish(row)
        close_break_ok = (
            float(row["close"]) > float(prev["high"])
            if CONFIRM_BREAK_PREVIOUS_CANDLE
            else True
        )
        close_position_ok = _close_position(row) >= 0.55
        return candle_ok and close_break_ok and close_position_ok and volume_ok

    candle_ok = _is_bearish(row)
    close_break_ok = (
        float(row["close"]) < float(prev["low"])
        if CONFIRM_BREAK_PREVIOUS_CANDLE
        else True
    )
    close_position_ok = _close_position(row) <= 0.45
    return candle_ok and close_break_ok and close_position_ok and volume_ok


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
        zone_low = float(setup["ob_low"])
        zone_high = float(setup["ob_high"])

        df = get_klines(symbol, ENTRY_TF, count=MONITOR_KLINE_COUNT)
        df = _ensure_df(df)

        if df.empty or len(df) < 10:
            _log_monitor_reason(setup, "WAIT_DATA_NOT_READY")
            return "WAIT", None

        completed = df.iloc[:-1].copy()
        recent = completed.tail(8)

        # Find whether the pending zone has been retested recently.
        touched_rows = []
        for idx, row in recent.iterrows():
            if _touches_zone(row, zone_low, zone_high):
                touched_rows.append((idx, row))

        if not touched_rows:
            last_close = float(completed["close"].iloc[-1])
            _log_monitor_reason(
                setup,
                "WAIT_NO_PULLBACK_TOUCH",
                f"zone={_fmt(zone_low)}-{_fmt(zone_high)} close={_fmt(last_close)}",
            )
            return "WAIT", None

        # Use last completed candle as the actual trigger candle.
        if len(completed) < 2:
            return "WAIT", None

        prev = completed.iloc[-2]
        trigger = completed.iloc[-1]
        avg_vol = float(completed["volume"].astype(float).tail(AVG_VOLUME_PERIOD + 1).iloc[:-1].mean()) \
            if "volume" in completed.columns and len(completed) > AVG_VOLUME_PERIOD else 0.0

        # Basic invalidation before entry: if price moved too far beyond zone, setup is stale.
        if direction == "LONG":
            invalid_level = zone_low * (1.0 - STOP_LOSS_PRICE_PCT / 100.0)
            if float(trigger["low"]) <= invalid_level:
                _log_monitor_reason(
                    setup,
                    "INVALIDATED_PULLBACK_TOO_DEEP",
                    f"low={_fmt(trigger['low'])} invalid={_fmt(invalid_level)}",
                    force=True,
                )
                return "INVALIDATED", None
        else:
            invalid_level = zone_high * (1.0 + STOP_LOSS_PRICE_PCT / 100.0)
            if float(trigger["high"]) >= invalid_level:
                _log_monitor_reason(
                    setup,
                    "INVALIDATED_PULLBACK_TOO_DEEP",
                    f"high={_fmt(trigger['high'])} invalid={_fmt(invalid_level)}",
                    force=True,
                )
                return "INVALIDATED", None

        if not _is_confirmation(trigger, prev, direction, avg_vol):
            _log_monitor_reason(
                setup,
                "WAIT_CONFIRMATION_CANDLE",
                f"body={_body_pct(trigger):.2f}% close={_fmt(trigger['close'])}",
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
            "FIRE_ENTRY_CONFIRMED",
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
                f"Momentum Pullback | {TREND_TF} trend {setup['bias']} | "
                f"{ENTRY_TF} impulse + pullback confirmation"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error evaluating pending setup {setup.get('id')}: {e}", exc_info=True)
        return "WAIT", None


# Compatibility wrapper. Main.py does not use this in the stateful flow.
def analyze_coin(symbol: str) -> "Signal | None":
    setup = detect_setup(symbol)
    if not setup:
        return None
    return None
