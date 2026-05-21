"""
Strategy — Breakout + Retest + EMA/VWAP Scalper.

Flow:
    1. detect_setup(symbol)
       - Detect 5m close above previous 20-candle high or below previous 20-candle low.
       - Require EMA50 + VWAP direction alignment.
       - Save pending retest setup.

    2. evaluate_pending_setup(setup)
       - Wait max RETEST_MAX_CANDLES.
       - Confirm retest and rejection.
       - Calculate ATR + structure SL.
       - Use TARGET_RR TP.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd

from market_data import get_market_klines
from config import (
    ENTRY_TF,
    TREND_TF,
    ENTRY_KLINE_COUNT,
    MONITOR_KLINE_COUNT,
    BREAKOUT_LOOKBACK,
    RETEST_MAX_CANDLES,
    EMA_PERIOD,
    VWAP_LOOKBACK_BARS,
    ATR_PERIOD,
    ATR_SL_BUFFER_MULTIPLIER,
    MIN_RR,
    TARGET_RR,
    MAX_RR,
    MAX_BREAKOUT_CANDLE_BODY_PCT,
    MAX_RETEST_CANDLE_BODY_PCT,
    MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT,
    MAX_DISTANCE_FROM_VWAP_PCT,
    MIN_VOLUME_MULTIPLIER,
    AVG_VOLUME_PERIOD,
    MIN_SIGNAL_SCORE,
    CANDLE_MINUTES,
    LEVERAGE,
    MIN_TP_ROI_PCT,
    MAX_TP_ROI_PCT,
    MIN_SL_ROI_PCT,
    MAX_SL_ROI_PCT,
    REST_FALLBACK_ENABLED,
)

logger = logging.getLogger(__name__)

MONITOR_LOG_REPEAT_SECONDS = 180
_LAST_MONITOR_LOG: dict[str, tuple[str, datetime]] = {}


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

    msg = f"[RETEST-REASON] {_setup_label(setup)} | {reason}"
    if details:
        msg += f" | {details}"

    logger.info(msg)


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


def _rolling_vwap(df: pd.DataFrame, lookback: int) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].clip(lower=0.0)

    pv = typical * volume
    min_periods = max(5, min(lookback, 20))

    rolling_pv = pv.rolling(lookback, min_periods=min_periods).sum()
    rolling_vol = volume.rolling(lookback, min_periods=min_periods).sum()

    return (rolling_pv / rolling_vol.replace(0, float("nan"))).ffill()


def _prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)

    if out.empty:
        return out

    out["ema"] = _ema(out["close"], EMA_PERIOD)
    out["vwap"] = _rolling_vwap(out, VWAP_LOOKBACK_BARS)
    out["atr"] = _atr(out, ATR_PERIOD)

    return out


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])
    if close <= 0:
        return 0.0
    return abs(float(row["close"]) - float(row["open"])) / close * 100.0


def _distance_pct(price: float, reference: float) -> float:
    if reference <= 0:
        return 999.0
    return abs(price - reference) / reference * 100.0


def _is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


def _avg_volume(df: pd.DataFrame, pos: int, period: int) -> float:
    start = max(0, pos - period)
    subset = df.iloc[start:pos]

    if subset.empty or "volume" not in subset.columns:
        return 0.0

    return float(subset["volume"].astype(float).mean())


def _to_iso(ts) -> str:
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _score_breakout(df: pd.DataFrame, pos: int, direction: str, level: float) -> float:
    row = df.iloc[pos]
    close = float(row["close"])
    ema = float(row.get("ema", 0.0))
    vwap = float(row.get("vwap", 0.0))

    score = 45.0

    if direction == "LONG" and close > ema:
        score += 12.0
    if direction == "SHORT" and close < ema:
        score += 12.0

    if direction == "LONG" and close > vwap:
        score += 12.0
    if direction == "SHORT" and close < vwap:
        score += 12.0

    dist_level = _distance_pct(close, level)
    if dist_level <= 0.20:
        score += 12.0
    elif dist_level <= MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT:
        score += 7.0
    else:
        score -= 10.0

    dist_vwap = _distance_pct(close, vwap)
    if dist_vwap <= 0.40:
        score += 8.0
    elif dist_vwap <= MAX_DISTANCE_FROM_VWAP_PCT:
        score += 4.0
    else:
        score -= 10.0

    body_pct = _body_pct(row)
    if body_pct <= 0.60:
        score += 7.0
    elif body_pct <= MAX_BREAKOUT_CANDLE_BODY_PCT:
        score += 3.0
    else:
        score -= 12.0

    avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)
    volume = float(row.get("volume", 0.0))

    if avg_vol > 0:
        vol_ratio = volume / avg_vol
        if vol_ratio >= 1.30:
            score += 8.0
        elif vol_ratio >= MIN_VOLUME_MULTIPLIER:
            score += 4.0
        else:
            score -= 8.0

    return round(max(0.0, min(score, 100.0)), 1)


def _calculate_prices(
    direction: str,
    entry: float,
    level: float,
    atr_value: float,
    trigger_row: pd.Series,
) -> tuple[float, float, float, float, float] | None:
    if entry <= 0 or atr_value <= 0 or level <= 0:
        return None

    buffer = atr_value * ATR_SL_BUFFER_MULTIPLIER

    if direction == "LONG":
        sl_price = min(float(trigger_row["low"]), level - buffer)
        risk = entry - sl_price

        if risk <= 0:
            return None

        rr = TARGET_RR
        tp_price = entry + risk * rr

        tp_move_pct = (tp_price - entry) / entry * 100.0
        sl_move_pct = (entry - sl_price) / entry * 100.0

    else:
        sl_price = max(float(trigger_row["high"]), level + buffer)
        risk = sl_price - entry

        if risk <= 0:
            return None

        rr = TARGET_RR
        tp_price = entry - risk * rr

        tp_move_pct = (entry - tp_price) / entry * 100.0
        sl_move_pct = (sl_price - entry) / entry * 100.0

    if rr < MIN_RR or rr > MAX_RR:
        return None

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
        round(rr, 2),
    )


def detect_setup(symbol: str) -> dict | None:
    try:
        raw_df = _ensure_df(
            get_market_klines(
                symbol,
                ENTRY_TF,
                count=ENTRY_KLINE_COUNT,
                allow_rest_fallback=REST_FALLBACK_ENABLED,
            )
        )

        if raw_df.empty or len(raw_df) < EMA_PERIOD + BREAKOUT_LOOKBACK + 10:
            logger.debug("[SETUP-REASON] %s rejected | not_enough_candles", symbol)
            return None

        completed = raw_df.iloc[:-1].copy()
        df = _prepare_df(completed)

        if df.empty or len(df) < EMA_PERIOD + BREAKOUT_LOOKBACK + 5:
            return None

        pos = len(df) - 1
        row = df.iloc[pos]
        prev = df.iloc[pos - BREAKOUT_LOOKBACK:pos]

        if len(prev) < BREAKOUT_LOOKBACK:
            return None

        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        ema = float(row["ema"])
        vwap = float(row["vwap"])
        atr_value = float(row["atr"])

        if close <= 0 or ema <= 0 or vwap <= 0 or atr_value <= 0:
            return None

        previous_high = float(prev["high"].max())
        previous_low = float(prev["low"].min())

        avg_vol = _avg_volume(df, pos, AVG_VOLUME_PERIOD)
        volume = float(row.get("volume", 0.0))

        if avg_vol > 0 and volume < avg_vol * MIN_VOLUME_MULTIPLIER:
            logger.debug("[SETUP-REASON] %s rejected | volume_low", symbol)
            return None

        if _body_pct(row) > MAX_BREAKOUT_CANDLE_BODY_PCT:
            logger.debug("[SETUP-REASON] %s rejected | breakout_body_too_large", symbol)
            return None

        direction = None
        level = None
        breakout_type = None

        if close > previous_high and close > ema and close > vwap:
            direction = "LONG"
            level = previous_high
            breakout_type = "BREAKOUT_PREV_HIGH"

        elif close < previous_low and close < ema and close < vwap:
            direction = "SHORT"
            level = previous_low
            breakout_type = "BREAKDOWN_PREV_LOW"

        else:
            logger.debug(
                "[SETUP-REASON] %s rejected | no_breakout close=%s high20=%s low20=%s ema=%s vwap=%s",
                symbol,
                _fmt(close),
                _fmt(previous_high),
                _fmt(previous_low),
                _fmt(ema),
                _fmt(vwap),
            )
            return None

        if _distance_pct(close, level) > MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT:
            logger.info(
                "[SETUP-REASON] %s %s rejected | too_far_from_breakout close=%s level=%s dist=%.3f%%",
                symbol,
                direction,
                _fmt(close),
                _fmt(level),
                _distance_pct(close, level),
            )
            return None

        if _distance_pct(close, vwap) > MAX_DISTANCE_FROM_VWAP_PCT:
            logger.info(
                "[SETUP-REASON] %s %s rejected | too_far_from_vwap close=%s vwap=%s dist=%.3f%%",
                symbol,
                direction,
                _fmt(close),
                _fmt(vwap),
                _distance_pct(close, vwap),
            )
            return None

        score = _score_breakout(df, pos, direction, level)

        if score < MIN_SIGNAL_SCORE:
            logger.info(
                "[SETUP-REASON] %s %s rejected | score_low score=%s min=%s",
                symbol,
                direction,
                score,
                MIN_SIGNAL_SCORE,
            )
            return None

        display_prices = _calculate_prices(direction, close, level, atr_value, row)

        if not display_prices:
            logger.info(
                "[SETUP-REASON] %s %s rejected | initial_price_model_failed close=%s atr=%s",
                symbol,
                direction,
                _fmt(close),
                _fmt(atr_value),
            )
            return None

        target_price, sl_price, _, _, rr_estimate = display_prices

        zone_pad = level * (MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT / 100.0)
        zone_low = level - zone_pad
        zone_high = level + zone_pad

        setup_time = _to_iso(df.index[pos])
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=RETEST_MAX_CANDLES * CANDLE_MINUTES
        )

        setup = {
            "symbol": symbol,
            "direction": direction,
            "trend_tf": TREND_TF,
            "entry_tf": ENTRY_TF,
            "bias": direction,
            "bias_break": level,

            "sweep_type": breakout_type,
            "sweep_level": level,
            "sweep_extreme": low if direction == "LONG" else high,
            "sweep_time": setup_time,

            "ob_type": "BREAKOUT_RETEST_ZONE",
            "ob_low": round(zone_low, 8),
            "ob_high": round(zone_high, 8),
            "ob_time": setup_time,

            "target_price": target_price,
            "sl_price": sl_price,
            "rr_estimate": rr_estimate,
            "score": score,
            "setup_time": setup_time,
            "expires_at": expires_at.isoformat(),
        }

        logger.info(
            "[SETUP] %s %s | level=%s close=%s ema=%s vwap=%s score=%s expires=%s",
            direction,
            symbol,
            _fmt(level),
            _fmt(close),
            _fmt(ema),
            _fmt(vwap),
            score,
            expires_at.strftime("%H:%M:%S UTC"),
        )

        return setup

    except Exception as e:
        logger.error("Error detecting breakout setup for %s: %s", symbol, e, exc_info=True)
        return None


def _is_valid_retest(trigger: pd.Series, setup: dict) -> tuple[bool, str]:
    direction = setup["direction"]
    level = float(setup["sweep_level"])

    close = float(trigger["close"])
    ema = float(trigger.get("ema", 0.0))
    vwap = float(trigger.get("vwap", 0.0))

    if _body_pct(trigger) > MAX_RETEST_CANDLE_BODY_PCT:
        return False, "retest_candle_body_too_large"

    if _distance_pct(close, level) > MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT:
        return False, "entry_too_far_from_breakout_level"

    if _distance_pct(close, vwap) > MAX_DISTANCE_FROM_VWAP_PCT:
        return False, "entry_too_far_from_vwap"

    if direction == "LONG":
        touched = float(trigger["low"]) <= level * (1.0 + MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT / 100.0)
        reclaimed = close >= level
        trend_ok = close > ema and close > vwap
        candle_ok = _is_bullish(trigger)

        if not touched:
            return False, "price_not_retested_level"
        if not reclaimed:
            return False, "close_not_reclaimed_level"
        if not trend_ok:
            return False, "ema_vwap_not_bullish"
        if not candle_ok:
            return False, "retest_candle_not_bullish"

        return True, "long_retest_confirmed"

    touched = float(trigger["high"]) >= level * (1.0 - MAX_ENTRY_DISTANCE_FROM_BREAKOUT_PCT / 100.0)
    reclaimed = close <= level
    trend_ok = close < ema and close < vwap
    candle_ok = _is_bearish(trigger)

    if not touched:
        return False, "price_not_retested_level"
    if not reclaimed:
        return False, "close_not_reclaimed_level"
    if not trend_ok:
        return False, "ema_vwap_not_bearish"
    if not candle_ok:
        return False, "retest_candle_not_bearish"

    return True, "short_retest_confirmed"


def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    try:
        now = datetime.now(timezone.utc)
        expires_at = _parse_utc(setup["expires_at"])

        if now >= expires_at:
            _log_monitor_reason(setup, "EXPIRED_RETEST_TIMEOUT", force=True)
            return "EXPIRED", None

        symbol = setup["symbol"]
        direction = setup["direction"]
        level = float(setup["sweep_level"])

        raw_df = _ensure_df(
            get_market_klines(
                symbol,
                ENTRY_TF,
                count=MONITOR_KLINE_COUNT,
                allow_rest_fallback=REST_FALLBACK_ENABLED,
            )
        )

        if raw_df.empty or len(raw_df) < EMA_PERIOD + ATR_PERIOD + 5:
            _log_monitor_reason(setup, "WAIT_DATA_NOT_READY")
            return "WAIT", None

        completed = raw_df.iloc[:-1].copy()
        df = _prepare_df(completed)

        if df.empty or len(df) < EMA_PERIOD + ATR_PERIOD + 5:
            _log_monitor_reason(setup, "WAIT_INDICATORS_NOT_READY")
            return "WAIT", None

        recent = df.tail(RETEST_MAX_CANDLES + 1)
        last = recent.iloc[-1]
        atr_value = float(last.get("atr", 0.0))

        if atr_value <= 0:
            _log_monitor_reason(setup, "WAIT_ATR_NOT_READY")
            return "WAIT", None

        if direction == "LONG":
            invalid_close = level - atr_value * 0.50
            if float(last["close"]) < invalid_close:
                _log_monitor_reason(
                    setup,
                    "INVALIDATED_CLOSE_BELOW_BREAKOUT",
                    f"close={_fmt(last['close'])} invalid={_fmt(invalid_close)}",
                    force=True,
                )
                return "INVALIDATED", None
        else:
            invalid_close = level + atr_value * 0.50
            if float(last["close"]) > invalid_close:
                _log_monitor_reason(
                    setup,
                    "INVALIDATED_CLOSE_ABOVE_BREAKDOWN",
                    f"close={_fmt(last['close'])} invalid={_fmt(invalid_close)}",
                    force=True,
                )
                return "INVALIDATED", None

        trigger = recent.iloc[-1]
        valid, reason = _is_valid_retest(trigger, setup)

        if not valid:
            _log_monitor_reason(
                setup,
                f"WAIT_{reason.upper()}",
                f"close={_fmt(trigger['close'])} level={_fmt(level)}",
            )
            return "WAIT", None

        entry = float(trigger["close"])

        prices = _calculate_prices(
            direction=direction,
            entry=entry,
            level=level,
            atr_value=atr_value,
            trigger_row=trigger,
        )

        if not prices:
            _log_monitor_reason(
                setup,
                "WAIT_PRICE_MODEL_FAILED",
                f"entry={_fmt(entry)} level={_fmt(level)} atr={_fmt(atr_value)}",
            )
            return "WAIT", None

        tp_price, sl_price, tp_roi_pct, sl_roi_pct, rr = prices
        score = min(float(setup["score"]) + 7.0, 100.0)

        _log_monitor_reason(
            setup,
            "FIRE_RETEST_CONFIRMED",
            f"entry={_fmt(entry)} tp={_fmt(tp_price)} sl={_fmt(sl_price)} rr={rr} score={score}",
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
                f"Breakout Retest | {ENTRY_TF} | EMA{EMA_PERIOD}+VWAP | RR {rr:g}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error("Error evaluating retest setup %s: %s", setup.get("id"), e, exc_info=True)
        return "WAIT", None


def analyze_coin(symbol: str) -> Signal | None:
    return None