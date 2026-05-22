"""
Strategy — HTF SMC Sweep + FVG/OB Retest Scalper.

Fresh model:
    1. 1h regime filter:
       - EMA50/EMA200 direction + recent close location.
    2. 15m setup detection:
       - liquidity sweep of recent swing
       - displacement candle back in regime direction
       - FVG confirmation
       - order block / FVG confluence zone
       - volume confirmation
    3. 5m execution:
       - retest of confluence zone
       - rejection close
       - VWAP/EMA alignment
       - optional minor BOS after retest
       - ATR/structure SL
       - fixed RR target

This file intentionally keeps the database payload compatible with the existing
pending_setups table by mapping:
    sweep_* -> liquidity sweep information
    ob_*    -> final confluence retest zone
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pandas as pd

from market_data import get_market_klines
from config import (
    STRATEGY_NAME,
    REGIME_TF,
    SETUP_TF,
    EXECUTION_TF,
    REGIME_KLINE_COUNT,
    SETUP_KLINE_COUNT,
    MONITOR_KLINE_COUNT,
    REGIME_EMA_FAST,
    REGIME_EMA_SLOW,
    EMA_PERIOD,
    VWAP_LOOKBACK_BARS,
    ATR_PERIOD,
    ATR_SL_BUFFER_MULTIPLIER,
    SWING_LEFT,
    SWING_RIGHT,
    SETUP_LOOKBACK,
    SWEEP_LOOKBACK,
    DISPLACEMENT_BODY_MULTIPLIER,
    DISPLACEMENT_CLOSE_POSITION,
    ORDER_BLOCK_LOOKBACK,
    FVG_LOOKBACK_AFTER_SWEEP,
    REQUIRE_FVG_CONFIRMATION,
    REQUIRE_VOLUME_CONFIRMATION,
    REQUIRE_MINOR_BOS_AFTER_RETEST,
    AVG_VOLUME_PERIOD,
    MIN_VOLUME_MULTIPLIER,
    MAX_WICK_TO_BODY_RATIO,
    MAX_ENTRY_DISTANCE_FROM_ZONE_PCT,
    MAX_DISTANCE_FROM_VWAP_PCT,
    MIN_RETEST_REJECTION_POSITION,
    RETEST_MAX_CANDLES,
    MIN_RR,
    TARGET_RR,
    MAX_RR,
    MIN_SL_PCT,
    MAX_SL_PCT,
    MIN_SIGNAL_SCORE,
    LEVERAGE,
    MIN_TP_ROI_PCT,
    MAX_TP_ROI_PCT,
    MIN_SL_ROI_PCT,
    MAX_SL_ROI_PCT,
    CANDLE_MINUTES,
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


# ── common helpers ────────────────────────────────────────────────

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

    out.sort_index(inplace=True)
    return out


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    return pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def _rolling_vwap(df: pd.DataFrame, lookback: int) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].clip(lower=0.0)
    pv = typical * volume

    min_periods = max(5, min(lookback, 20))
    rolling_pv = pv.rolling(lookback, min_periods=min_periods).sum()
    rolling_vol = volume.rolling(lookback, min_periods=min_periods).sum()

    return (rolling_pv / rolling_vol.replace(0, float("nan"))).ffill().bfill()


def _prepare_execution_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out

    out["ema"] = _ema(out["close"], EMA_PERIOD)
    out["vwap"] = _rolling_vwap(out, VWAP_LOOKBACK_BARS)
    out["atr"] = _atr(out, ATR_PERIOD)
    return out


def _body_size(row: pd.Series) -> float:
    return abs(float(row["close"]) - float(row["open"]))


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])
    return 0.0 if close <= 0 else _body_size(row) / close * 100.0


def _is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


def _range_position(row: pd.Series) -> float:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    rng = high - low
    return 0.5 if rng <= 0 else (close - low) / rng


def _wick_to_body_ratio(row: pd.Series, direction: str) -> float:
    body = _body_size(row)
    if body <= 0:
        return 999.0

    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])
    close = float(row["close"])

    if direction == "LONG":
        against_wick = min(open_price, close) - low
    else:
        against_wick = high - max(open_price, close)

    return max(0.0, against_wick) / body


def _distance_pct(price: float, reference: float) -> float:
    if reference <= 0:
        return 999.0
    return abs(price - reference) / reference * 100.0


def _avg_body(df: pd.DataFrame, pos: int, period: int = 20) -> float:
    subset = df.iloc[max(0, pos - period):pos]
    if subset.empty:
        return 0.0
    return float((subset["close"] - subset["open"]).abs().mean())


def _avg_volume(df: pd.DataFrame, pos: int, period: int) -> float:
    subset = df.iloc[max(0, pos - period):pos]
    if subset.empty or "volume" not in subset.columns:
        return 0.0
    return float(subset["volume"].mean())


def _to_iso(ts) -> str:
    if hasattr(ts, "to_pydatetime"):
        return ts.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc).isoformat() if ts.tzinfo is None else ts.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


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

    msg = f"[ENTRY-REASON] {_setup_label(setup)} | {reason}"
    if details:
        msg += f" | {details}"
    logger.info(msg)


# ── swings / structure ────────────────────────────────────────────

def _find_swings(df: pd.DataFrame, left: int, right: int) -> list[dict]:
    swings: list[dict] = []

    if len(df) < left + right + 5:
        return swings

    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    for i in range(left, len(df) - right):
        high = float(highs.iloc[i])
        low = float(lows.iloc[i])

        if high > float(highs.iloc[i - left:i].max()) and high > float(highs.iloc[i + 1:i + right + 1].max()):
            swings.append({"type": "HIGH", "pos": i, "time": df.index[i], "price": high})

        if low < float(lows.iloc[i - left:i].min()) and low < float(lows.iloc[i + 1:i + right + 1].min()):
            swings.append({"type": "LOW", "pos": i, "time": df.index[i], "price": low})

    swings.sort(key=lambda item: item["pos"])
    return swings


def _last_swing_before(swings: list[dict], swing_type: str, pos: int) -> dict | None:
    candidates = [s for s in swings if s["type"] == swing_type and s["pos"] < pos]
    return candidates[-1] if candidates else None


def _find_target_swing(swings: list[dict], direction: str, pos: int, reference_price: float) -> dict | None:
    if direction == "LONG":
        candidates = [
            s for s in swings
            if s["type"] == "HIGH" and s["pos"] < pos and s["price"] > reference_price
        ]
    else:
        candidates = [
            s for s in swings
            if s["type"] == "LOW" and s["pos"] < pos and s["price"] < reference_price
        ]

    return candidates[-1] if candidates else None


# ── 1h regime ─────────────────────────────────────────────────────

def _get_regime_bias(symbol: str) -> tuple[str | None, dict]:
    raw = _ensure_df(
        get_market_klines(
            symbol,
            REGIME_TF,
            count=REGIME_KLINE_COUNT,
            allow_rest_fallback=REST_FALLBACK_ENABLED,
        )
    )

    min_count = max(REGIME_EMA_SLOW + 5, 80)
    if raw.empty or len(raw) < min_count:
        return None, {"reason": "regime_data_not_ready"}

    df = raw.iloc[:-1].copy()
    df["ema_fast"] = _ema(df["close"], REGIME_EMA_FAST)
    df["ema_slow"] = _ema(df["close"], REGIME_EMA_SLOW)

    last = df.iloc[-1]
    prev = df.iloc[-4:-1]

    close = float(last["close"])
    ema_fast = float(last["ema_fast"])
    ema_slow = float(last["ema_slow"])

    if close <= 0 or ema_fast <= 0 or ema_slow <= 0:
        return None, {"reason": "regime_invalid_values"}

    fast_slope = ema_fast - float(df["ema_fast"].iloc[-4])

    if close > ema_fast > ema_slow and fast_slope > 0:
        return "LONG", {
            "regime_close": close,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "slope": fast_slope,
            "time": _to_iso(df.index[-1]),
        }

    if close < ema_fast < ema_slow and fast_slope < 0:
        return "SHORT", {
            "regime_close": close,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "slope": fast_slope,
            "time": _to_iso(df.index[-1]),
        }

    return None, {
        "reason": "regime_not_aligned",
        "close": close,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "slope": fast_slope,
    }


# ── 15m setup detection helpers ───────────────────────────────────

def _detect_sell_side_sweep(df: pd.DataFrame, swings: list[dict], pos: int) -> dict | None:
    prev_low = _last_swing_before(swings, "LOW", pos)
    if not prev_low:
        return None

    row = df.iloc[pos]
    low = float(row["low"])
    close = float(row["close"])

    if low < prev_low["price"] and close > prev_low["price"]:
        return {
            "type": "SELL_SIDE_SWEEP",
            "swing": prev_low,
            "pos": pos,
            "time": df.index[pos],
            "level": prev_low["price"],
            "extreme": low,
        }

    return None


def _detect_buy_side_sweep(df: pd.DataFrame, swings: list[dict], pos: int) -> dict | None:
    prev_high = _last_swing_before(swings, "HIGH", pos)
    if not prev_high:
        return None

    row = df.iloc[pos]
    high = float(row["high"])
    close = float(row["close"])

    if high > prev_high["price"] and close < prev_high["price"]:
        return {
            "type": "BUY_SIDE_SWEEP",
            "swing": prev_high,
            "pos": pos,
            "time": df.index[pos],
            "level": prev_high["price"],
            "extreme": high,
        }

    return None


def _is_bullish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos)
    if avg_body <= 0:
        return False

    return (
        _is_bullish(row)
        and _body_size(row) >= avg_body * DISPLACEMENT_BODY_MULTIPLIER
        and _range_position(row) >= DISPLACEMENT_CLOSE_POSITION
    )


def _is_bearish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos)
    if avg_body <= 0:
        return False

    return (
        _is_bearish(row)
        and _body_size(row) >= avg_body * DISPLACEMENT_BODY_MULTIPLIER
        and _range_position(row) <= (1.0 - DISPLACEMENT_CLOSE_POSITION)
    )


def _find_bullish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)

    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]
        if _is_bearish(row):
            return {
                "type": "BULLISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": float(row["low"]),
                "zone_high": max(float(row["open"]), float(row["close"])),
            }

    return None


def _find_bearish_ob(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)

    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]
        if _is_bullish(row):
            return {
                "type": "BEARISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": min(float(row["open"]), float(row["close"])),
                "zone_high": float(row["high"]),
            }

    return None


def _find_bullish_fvg(df: pd.DataFrame, sweep_pos: int, displacement_pos: int) -> dict | None:
    start = max(sweep_pos + 1, displacement_pos - FVG_LOOKBACK_AFTER_SWEEP)
    end = displacement_pos

    for i in range(end, start - 1, -1):
        if i < 2:
            continue

        prev2_high = float(df["high"].iloc[i - 2])
        cur_low = float(df["low"].iloc[i])

        if cur_low > prev2_high:
            return {
                "type": "BULLISH_FVG",
                "pos": i,
                "time": df.index[i],
                "zone_low": prev2_high,
                "zone_high": cur_low,
            }

    return None


def _find_bearish_fvg(df: pd.DataFrame, sweep_pos: int, displacement_pos: int) -> dict | None:
    start = max(sweep_pos + 1, displacement_pos - FVG_LOOKBACK_AFTER_SWEEP)
    end = displacement_pos

    for i in range(end, start - 1, -1):
        if i < 2:
            continue

        prev2_low = float(df["low"].iloc[i - 2])
        cur_high = float(df["high"].iloc[i])

        if cur_high < prev2_low:
            return {
                "type": "BEARISH_FVG",
                "pos": i,
                "time": df.index[i],
                "zone_low": cur_high,
                "zone_high": prev2_low,
            }

    return None


def _merge_zones(ob: dict, fvg: dict | None, direction: str) -> dict | None:
    if fvg is None:
        if REQUIRE_FVG_CONFIRMATION:
            return None
        return {
            "type": ob["type"],
            "zone_low": ob["zone_low"],
            "zone_high": ob["zone_high"],
            "time": ob["time"],
        }

    # Prefer the overlap between OB and FVG. If no overlap, use the FVG because
    # it normally represents the displacement inefficiency that price retests.
    overlap_low = max(float(ob["zone_low"]), float(fvg["zone_low"]))
    overlap_high = min(float(ob["zone_high"]), float(fvg["zone_high"]))

    if overlap_low < overlap_high:
        return {
            "type": f"{ob['type']}+{fvg['type']}_OVERLAP",
            "zone_low": overlap_low,
            "zone_high": overlap_high,
            "time": fvg["time"],
        }

    return {
        "type": f"{ob['type']}+{fvg['type']}",
        "zone_low": float(fvg["zone_low"]),
        "zone_high": float(fvg["zone_high"]),
        "time": fvg["time"],
    }


def _calculate_setup_prices(direction: str, zone: dict, sweep: dict, target_swing: dict | None, atr_value: float) -> tuple[float, float, float] | None:
    zone_mid = (float(zone["zone_low"]) + float(zone["zone_high"])) / 2.0
    if zone_mid <= 0 or atr_value <= 0:
        return None

    atr_buffer = atr_value * ATR_SL_BUFFER_MULTIPLIER

    if direction == "LONG":
        sl_price = min(float(sweep["extreme"]), float(zone["zone_low"]) - atr_buffer)
        risk = zone_mid - sl_price
        if target_swing and target_swing["price"] > zone_mid:
            target_price = target_swing["price"]
        else:
            target_price = zone_mid + risk * TARGET_RR
        reward = target_price - zone_mid
    else:
        sl_price = max(float(sweep["extreme"]), float(zone["zone_high"]) + atr_buffer)
        risk = sl_price - zone_mid
        if target_swing and target_swing["price"] < zone_mid:
            target_price = target_swing["price"]
        else:
            target_price = zone_mid - risk * TARGET_RR
        reward = zone_mid - target_price

    if risk <= 0 or reward <= 0:
        return None

    sl_pct = risk / zone_mid * 100.0
    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None

    rr = reward / risk
    if rr < MIN_RR:
        return None

    if rr > MAX_RR:
        rr = MAX_RR
        if direction == "LONG":
            target_price = zone_mid + risk * rr
        else:
            target_price = zone_mid - risk * rr

    return round(sl_price, 8), round(target_price, 8), round(rr, 2)


def _score_setup(rr: float, zone_age: int, sweep_age: int, vol_ratio: float, has_fvg: bool, regime_details: dict) -> float:
    score = 45.0

    score += 14.0  # mandatory regime alignment

    if has_fvg:
        score += 12.0

    if rr >= 2.0:
        score += 10.0
    elif rr >= MIN_RR:
        score += 6.0

    if zone_age <= 8:
        score += 8.0
    elif zone_age <= 16:
        score += 5.0

    if sweep_age <= 10:
        score += 6.0
    elif sweep_age <= 20:
        score += 3.0

    if vol_ratio >= 1.50:
        score += 8.0
    elif vol_ratio >= MIN_VOLUME_MULTIPLIER:
        score += 5.0

    ema_fast = float(regime_details.get("ema_fast", 0.0))
    ema_slow = float(regime_details.get("ema_slow", 0.0))
    if ema_fast > 0 and ema_slow > 0:
        ema_gap = _distance_pct(ema_fast, ema_slow)
        if ema_gap >= 0.35:
            score += 5.0
        elif ema_gap >= 0.15:
            score += 2.0

    return round(max(0.0, min(score, 100.0)), 1)


# ── setup detection ───────────────────────────────────────────────

def detect_setup(symbol: str) -> dict | None:
    try:
        regime_bias, regime_details = _get_regime_bias(symbol)

        if regime_bias is None:
            logger.debug(
                "[SETUP-REASON] %s rejected | regime_failed reason=%s",
                symbol,
                regime_details.get("reason"),
            )
            return None

        raw = _ensure_df(
            get_market_klines(
                symbol,
                SETUP_TF,
                count=SETUP_KLINE_COUNT,
                allow_rest_fallback=REST_FALLBACK_ENABLED,
            )
        )

        if raw.empty or len(raw) < SETUP_LOOKBACK // 2:
            logger.debug("[SETUP-REASON] %s rejected | setup_data_not_ready", symbol)
            return None

        completed = raw.iloc[:-1].copy().tail(SETUP_LOOKBACK)
        completed["atr"] = _atr(completed, ATR_PERIOD)

        swings = _find_swings(completed, SWING_LEFT, SWING_RIGHT)
        if len(swings) < 4:
            logger.debug("[SETUP-REASON] %s rejected | not_enough_swings", symbol)
            return None

        start = max(30, SWEEP_LOOKBACK + ORDER_BLOCK_LOOKBACK + 5)
        last_possible = len(completed) - 2

        best_setup = None
        best_score = -1.0

        for displacement_pos in range(start, last_possible + 1):
            if regime_bias == "LONG" and not _is_bullish_displacement(completed, displacement_pos):
                continue

            if regime_bias == "SHORT" and not _is_bearish_displacement(completed, displacement_pos):
                continue

            row = completed.iloc[displacement_pos]
            avg_vol = _avg_volume(completed, displacement_pos, AVG_VOLUME_PERIOD)
            volume = float(row.get("volume", 0.0))
            vol_ratio = volume / avg_vol if avg_vol > 0 else 0.0

            if REQUIRE_VOLUME_CONFIRMATION and avg_vol > 0 and vol_ratio < MIN_VOLUME_MULTIPLIER:
                continue

            sweep = None
            sweep_start = max(0, displacement_pos - SWEEP_LOOKBACK)

            for sweep_pos in range(displacement_pos - 1, sweep_start - 1, -1):
                if regime_bias == "LONG":
                    sweep = _detect_sell_side_sweep(completed, swings, sweep_pos)
                else:
                    sweep = _detect_buy_side_sweep(completed, swings, sweep_pos)

                if sweep:
                    break

            if not sweep:
                continue

            ob = _find_bullish_ob(completed, displacement_pos) if regime_bias == "LONG" else _find_bearish_ob(completed, displacement_pos)
            if not ob:
                continue

            fvg = _find_bullish_fvg(completed, sweep["pos"], displacement_pos) if regime_bias == "LONG" else _find_bearish_fvg(completed, sweep["pos"], displacement_pos)
            zone = _merge_zones(ob, fvg, regime_bias)

            if not zone:
                continue

            zone_mid = (zone["zone_low"] + zone["zone_high"]) / 2.0
            target_swing = _find_target_swing(swings, regime_bias, displacement_pos, zone_mid)
            atr_value = float(completed["atr"].iloc[displacement_pos])

            prices = _calculate_setup_prices(regime_bias, zone, sweep, target_swing, atr_value)
            if not prices:
                continue

            sl_price, target_price, rr_estimate = prices

            zone_age = len(completed) - displacement_pos
            sweep_age = len(completed) - sweep["pos"]
            score = _score_setup(
                rr=rr_estimate,
                zone_age=zone_age,
                sweep_age=sweep_age,
                vol_ratio=vol_ratio,
                has_fvg=fvg is not None,
                regime_details=regime_details,
            )

            if score < MIN_SIGNAL_SCORE or score <= best_score:
                continue

            setup_time = completed.index[displacement_pos]
            expires_at = datetime.now(timezone.utc) + timedelta(
                minutes=RETEST_MAX_CANDLES * CANDLE_MINUTES
            )

            best_score = score
            best_setup = {
                "symbol": symbol,
                "direction": regime_bias,
                "trend_tf": REGIME_TF,
                "entry_tf": EXECUTION_TF,
                "bias": regime_bias,
                "bias_break": regime_details.get("ema_fast"),

                "sweep_type": sweep["type"],
                "sweep_level": sweep["level"],
                "sweep_extreme": sweep["extreme"],
                "sweep_time": _to_iso(sweep["time"]),

                "ob_type": zone["type"],
                "ob_low": round(float(zone["zone_low"]), 8),
                "ob_high": round(float(zone["zone_high"]), 8),
                "ob_time": _to_iso(zone["time"]),

                "target_price": target_price,
                "sl_price": sl_price,
                "rr_estimate": rr_estimate,
                "score": score,
                "setup_time": _to_iso(setup_time),
                "expires_at": expires_at.isoformat(),
            }

        if best_setup:
            logger.info(
                "[SETUP] %s %s | regime=%s setup=%s exec=%s zone=%s-%s type=%s rr=%s score=%s",
                best_setup["direction"],
                symbol,
                REGIME_TF,
                SETUP_TF,
                EXECUTION_TF,
                _fmt(best_setup["ob_low"]),
                _fmt(best_setup["ob_high"]),
                best_setup["ob_type"],
                best_setup["rr_estimate"],
                best_setup["score"],
            )

        return best_setup

    except Exception as e:
        logger.error("Error detecting %s setup for %s: %s", STRATEGY_NAME, symbol, e, exc_info=True)
        return None


# ── 5m execution ──────────────────────────────────────────────────

def _touches_zone(row: pd.Series, zone_low: float, zone_high: float) -> bool:
    return float(row["low"]) <= zone_high and float(row["high"]) >= zone_low


def _minor_bos_confirmed(df_after_setup: pd.DataFrame, trigger_pos: int, direction: str) -> bool:
    if trigger_pos < 3:
        return False

    lookback = df_after_setup.iloc[max(0, trigger_pos - 6):trigger_pos]
    if len(lookback) < 3:
        return False

    trigger = df_after_setup.iloc[trigger_pos]

    if direction == "LONG":
        return float(trigger["close"]) > float(lookback["high"].max())

    return float(trigger["close"]) < float(lookback["low"].min())


def _calculate_final_prices(direction: str, entry: float, setup: dict, trigger: pd.Series, atr_value: float) -> tuple[float, float, float, float, float] | None:
    if entry <= 0 or atr_value <= 0:
        return None

    zone_low = float(setup["ob_low"])
    zone_high = float(setup["ob_high"])
    target_price = float(setup["target_price"])
    base_sl = float(setup["sl_price"])
    atr_buffer = atr_value * ATR_SL_BUFFER_MULTIPLIER

    if direction == "LONG":
        sl_price = min(base_sl, float(trigger["low"]) - atr_buffer, zone_low - atr_buffer)
        risk = entry - sl_price
        reward_to_structure = target_price - entry
    else:
        sl_price = max(base_sl, float(trigger["high"]) + atr_buffer, zone_high + atr_buffer)
        risk = sl_price - entry
        reward_to_structure = entry - target_price

    if risk <= 0:
        return None

    sl_pct = risk / entry * 100.0
    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None

    rr = reward_to_structure / risk if reward_to_structure > 0 else 0.0

    if rr < MIN_RR:
        rr = TARGET_RR
        if direction == "LONG":
            target_price = entry + risk * rr
        else:
            target_price = entry - risk * rr
    elif rr > MAX_RR:
        rr = MAX_RR
        if direction == "LONG":
            target_price = entry + risk * rr
        else:
            target_price = entry - risk * rr

    if direction == "LONG":
        tp_move_pct = (target_price - entry) / entry * 100.0
        sl_move_pct = (entry - sl_price) / entry * 100.0
    else:
        tp_move_pct = (entry - target_price) / entry * 100.0
        sl_move_pct = (sl_price - entry) / entry * 100.0

    tp_roi = tp_move_pct * LEVERAGE
    sl_roi = sl_move_pct * LEVERAGE

    if tp_roi < MIN_TP_ROI_PCT or tp_roi > MAX_TP_ROI_PCT:
        return None

    if sl_roi < MIN_SL_ROI_PCT or sl_roi > MAX_SL_ROI_PCT:
        return None

    return (
        round(target_price, 8),
        round(sl_price, 8),
        round(tp_roi, 1),
        round(sl_roi, 1),
        round(rr, 2),
    )


def _entry_candle_valid(df_after_setup: pd.DataFrame, pos: int, setup: dict) -> tuple[bool, str]:
    trigger = df_after_setup.iloc[pos]
    direction = setup["direction"]
    zone_low = float(setup["ob_low"])
    zone_high = float(setup["ob_high"])
    zone_mid = (zone_low + zone_high) / 2.0

    close = float(trigger["close"])
    ema = float(trigger.get("ema", 0.0))
    vwap = float(trigger.get("vwap", 0.0))

    if not _touches_zone(trigger, zone_low, zone_high):
        return False, "price_not_in_zone"

    if _distance_pct(close, zone_mid) > MAX_ENTRY_DISTANCE_FROM_ZONE_PCT:
        return False, "entry_too_far_from_zone"

    if vwap > 0 and _distance_pct(close, vwap) > MAX_DISTANCE_FROM_VWAP_PCT:
        return False, "entry_too_far_from_vwap"

    if _wick_to_body_ratio(trigger, direction) > MAX_WICK_TO_BODY_RATIO:
        return False, "against_wick_too_large"

    if direction == "LONG":
        if not _is_bullish(trigger):
            return False, "entry_candle_not_bullish"
        if close < zone_mid:
            return False, "close_not_above_zone_mid"
        if ema > 0 and close < ema:
            return False, "close_below_ema"
        if vwap > 0 and close < vwap:
            return False, "close_below_vwap"
        if _range_position(trigger) < MIN_RETEST_REJECTION_POSITION:
            return False, "weak_bullish_rejection"
    else:
        if not _is_bearish(trigger):
            return False, "entry_candle_not_bearish"
        if close > zone_mid:
            return False, "close_not_below_zone_mid"
        if ema > 0 and close > ema:
            return False, "close_above_ema"
        if vwap > 0 and close > vwap:
            return False, "close_above_vwap"
        if _range_position(trigger) > (1.0 - MIN_RETEST_REJECTION_POSITION):
            return False, "weak_bearish_rejection"

    if REQUIRE_MINOR_BOS_AFTER_RETEST and not _minor_bos_confirmed(df_after_setup, pos, direction):
        return False, "minor_bos_not_confirmed"

    return True, "entry_confirmed"


def evaluate_pending_setup(setup: dict) -> tuple[str, Signal | None]:
    try:
        now = datetime.now(timezone.utc)
        expires_at = _parse_utc(setup["expires_at"])

        if now >= expires_at:
            _log_monitor_reason(setup, "EXPIRED_RETEST_TIMEOUT", force=True)
            return "EXPIRED", None

        symbol = setup["symbol"]
        direction = setup["direction"]
        zone_low = float(setup["ob_low"])
        zone_high = float(setup["ob_high"])
        sl_price = float(setup["sl_price"])
        setup_time = _to_naive_utc(_parse_utc(setup["setup_time"]))

        regime_bias, regime_details = _get_regime_bias(symbol)
        if regime_bias != direction:
            _log_monitor_reason(
                setup,
                "INVALIDATED_REGIME_CHANGED",
                f"setup_direction={direction} current_regime={regime_bias}",
                force=True,
            )
            return "INVALIDATED", None

        raw = _ensure_df(
            get_market_klines(
                symbol,
                EXECUTION_TF,
                count=MONITOR_KLINE_COUNT,
                allow_rest_fallback=REST_FALLBACK_ENABLED,
            )
        )

        if raw.empty or len(raw) < EMA_PERIOD + ATR_PERIOD + 5:
            _log_monitor_reason(setup, "WAIT_EXECUTION_DATA_NOT_READY")
            return "WAIT", None

        completed = raw.iloc[:-1].copy()
        df = _prepare_execution_df(completed)

        after_setup = df[df.index > setup_time].copy()
        if after_setup.empty:
            _log_monitor_reason(setup, "WAIT_NO_5M_CANDLE_AFTER_SETUP")
            return "WAIT", None

        after_setup = after_setup.tail(RETEST_MAX_CANDLES)

        last = after_setup.iloc[-1]
        if direction == "LONG" and float(last["low"]) <= sl_price:
            _log_monitor_reason(
                setup,
                "INVALIDATED_STRUCTURE_SL_TOUCHED_BEFORE_ENTRY",
                f"low={_fmt(last['low'])} sl={_fmt(sl_price)}",
                force=True,
            )
            return "INVALIDATED", None

        if direction == "SHORT" and float(last["high"]) >= sl_price:
            _log_monitor_reason(
                setup,
                "INVALIDATED_STRUCTURE_SL_TOUCHED_BEFORE_ENTRY",
                f"high={_fmt(last['high'])} sl={_fmt(sl_price)}",
                force=True,
            )
            return "INVALIDATED", None

        trigger_pos = len(after_setup) - 1
        valid, reason = _entry_candle_valid(after_setup, trigger_pos, setup)

        if not valid:
            _log_monitor_reason(
                setup,
                f"WAIT_{reason.upper()}",
                f"close={_fmt(last['close'])} zone={_fmt(zone_low)}-{_fmt(zone_high)}",
            )
            return "WAIT", None

        entry = float(last["close"])
        atr_value = float(last.get("atr", 0.0))

        prices = _calculate_final_prices(
            direction=direction,
            entry=entry,
            setup=setup,
            trigger=last,
            atr_value=atr_value,
        )

        if not prices:
            _log_monitor_reason(
                setup,
                "WAIT_PRICE_MODEL_FAILED",
                f"entry={_fmt(entry)} atr={_fmt(atr_value)} zone={_fmt(zone_low)}-{_fmt(zone_high)}",
            )
            return "WAIT", None

        tp_price, final_sl_price, tp_roi_pct, sl_roi_pct, rr = prices
        score = min(float(setup["score"]) + 4.0, 100.0)

        _log_monitor_reason(
            setup,
            "FIRE_5M_RETEST_CONFIRMED",
            f"entry={_fmt(entry)} tp={_fmt(tp_price)} sl={_fmt(final_sl_price)} rr={rr} score={score}",
            force=True,
        )

        return "FIRE", Signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            tp_price=tp_price,
            sl_price=final_sl_price,
            leverage=LEVERAGE,
            tp_roi_pct=tp_roi_pct,
            sl_roi_pct=sl_roi_pct,
            timeframe_summary=(
                f"HTF SMC | {REGIME_TF} regime | {SETUP_TF} sweep+FVG/OB | "
                f"{EXECUTION_TF} retest+BOS | RR {rr:g}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error("Error evaluating setup %s: %s", setup.get("id"), e, exc_info=True)
        return "WAIT", None


def analyze_coin(symbol: str) -> Signal | None:
    return None
