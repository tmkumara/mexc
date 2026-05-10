"""
Trend Scanner: identifies trending coins on 4H and 1D timeframes.

Scan logic (per coin, per timeframe):
  1. Market structure — last pivot high > previous (HH) → LONG
                        last pivot low  < previous (LL) → SHORT
  2. Impulse leg size ≥ TREND_MIN_IMPULSE_PCT (4H) / TREND_MIN_IMPULSE_1D (1D)
  3. ADX ≥ TREND_ADX_MIN (directional momentum, not sideways chop)

Alert: Fibonacci retracement levels (23.6%–78.6%) for the detected impulse.
       Draw fib from swing_low → swing_high, wait for 50–61.8% retest to enter.

Deduplication: fires once per unique (symbol, tf, direction, swing) — resets on restart.
Fallback:      if MEXC throttles kline calls, errored coins are silently skipped;
               the scan still completes on whichever coins respond.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import pandas_ta as ta
from telegram.constants import ParseMode
from telegram.ext import Application

from mexc_client import get_klines, get_tickers, get_all_contracts
from config import (
    EXCLUDE_COINS, LKT, TELEGRAM_CHANNEL_ID,
    TREND_N_COINS, TREND_SCAN_WORKERS,
    TREND_PIVOT_LOOKBACK, TREND_MIN_IMPULSE_PCT, TREND_MIN_IMPULSE_1D,
    TREND_ADX_MIN, TREND_ADX_PERIOD,
    TREND_KLINE_COUNT_4H, TREND_KLINE_COUNT_1D,
    TREND_IMPULSE_WINDOW, TREND_MIN_MOMENTUM_RATIO, TREND_MIN_BODY_RATIO,
)

logger = logging.getLogger(__name__)

_trend_coins: list[str] = []
_alerted: dict[tuple, tuple] = {}   # (symbol, tf, direction) → (swing_low, swing_high)


# ── Coin pool ─────────────────────────────────────────────────────

def refresh_trend_coins() -> list[str]:
    global _trend_coins
    try:
        tickers = get_tickers()
        rows: list[tuple[str, float]] = []
        for sym, t in tickers.items():
            if not sym.endswith("_USDT") or sym in EXCLUDE_COINS:
                continue
            vol = 0.0
            for key in ("amount24", "volume24", "vol24", "volume"):
                v = t.get(key)
                if v is not None:
                    try:
                        vol = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
            if vol > 0:
                rows.append((sym, vol))

        rows.sort(key=lambda x: x[1], reverse=True)
        coins = [sym for sym, _ in rows[:TREND_N_COINS]]

        try:
            contracts = get_all_contracts()
            active = {c["symbol"] for c in contracts if c.get("state") in (0, None)}
            coins = [s for s in coins if s in active]
        except Exception as e:
            logger.warning(f"[TREND] Contract validation skipped: {e}")

        if not coins:
            logger.warning("[TREND] No coins fetched — keeping previous cache")
            return _trend_coins

        _trend_coins = coins
        logger.info(
            f"[TREND] Coin pool: {len(_trend_coins)} coins "
            f"(top: {[s.replace('_USDT','') for s in _trend_coins[:10]]}...)"
        )
        return _trend_coins

    except Exception as e:
        logger.error(f"[TREND] refresh_trend_coins error: {e}")
        return _trend_coins


def get_trend_coins() -> list[str]:
    if not _trend_coins:
        return refresh_trend_coins()
    return _trend_coins


# ── Pivot helpers ─────────────────────────────────────────────────

def _pivot_highs(series: pd.Series) -> list[tuple[int, float]]:
    lb = TREND_PIVOT_LOOKBACK
    n  = len(series)
    result = []
    for i in range(lb, n - lb):
        v = float(series.iloc[i])
        if all(series.iloc[i - lb:i] < v) and all(series.iloc[i + 1:i + lb + 1] < v):
            result.append((i, v))
    return result


def _pivot_lows(series: pd.Series) -> list[tuple[int, float]]:
    lb = TREND_PIVOT_LOOKBACK
    n  = len(series)
    result = []
    for i in range(lb, n - lb):
        v = float(series.iloc[i])
        if all(series.iloc[i - lb:i] > v) and all(series.iloc[i + 1:i + lb + 1] > v):
            result.append((i, v))
    return result


# ── Trend + impulse detection ─────────────────────────────────────

@dataclass
class TrendResult:
    symbol:        str
    timeframe:     str
    direction:     str
    swing_low:     float
    swing_high:    float
    impulse_pct:   float
    adx:           float
    current_price: float


def _momentum_quality(completed: pd.DataFrame, pivot_idx: int, direction: str) -> bool:
    """
    Check that the candles leading into the pivot show strong directional momentum
    (like the screenshot: consecutive big bullish/bearish candles, minimal wicks).

    Looks at TREND_IMPULSE_WINDOW candles ending at pivot_idx.
    Requires:
      - ≥ TREND_MIN_MOMENTUM_RATIO of candles close in the trend direction
      - avg body/range ≥ TREND_MIN_BODY_RATIO (rules out doji/wick-heavy moves)
    """
    start = max(0, pivot_idx - TREND_IMPULSE_WINDOW)
    window = completed.iloc[start:pivot_idx + 1]
    if len(window) < 3:
        return False

    opens  = window["open"]
    closes = window["close"]
    highs  = window["high"]
    lows   = window["low"]

    if direction == "LONG":
        trend_closes = (closes > opens).sum()
    else:
        trend_closes = (closes < opens).sum()

    momentum_ratio = trend_closes / len(window)
    if momentum_ratio < TREND_MIN_MOMENTUM_RATIO:
        return False

    candle_range = (highs - lows).clip(lower=1e-10)
    avg_body     = ((closes - opens).abs() / candle_range).mean()
    if avg_body < TREND_MIN_BODY_RATIO:
        return False

    return True


def _detect_trend(df: pd.DataFrame, min_impulse_pct: float) -> tuple | None:
    """
    Returns (direction, swing_low, swing_high) or None.
    LONG:  last pivot high > previous (HH) + strong bullish candles into the pivot
    SHORT: last pivot low  < previous (LL) + strong bearish candles into the pivot
    Also checks impulse size ≥ min_impulse_pct.
    Works on completed candles only (df.iloc[:-1]).
    """
    completed = df.iloc[:-1]
    if len(completed) < TREND_PIVOT_LOOKBACK * 2 + 15:
        return None

    highs = _pivot_highs(completed["high"])
    lows  = _pivot_lows(completed["low"])

    if not highs or not lows:
        return None

    # ── LONG: HH + bullish momentum into the pivot ───────────────
    if len(highs) >= 2:
        last_ph_idx, last_ph = highs[-1]
        _,           prev_ph = highs[-2]
        if last_ph > prev_ph:
            lows_before = [(i, l) for i, l in lows if i < last_ph_idx]
            if lows_before:
                _, sw_low = min(lows_before[-3:], key=lambda x: x[1])
                if sw_low > 0:
                    impulse_pct = (last_ph - sw_low) / sw_low * 100
                    if impulse_pct >= min_impulse_pct:
                        if _momentum_quality(completed, last_ph_idx, "LONG"):
                            return ("LONG", sw_low, last_ph)

    # ── SHORT: LL + bearish momentum into the pivot ───────────────
    if len(lows) >= 2:
        last_pl_idx, last_pl = lows[-1]
        _,           prev_pl = lows[-2]
        if last_pl < prev_pl:
            highs_before = [(i, h) for i, h in highs if i < last_pl_idx]
            if highs_before:
                _, sw_high = max(highs_before[-3:], key=lambda x: x[1])
                if sw_high > 0:
                    impulse_pct = (sw_high - last_pl) / sw_high * 100
                    if impulse_pct >= min_impulse_pct:
                        if _momentum_quality(completed, last_pl_idx, "SHORT"):
                            return ("SHORT", last_pl, sw_high)

    return None


def _get_adx(df: pd.DataFrame) -> float:
    try:
        completed = df.iloc[:-1]
        result = ta.adx(
            completed["high"], completed["low"], completed["close"],
            length=TREND_ADX_PERIOD,
        )
        if result is None or result.empty:
            return 0.0
        col = next((c for c in result.columns if c.startswith("ADX_")), None)
        if col is None:
            return 0.0
        val = result[col].iloc[-1]
        return float(val) if not pd.isna(val) else 0.0
    except Exception:
        return 0.0


def _analyze_coin(symbol: str) -> list[TrendResult]:
    results = []
    for tf, count, min_pct in [
        ("4h", TREND_KLINE_COUNT_4H, TREND_MIN_IMPULSE_PCT),
        ("1d", TREND_KLINE_COUNT_1D, TREND_MIN_IMPULSE_1D),
    ]:
        try:
            df = get_klines(symbol, tf, count=count)
            if df.empty or len(df) < TREND_PIVOT_LOOKBACK * 2 + 20:
                continue

            trend = _detect_trend(df, min_pct)
            if trend is None:
                continue

            direction, sw_low, sw_high = trend
            adx = _get_adx(df)
            if adx < TREND_ADX_MIN:
                logger.debug(
                    f"[TREND] {symbol} {tf} {direction}: ADX={adx:.1f} < {TREND_ADX_MIN}, skip"
                )
                continue

            impulse_pct   = (sw_high - sw_low) / sw_low * 100
            current_price = float(df["close"].iloc[-2])

            results.append(TrendResult(
                symbol=symbol, timeframe=tf, direction=direction,
                swing_low=sw_low, swing_high=sw_high,
                impulse_pct=impulse_pct, adx=adx,
                current_price=current_price,
            ))
            logger.debug(
                f"[TREND] {symbol} {tf} {direction}: "
                f"swing={sw_low:.5g}→{sw_high:.5g} "
                f"impulse={impulse_pct:.1f}% ADX={adx:.1f}"
            )
        except Exception as e:
            logger.debug(f"[TREND] {symbol} {tf} error: {e}")

    return results


# ── Fibonacci ─────────────────────────────────────────────────────

def _fib_levels(sw_low: float, sw_high: float) -> dict[str, float]:
    rng = sw_high - sw_low
    return {
        "23.6": sw_high - 0.236 * rng,
        "38.2": sw_high - 0.382 * rng,
        "50.0": sw_high - 0.500 * rng,
        "61.8": sw_high - 0.618 * rng,
        "78.6": sw_high - 0.786 * rng,
    }


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}"


# ── Alert formatting ──────────────────────────────────────────────

def format_trend_alert(r: TrendResult) -> str:
    arrow  = "📈 Bullish" if r.direction == "LONG" else "📉 Bearish"
    struct = "HH+HL" if r.direction == "LONG" else "LH+LL"
    coin   = r.symbol.replace("_USDT", "")
    fib    = _fib_levels(r.swing_low, r.swing_high)

    return "\n".join([
        f"🎯 *{coin}* — {r.timeframe.upper()} TREND",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} _({struct} confirmed)_",
        f"💪 Swing: `${_fmt(r.swing_low)}` → `${_fmt(r.swing_high)}`  _({r.impulse_pct:.1f}%)_",
        f"📊 ADX: `{r.adx:.1f}`",
        "",
        "*Fibonacci Retracement:*",
        f"  23.6%  `${_fmt(fib['23.6'])}`",
        f"  38.2%  `${_fmt(fib['38.2'])}`",
        f"  50.0%  `${_fmt(fib['50.0'])}`  ← watch",
        f"  61.8%  `${_fmt(fib['61.8'])}`  ← OTE",
        f"  78.6%  `${_fmt(fib['78.6'])}`",
        "",
        f"📍 Now: `${_fmt(r.current_price)}`",
        f"⏰ `{datetime.now(LKT).strftime('%Y-%m-%d %H:%M LKT')}`",
        "_Draw fib low→high, enter at 50-61.8% retest_",
    ])


# ── Main scan ─────────────────────────────────────────────────────

async def scan_and_alert(app: Application) -> int:
    """
    Scan top-150 coins on 4H + 1D. Send one Telegram alert per new trending swing.
    Returns number of alerts sent.
    """
    coins = get_trend_coins()
    if not coins:
        logger.warning("[TREND] Coin pool empty, skipping scan")
        return 0

    logger.info(f"[TREND] Scanning {len(coins)} coins on 4H + 1D...")

    loop = asyncio.get_running_loop()

    def _run() -> list[TrendResult]:
        all_results: list[TrendResult] = []
        with ThreadPoolExecutor(max_workers=TREND_SCAN_WORKERS) as ex:
            futs = {ex.submit(_analyze_coin, s): s for s in coins}
            for fut in as_completed(futs):
                try:
                    all_results.extend(fut.result())
                except Exception as e:
                    logger.debug(f"[TREND] worker error: {e}")
        return all_results

    all_results = await loop.run_in_executor(None, _run)

    # Only alert on new or changed swings
    new_alerts: list[TrendResult] = []
    for r in all_results:
        key           = (r.symbol, r.timeframe, r.direction)
        current_swing = (round(r.swing_low, 8), round(r.swing_high, 8))
        if _alerted.get(key) != current_swing:
            new_alerts.append(r)

    # 1D alerts first, then 4H — within each group, largest impulse first
    new_alerts.sort(key=lambda r: (0 if r.timeframe == "1d" else 1, -r.impulse_pct))

    logger.info(
        f"[TREND] {len(new_alerts)} new alert(s) "
        f"from {len(all_results)} trending coins "
        f"({len(coins)} scanned)"
    )

    sent = 0
    for r in new_alerts:
        _alerted[(r.symbol, r.timeframe, r.direction)] = (
            round(r.swing_low, 8), round(r.swing_high, 8)
        )
        try:
            await app.bot.send_message(
                chat_id    = TELEGRAM_CHANNEL_ID,
                text       = format_trend_alert(r),
                parse_mode = ParseMode.MARKDOWN,
            )
            sent += 1
            logger.info(
                f"[TREND] Alert: {r.symbol} {r.timeframe} {r.direction} "
                f"impulse={r.impulse_pct:.1f}% ADX={r.adx:.1f}"
            )
        except Exception as e:
            logger.error(f"[TREND] Send failed {r.symbol} {r.timeframe}: {e}")

    return sent
