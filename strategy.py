"""
SMC / Market Structure Scalping Strategy.

This strategy does NOT use classic indicators like:
    Nadaraya
    Supertrend
    EMA
    VWAP
    RSI
    ADX

Structure:
    15m = market structure bias
    5m  = liquidity sweep + displacement + order block retest

LONG setup:
    1. 15m market structure bias is bullish
    2. 5m sell-side liquidity sweep happens
       - price takes previous swing low
       - candle closes back above that swing low
    3. Bullish displacement candle appears
    4. Bullish order block is detected
       - last bearish candle before displacement
    5. Price retests/mitigates order block
    6. Retest candle closes bullish away from zone
    7. SL below sweep low / OB low
    8. TP at next buy-side liquidity / swing high
    9. RR must be >= MIN_STRUCTURE_RR

SHORT setup:
    1. 15m market structure bias is bearish
    2. 5m buy-side liquidity sweep happens
       - price takes previous swing high
       - candle closes back below that swing high
    3. Bearish displacement candle appears
    4. Bearish order block is detected
       - last bullish candle before displacement
    5. Price retests/mitigates order block
    6. Retest candle closes bearish away from zone
    7. SL above sweep high / OB high
    8. TP at next sell-side liquidity / swing low
    9. RR must be >= MIN_STRUCTURE_RR
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from mexc_client import get_klines
from config import (
    TREND_TF,
    ENTRY_TF,
    TREND_KLINE_COUNT,
    ENTRY_KLINE_COUNT,
    SWING_LEFT,
    SWING_RIGHT,
    STRUCTURE_LOOKBACK,
    ENTRY_LOOKBACK,
    SWEEP_LOOKBACK,
    AVG_BODY_PERIOD,
    DISPLACEMENT_BODY_MULTIPLIER,
    DISPLACEMENT_CLOSE_POSITION,
    ORDER_BLOCK_LOOKBACK,
    RETEST_LOOKBACK_AFTER_DISPLACEMENT,
    MAX_SIGNAL_CANDLE_BODY_PCT,
    MIN_STRUCTURE_RR,
    MAX_STRUCTURE_RR,
    SL_BUFFER_PCT,
    TP_BUFFER_PCT,
    MIN_SL_PCT,
    MAX_SL_PCT,
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


# ── General helpers ───────────────────────────────────────────────

def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return abs(a - b) / abs(b) * 100.0


def _body_size(row: pd.Series) -> float:
    return abs(float(row["close"]) - float(row["open"]))


def _range_size(row: pd.Series) -> float:
    return max(float(row["high"]) - float(row["low"]), 0.0)


def _is_bullish(row: pd.Series) -> bool:
    return float(row["close"]) > float(row["open"])


def _is_bearish(row: pd.Series) -> bool:
    return float(row["close"]) < float(row["open"])


def _close_position(row: pd.Series) -> float:
    """
    Return close position inside candle range:
        0.0 = close near low
        1.0 = close near high
    """
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

    bodies = (subset["close"].astype(float) - subset["open"].astype(float)).abs()
    return float(bodies.mean())


def _body_pct(row: pd.Series) -> float:
    close = float(row["close"])
    if close <= 0:
        return 0.0
    return _body_size(row) / close * 100.0


# ── Swing detection ───────────────────────────────────────────────

def _find_swings(df: pd.DataFrame, left: int, right: int) -> list[dict]:
    """
    Confirmed swing detection.

    Swing high:
        candle high is greater than highs on left and right side

    Swing low:
        candle low is lower than lows on left and right side
    """
    swings: list[dict] = []

    if len(df) < left + right + 5:
        return swings

    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    for i in range(left, len(df) - right):
        high = float(highs.iloc[i])
        low = float(lows.iloc[i])

        left_high = float(highs.iloc[i - left:i].max())
        right_high = float(highs.iloc[i + 1:i + right + 1].max())

        left_low = float(lows.iloc[i - left:i].min())
        right_low = float(lows.iloc[i + 1:i + right + 1].min())

        if high > left_high and high > right_high:
            swings.append({
                "type": "HIGH",
                "pos": i,
                "time": df.index[i],
                "price": high,
            })

        if low < left_low and low < right_low:
            swings.append({
                "type": "LOW",
                "pos": i,
                "time": df.index[i],
                "price": low,
            })

    swings.sort(key=lambda x: x["pos"])
    return swings


def _last_swing_before(swings: list[dict], swing_type: str, pos: int) -> dict | None:
    candidates = [
        swing for swing in swings
        if swing["type"] == swing_type and swing["pos"] < pos
    ]

    if not candidates:
        return None

    return candidates[-1]


def _next_swing_after(swings: list[dict], swing_type: str, pos: int, entry: float, direction: str) -> dict | None:
    candidates = [
        swing for swing in swings
        if swing["type"] == swing_type and swing["pos"] > pos
    ]

    if direction == "LONG":
        candidates = [s for s in candidates if s["price"] > entry]
    else:
        candidates = [s for s in candidates if s["price"] < entry]

    if not candidates:
        return None

    return candidates[0]


# ── 15m market structure bias ─────────────────────────────────────

def _get_market_structure_bias(trend_df: pd.DataFrame) -> tuple[str | None, dict]:
    """
    Determine market structure bias using recent BOS.

    Bullish BOS:
        close breaks above previous confirmed swing high

    Bearish BOS:
        close breaks below previous confirmed swing low
    """
    completed = trend_df.iloc[:-1].copy()

    if len(completed) < STRUCTURE_LOOKBACK // 2:
        return None, {}

    recent = completed.tail(STRUCTURE_LOOKBACK).copy()
    swings = _find_swings(recent, SWING_LEFT, SWING_RIGHT)

    if len(swings) < 4:
        return None, {}

    last_bias = None
    last_break_price = None
    last_break_time = None

    for i in range(SWING_LEFT + SWING_RIGHT + 2, len(recent)):
        close = float(recent["close"].iloc[i])

        previous_high = _last_swing_before(swings, "HIGH", i)
        previous_low = _last_swing_before(swings, "LOW", i)

        if previous_high and close > previous_high["price"]:
            last_bias = "LONG"
            last_break_price = previous_high["price"]
            last_break_time = recent.index[i]

        if previous_low and close < previous_low["price"]:
            last_bias = "SHORT"
            last_break_price = previous_low["price"]
            last_break_time = recent.index[i]

    if last_bias is None:
        return None, {}

    details = {
        "bias": last_bias,
        "break_price": last_break_price,
        "break_time": last_break_time,
        "swing_count": len(swings),
    }

    return last_bias, details


# ── Sweep detection ───────────────────────────────────────────────

def _detect_sell_side_sweep(df: pd.DataFrame, swings: list[dict], pos: int) -> dict | None:
    """
    Sell-side liquidity sweep for LONG:
        low takes previous swing low
        close reclaims above that swing low
    """
    row = df.iloc[pos]
    prev_low = _last_swing_before(swings, "LOW", pos)

    if not prev_low:
        return None

    low = float(row["low"])
    close = float(row["close"])

    if low < prev_low["price"] and close > prev_low["price"]:
        return {
            "type": "SELL_SIDE_SWEEP",
            "swing": prev_low,
            "sweep_pos": pos,
            "sweep_time": df.index[pos],
            "sweep_level": prev_low["price"],
            "sweep_extreme": low,
        }

    return None


def _detect_buy_side_sweep(df: pd.DataFrame, swings: list[dict], pos: int) -> dict | None:
    """
    Buy-side liquidity sweep for SHORT:
        high takes previous swing high
        close rejects below that swing high
    """
    row = df.iloc[pos]
    prev_high = _last_swing_before(swings, "HIGH", pos)

    if not prev_high:
        return None

    high = float(row["high"])
    close = float(row["close"])

    if high > prev_high["price"] and close < prev_high["price"]:
        return {
            "type": "BUY_SIDE_SWEEP",
            "swing": prev_high,
            "sweep_pos": pos,
            "sweep_time": df.index[pos],
            "sweep_level": prev_high["price"],
            "sweep_extreme": high,
        }

    return None


# ── Displacement / Order Block ────────────────────────────────────

def _is_bullish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)
    body = _body_size(row)

    if avg_body <= 0:
        return False

    return (
        _is_bullish(row)
        and body >= avg_body * DISPLACEMENT_BODY_MULTIPLIER
        and _close_position(row) >= DISPLACEMENT_CLOSE_POSITION
    )


def _is_bearish_displacement(df: pd.DataFrame, pos: int) -> bool:
    row = df.iloc[pos]
    avg_body = _avg_body(df, pos, AVG_BODY_PERIOD)
    body = _body_size(row)

    if avg_body <= 0:
        return False

    return (
        _is_bearish(row)
        and body >= avg_body * DISPLACEMENT_BODY_MULTIPLIER
        and _close_position(row) <= (1.0 - DISPLACEMENT_CLOSE_POSITION)
    )


def _find_bullish_order_block(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    """
    Bullish OB:
        last bearish candle before bullish displacement

    Zone:
        low to open of the bearish candle
    """
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)

    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]

        if _is_bearish(row):
            zone_low = float(row["low"])
            zone_high = float(row["open"])

            if zone_high <= zone_low:
                zone_high = max(float(row["open"]), float(row["close"]))

            return {
                "type": "BULLISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": zone_low,
                "zone_high": zone_high,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }

    return None


def _find_bearish_order_block(df: pd.DataFrame, displacement_pos: int) -> dict | None:
    """
    Bearish OB:
        last bullish candle before bearish displacement

    Zone:
        open to high of the bullish candle
    """
    start = max(0, displacement_pos - ORDER_BLOCK_LOOKBACK)

    for i in range(displacement_pos - 1, start - 1, -1):
        row = df.iloc[i]

        if _is_bullish(row):
            zone_low = float(row["open"])
            zone_high = float(row["high"])

            if zone_high <= zone_low:
                zone_low = min(float(row["open"]), float(row["close"]))

            return {
                "type": "BEARISH_OB",
                "pos": i,
                "time": df.index[i],
                "zone_low": zone_low,
                "zone_high": zone_high,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }

    return None


# ── Retest / Entry validation ─────────────────────────────────────

def _touches_zone(row: pd.Series, zone_low: float, zone_high: float) -> bool:
    high = float(row["high"])
    low = float(row["low"])

    return low <= zone_high and high >= zone_low


def _find_bullish_retest(df: pd.DataFrame, ob: dict, displacement_pos: int) -> dict | None:
    """
    Bullish retest:
        candle touches bullish OB zone
        candle closes bullish
        candle closes above OB midpoint
    """
    start = displacement_pos + 1
    end = min(len(df), displacement_pos + RETEST_LOOKBACK_AFTER_DISPLACEMENT + 1)

    zone_low = ob["zone_low"]
    zone_high = ob["zone_high"]
    midpoint = (zone_low + zone_high) / 2.0

    for i in range(start, end):
        row = df.iloc[i]

        if not _touches_zone(row, zone_low, zone_high):
            continue

        if _body_pct(row) > MAX_SIGNAL_CANDLE_BODY_PCT:
            continue

        if _is_bullish(row) and float(row["close"]) > midpoint:
            return {
                "pos": i,
                "time": df.index[i],
                "entry": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "body_pct": _body_pct(row),
            }

    return None


def _find_bearish_retest(df: pd.DataFrame, ob: dict, displacement_pos: int) -> dict | None:
    """
    Bearish retest:
        candle touches bearish OB zone
        candle closes bearish
        candle closes below OB midpoint
    """
    start = displacement_pos + 1
    end = min(len(df), displacement_pos + RETEST_LOOKBACK_AFTER_DISPLACEMENT + 1)

    zone_low = ob["zone_low"]
    zone_high = ob["zone_high"]
    midpoint = (zone_low + zone_high) / 2.0

    for i in range(start, end):
        row = df.iloc[i]

        if not _touches_zone(row, zone_low, zone_high):
            continue

        if _body_pct(row) > MAX_SIGNAL_CANDLE_BODY_PCT:
            continue

        if _is_bearish(row) and float(row["close"]) < midpoint:
            return {
                "pos": i,
                "time": df.index[i],
                "entry": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "body_pct": _body_pct(row),
            }

    return None


# ── Structure SL / TP ─────────────────────────────────────────────

def _calculate_structure_prices(
    direction: str,
    entry: float,
    sweep: dict,
    ob: dict,
    retest: dict,
    swings: list[dict],
) -> tuple[float, float, float, float, float] | None:
    """
    Returns:
        tp_price, sl_price, tp_roi_pct, sl_roi_pct, rr
    """
    if entry <= 0:
        return None

    if direction == "LONG":
        raw_sl = min(sweep["sweep_extreme"], ob["zone_low"])
        sl_price = raw_sl * (1.0 - SL_BUFFER_PCT / 100.0)

        target_swing = _next_swing_after(
            swings=swings,
            swing_type="HIGH",
            pos=retest["pos"],
            entry=entry,
            direction=direction,
        )

        if target_swing:
            tp_price = target_swing["price"] * (1.0 - TP_BUFFER_PCT / 100.0)
        else:
            tp_price = entry + (entry - sl_price) * MIN_STRUCTURE_RR

        risk = entry - sl_price
        reward = tp_price - entry

    else:
        raw_sl = max(sweep["sweep_extreme"], ob["zone_high"])
        sl_price = raw_sl * (1.0 + SL_BUFFER_PCT / 100.0)

        target_swing = _next_swing_after(
            swings=swings,
            swing_type="LOW",
            pos=retest["pos"],
            entry=entry,
            direction=direction,
        )

        if target_swing:
            tp_price = target_swing["price"] * (1.0 + TP_BUFFER_PCT / 100.0)
        else:
            tp_price = entry - (sl_price - entry) * MIN_STRUCTURE_RR

        risk = sl_price - entry
        reward = entry - tp_price

    if risk <= 0 or reward <= 0:
        return None

    sl_pct = risk / entry * 100.0

    if sl_pct < MIN_SL_PCT or sl_pct > MAX_SL_PCT:
        return None

    rr = reward / risk

    if rr < MIN_STRUCTURE_RR:
        return None

    if rr > MAX_STRUCTURE_RR:
        if direction == "LONG":
            tp_price = entry + risk * MAX_STRUCTURE_RR
        else:
            tp_price = entry - risk * MAX_STRUCTURE_RR

        reward = abs(tp_price - entry)
        rr = reward / risk

    if direction == "LONG":
        tp_move_pct = (tp_price - entry) / entry * 100.0
        sl_move_pct = (entry - sl_price) / entry * 100.0
    else:
        tp_move_pct = (entry - tp_price) / entry * 100.0
        sl_move_pct = (sl_price - entry) / entry * 100.0

    tp_roi_pct = tp_move_pct * LEVERAGE
    sl_roi_pct = sl_move_pct * LEVERAGE

    return (
        round(tp_price, 8),
        round(sl_price, 8),
        round(tp_roi_pct, 1),
        round(sl_roi_pct, 1),
        round(rr, 2),
    )


# ── Signal scoring ────────────────────────────────────────────────

def _calculate_score(direction: str, sweep: dict, ob: dict, retest: dict, rr: float) -> float:
    score = 50.0

    # RR quality
    if rr >= 3.0:
        score += 20.0
    elif rr >= 2.0:
        score += 15.0
    elif rr >= MIN_STRUCTURE_RR:
        score += 10.0

    # Retest candle quality
    if retest["body_pct"] <= 0.35:
        score += 10.0
    elif retest["body_pct"] <= 0.70:
        score += 5.0

    # OB freshness
    candles_from_ob_to_retest = retest["pos"] - ob["pos"]

    if candles_from_ob_to_retest <= 12:
        score += 10.0
    elif candles_from_ob_to_retest <= 24:
        score += 5.0

    # Sweep-to-retest compactness
    candles_from_sweep_to_retest = retest["pos"] - sweep["sweep_pos"]

    if candles_from_sweep_to_retest <= 20:
        score += 10.0
    elif candles_from_sweep_to_retest <= 35:
        score += 5.0

    return round(min(score, 100.0), 1)


# ── SMC setup search ──────────────────────────────────────────────

def _find_long_setup(entry_df: pd.DataFrame) -> tuple[dict | None, dict]:
    completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)

    if len(completed) < 80:
        return None, {}

    swings = _find_swings(completed, SWING_LEFT, SWING_RIGHT)

    if len(swings) < 4:
        return None, {}

    start = max(AVG_BODY_PERIOD + SWEEP_LOOKBACK + ORDER_BLOCK_LOOKBACK, 30)
    last_possible = len(completed) - 2

    best_setup = None
    best_score = -1.0

    for displacement_pos in range(start, last_possible + 1):
        if not _is_bullish_displacement(completed, displacement_pos):
            continue

        sweep = None
        sweep_start = max(0, displacement_pos - SWEEP_LOOKBACK)

        for sweep_pos in range(displacement_pos - 1, sweep_start - 1, -1):
            sweep = _detect_sell_side_sweep(completed, swings, sweep_pos)
            if sweep:
                break

        if not sweep:
            continue

        ob = _find_bullish_order_block(completed, displacement_pos)

        if not ob:
            continue

        retest = _find_bullish_retest(completed, ob, displacement_pos)

        if not retest:
            continue

        # Only accept if retest is very recent.
        if retest["pos"] < len(completed) - 3:
            continue

        entry = retest["entry"]

        prices = _calculate_structure_prices(
            direction="LONG",
            entry=entry,
            sweep=sweep,
            ob=ob,
            retest=retest,
            swings=swings,
        )

        if not prices:
            continue

        tp_price, sl_price, tp_roi_pct, sl_roi_pct, rr = prices
        score = _calculate_score("LONG", sweep, ob, retest, rr)

        if score > best_score:
            best_score = score
            best_setup = {
                "direction": "LONG",
                "entry": entry,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "tp_roi_pct": tp_roi_pct,
                "sl_roi_pct": sl_roi_pct,
                "rr": rr,
                "score": score,
                "sweep": sweep,
                "ob": ob,
                "retest": retest,
                "displacement_pos": displacement_pos,
                "displacement_time": completed.index[displacement_pos],
            }

    return best_setup, {"swing_count": len(swings)}


def _find_short_setup(entry_df: pd.DataFrame) -> tuple[dict | None, dict]:
    completed = entry_df.iloc[:-1].copy().tail(ENTRY_LOOKBACK)

    if len(completed) < 80:
        return None, {}

    swings = _find_swings(completed, SWING_LEFT, SWING_RIGHT)

    if len(swings) < 4:
        return None, {}

    start = max(AVG_BODY_PERIOD + SWEEP_LOOKBACK + ORDER_BLOCK_LOOKBACK, 30)
    last_possible = len(completed) - 2

    best_setup = None
    best_score = -1.0

    for displacement_pos in range(start, last_possible + 1):
        if not _is_bearish_displacement(completed, displacement_pos):
            continue

        sweep = None
        sweep_start = max(0, displacement_pos - SWEEP_LOOKBACK)

        for sweep_pos in range(displacement_pos - 1, sweep_start - 1, -1):
            sweep = _detect_buy_side_sweep(completed, swings, sweep_pos)
            if sweep:
                break

        if not sweep:
            continue

        ob = _find_bearish_order_block(completed, displacement_pos)

        if not ob:
            continue

        retest = _find_bearish_retest(completed, ob, displacement_pos)

        if not retest:
            continue

        # Only accept if retest is very recent.
        if retest["pos"] < len(completed) - 3:
            continue

        entry = retest["entry"]

        prices = _calculate_structure_prices(
            direction="SHORT",
            entry=entry,
            sweep=sweep,
            ob=ob,
            retest=retest,
            swings=swings,
        )

        if not prices:
            continue

        tp_price, sl_price, tp_roi_pct, sl_roi_pct, rr = prices
        score = _calculate_score("SHORT", sweep, ob, retest, rr)

        if score > best_score:
            best_score = score
            best_setup = {
                "direction": "SHORT",
                "entry": entry,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "tp_roi_pct": tp_roi_pct,
                "sl_roi_pct": sl_roi_pct,
                "rr": rr,
                "score": score,
                "sweep": sweep,
                "ob": ob,
                "retest": retest,
                "displacement_pos": displacement_pos,
                "displacement_time": completed.index[displacement_pos],
            }

    return best_setup, {"swing_count": len(swings)}


# ── Main analysis ─────────────────────────────────────────────────

def analyze_coin(symbol: str) -> "Signal | None":
    try:
        trend_df = get_klines(symbol, TREND_TF, count=TREND_KLINE_COUNT)
        entry_df = get_klines(symbol, ENTRY_TF, count=ENTRY_KLINE_COUNT)

        if trend_df is None or trend_df.empty:
            return None

        if entry_df is None or entry_df.empty:
            return None

        bias, bias_details = _get_market_structure_bias(trend_df)

        if bias is None:
            return None

        if bias == "LONG":
            setup, setup_details = _find_long_setup(entry_df)
        else:
            setup, setup_details = _find_short_setup(entry_df)

        if not setup:
            return None

        direction = setup["direction"]
        entry = setup["entry"]
        tp_price = setup["tp_price"]
        sl_price = setup["sl_price"]
        tp_roi_pct = setup["tp_roi_pct"]
        sl_roi_pct = setup["sl_roi_pct"]
        rr = setup["rr"]
        score = setup["score"]

        sweep = setup["sweep"]
        ob = setup["ob"]
        retest = setup["retest"]

        logger.info(
            f"[SIGNAL] {direction} {symbol} @ {entry:.6g} | "
            f"TP={tp_price:.6g} (+{tp_roi_pct:.1f}% ROI) "
            f"SL={sl_price:.6g} (-{sl_roi_pct:.1f}% ROI) | "
            f"RR={rr:.2f} | "
            f"bias={bias} {TREND_TF} break={bias_details.get('break_price')} | "
            f"sweep={sweep['type']} level={sweep['sweep_level']:.6g} "
            f"extreme={sweep['sweep_extreme']:.6g} | "
            f"OB={ob['type']} zone={ob['zone_low']:.6g}-{ob['zone_high']:.6g} | "
            f"retest_body={retest['body_pct']:.3f}% | "
            f"score={score}"
        )

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
                f"SMC {ENTRY_TF} | {TREND_TF} bias {bias} | "
                f"{sweep['type']} + OB retest | RR {rr:g}"
            ),
            generated_at=datetime.now(timezone.utc),
            score=score,
        )

    except Exception as e:
        logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
        return None