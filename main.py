"""
Main entry point — Breakout + Retest + EMA/VWAP Scalper.

Scheduler jobs:
  Every 1 min     — scan coin pool for breakout setups
  Every 1 min     — monitor pending retest setups
  Every 1 min     — check pending signal outcomes
  Every 6h        — refresh futures-only coin pool
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import strategy
import bot as tg
import coin_scanner

from candle_cache import CandleCache
from market_data import set_candle_cache
from mexc_client import get_klines, get_current_price
from mexc_ws_client import MexcWebSocketClient

from config import (
    LKT,
    STRATEGY_NAME,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    MAX_CONCURRENT_SIGNALS,
    LEVERAGE,
    ENTRY_TF,
    SETUP_SCAN_CRON_MINUTES,
    SETUP_MONITOR_MINUTES,
    SIGNALS_PER_SCAN,
    OUTCOME_CHECK_MINUTES,
    CANDLE_MINUTES,
    SCAN_WORKERS,
    MIN_SIGNAL_SCORE,
    SETUPS_PER_SCAN,
    ENABLE_WEBSOCKET,
    CANDLE_CACHE_LIMIT,
    CANDLE_BOOTSTRAP_WORKERS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mexc_bot.log"),
    ],
)

logging.Formatter.converter = lambda *args: datetime.now(LKT).timetuple()

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_RUNTIME_CACHE: CandleCache | None = None
_WS_CLIENT: MexcWebSocketClient | None = None
_WS_TASK: asyncio.Task | None = None


# ── candle cache / websocket ──────────────────────────────────────

def _bootstrap_one_symbol(cache: CandleCache, symbol: str) -> tuple[str, bool, str]:
    try:
        candles = get_klines(symbol, ENTRY_TF, count=CANDLE_CACHE_LIMIT)

        if candles is None or candles.empty:
            return symbol, False, "empty_rest_candles"

        cache.seed(symbol, ENTRY_TF, candles)
        return symbol, True, "seeded"

    except Exception as e:
        return symbol, False, str(e)


async def bootstrap_candle_cache(symbols: list[str]) -> CandleCache:
    cache = CandleCache(limit=CANDLE_CACHE_LIMIT)

    if not symbols:
        logger.warning("[CACHE] No symbols to bootstrap")
        set_candle_cache(cache)
        return cache

    logger.info("[CACHE] Bootstrapping %s symbols x %s candles", len(symbols), CANDLE_CACHE_LIMIT)

    loop = asyncio.get_event_loop()

    with ThreadPoolExecutor(max_workers=CANDLE_BOOTSTRAP_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(lambda s: _bootstrap_one_symbol(cache, s), symbols)),
        )

    ok = sum(1 for _, success, _ in results if success)
    failed = [f"{symbol}:{reason}" for symbol, success, reason in results if not success]

    logger.info("[CACHE] Bootstrap complete ok=%s failed=%s", ok, len(failed))

    if failed:
        logger.warning("[CACHE] Bootstrap failed preview=%s", failed[:10])

    set_candle_cache(cache)
    return cache


async def start_market_websocket(cache: CandleCache, symbols: list[str]) -> asyncio.Task | None:
    if not ENABLE_WEBSOCKET:
        logger.info("[WS] Disabled by config")
        return None

    if not symbols:
        logger.warning("[WS] No symbols to subscribe")
        return None

    async def on_candle_update(result):
        # Future optimization: scan only closed candle symbol.
        return None

    client = MexcWebSocketClient(
        candle_cache=cache,
        symbols=symbols,
        app_intervals=[ENTRY_TF],
        on_candle_update=on_candle_update,
    )

    task = asyncio.create_task(client.start(), name="mexc_ws_client")

    global _WS_CLIENT
    _WS_CLIENT = client

    logger.info("[WS] Started client for %s symbols on %s", len(symbols), ENTRY_TF)
    return task


# ── setup detection scan ──────────────────────────────────────────

async def scan_for_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SETUP-SCAN] Paused, skipping")
        return

    coins = coin_scanner.get_cached_coins()

    if not coins:
        logger.warning("[SETUP-SCAN] Empty coin pool, skipping")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)

    filtered_cooldown = 0
    filtered_pending = 0

    to_scan = []

    for symbol in coins:
        if db.signal_exists_for_coin(symbol, cooldown_since):
            filtered_cooldown += 1
            continue

        if db.pending_setup_exists(symbol):
            filtered_pending += 1
            continue

        to_scan.append(symbol)

    logger.info(
        "[SETUP-SCAN] pool=%s eligible=%s cooldown=%s pending=%s",
        len(coins),
        len(to_scan),
        filtered_cooldown,
        filtered_pending,
    )

    if not to_scan:
        return

    def _detect(symbol: str):
        try:
            return strategy.detect_setup(symbol)
        except Exception as e:
            logger.error("[SETUP-SCAN] %s setup error: %s", symbol, e, exc_info=True)
            return None

    loop = asyncio.get_event_loop()

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(_detect, to_scan)),
        )

    setups = [
        setup
        for setup in results
        if setup is not None and float(setup.get("score", 0.0)) >= MIN_SIGNAL_SCORE
    ]

    if not setups:
        logger.info("[SETUP-SCAN] Done — 0 breakout setups found")
        return

    setups.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    setups = setups[:SETUPS_PER_SCAN]

    saved = 0

    for setup in setups:
        setup_id = db.save_pending_setup(setup)

        if setup_id:
            saved += 1
            logger.info(
                "[SETUP-SCAN] Saved setup #%s %s %s level=%s score=%s",
                setup_id,
                setup["symbol"],
                setup["direction"],
                f"{setup['sweep_level']:.6g}",
                setup["score"],
            )

    logger.info("[SETUP-SCAN] Done — saved=%s candidates=%s", saved, len(setups))


# ── pending retest monitor ────────────────────────────────────────

async def monitor_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[RETEST-MONITOR] Paused, skipping")
        return

    active = db.count_active_signals()
    slots = MAX_CONCURRENT_SIGNALS - active

    if slots <= 0:
        logger.info("[RETEST-MONITOR] %s/%s active signals, skipping", active, MAX_CONCURRENT_SIGNALS)
        return

    now = datetime.now(timezone.utc)
    db.expire_old_waiting_setups(now)

    setups = db.get_waiting_setups(limit=100)

    if not setups:
        logger.info("[RETEST-MONITOR] No waiting retest setups")
        return

    logger.info("[RETEST-MONITOR] Checking %s waiting setups", len(setups))

    fired_signals = []

    for setup in setups:
        status, sig = strategy.evaluate_pending_setup(setup)

        if status == "EXPIRED":
            db.mark_setup_expired(setup["id"])
            logger.info("[RETEST-MONITOR] Expired setup #%s %s", setup["id"], setup["symbol"])

        elif status == "INVALIDATED":
            db.mark_setup_invalidated(setup["id"])
            logger.info("[RETEST-MONITOR] Invalidated setup #%s %s", setup["id"], setup["symbol"])

        elif status == "FIRE" and sig is not None:
            fired_signals.append((setup, sig))

    if not fired_signals:
        logger.info("[RETEST-MONITOR] Done — 0 retest entries fired")
        return

    fired_signals.sort(key=lambda item: item[1].score, reverse=True)
    to_send = fired_signals[:min(SIGNALS_PER_SCAN, slots)]

    logger.info("[RETEST-MONITOR] fired=%s sending=%s", len(fired_signals), len(to_send))

    for setup, sig in to_send:
        signal_id = db.save_signal(
            symbol=sig.symbol,
            direction=sig.direction,
            entry_price=sig.entry_price,
            tp_price=sig.tp_price,
            sl_price=sig.sl_price,
            leverage=sig.leverage,
            generated_at=sig.generated_at,
        )

        db.mark_setup_fired(setup["id"], signal_id)

        try:
            await tg.broadcast_signal(app, sig, signal_id)
            logger.info(
                "[RETEST-MONITOR] Sent signal #%s from setup #%s %s %s score=%s",
                signal_id,
                setup["id"],
                sig.symbol,
                sig.direction,
                sig.score,
            )
        except Exception as e:
            logger.error("Failed to broadcast %s: %s", sig.symbol, e, exc_info=True)


# ── outcome checker ───────────────────────────────────────────────

def _calculate_pnl_roi(
    direction: str,
    outcome: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
) -> float:
    if outcome == "win":
        if direction == "LONG":
            price_move_pct = (tp_price - entry_price) / entry_price * 100
        else:
            price_move_pct = (entry_price - tp_price) / entry_price * 100
    else:
        if direction == "LONG":
            price_move_pct = (sl_price - entry_price) / entry_price * 100
        else:
            price_move_pct = (entry_price - sl_price) / entry_price * 100

    return price_move_pct * LEVERAGE


async def _close_signal(app: Application, sig: dict, outcome: str, pnl: float) -> None:
    db.update_signal_outcome(sig["id"], outcome, pnl)

    logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], outcome.upper(), sig["symbol"], pnl)

    try:
        await tg.notify_outcome(
            app,
            {
                **sig,
                "status": outcome,
                "pnl_roi": pnl,
            },
        )
    except Exception as e:
        logger.error("Failed to notify %s for %s: %s", outcome, sig["symbol"], e, exc_info=True)


async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        tp_price = sig["tp_price"]
        sl_price = sig["sl_price"]
        entry_price = sig["entry_price"]

        generated = datetime.fromisoformat(sig["generated_at"])

        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            await _close_signal(app, sig, "expired", 0.0)
            continue

        try:
            current_price = get_current_price(symbol)
        except Exception as e:
            logger.warning("Could not fetch current price for %s: %s", symbol, e)
            current_price = None

        instant_outcome = None

        if current_price is not None:
            if direction == "LONG":
                if current_price >= tp_price:
                    instant_outcome = "win"
                elif current_price <= sl_price:
                    instant_outcome = "loss"
            else:
                if current_price <= tp_price:
                    instant_outcome = "win"
                elif current_price >= sl_price:
                    instant_outcome = "loss"

        if instant_outcome is not None:
            pnl = _calculate_pnl_roi(
                direction=direction,
                outcome=instant_outcome,
                entry_price=entry_price,
                tp_price=tp_price,
                sl_price=sl_price,
            )

            await _close_signal(app, sig, instant_outcome, pnl)
            continue

        elapsed_min = max(
            (now - generated).total_seconds() / 60,
            CANDLE_MINUTES,
        )

        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_klines(symbol, ENTRY_TF, count=fetch_count)

            if df.empty or len(df) < 2:
                continue

        except Exception as e:
            logger.warning("Could not fetch candles for %s: %s", symbol, e)
            continue

        entry_candle_cutoff = (
            generated - timedelta(minutes=CANDLE_MINUTES)
        ).replace(tzinfo=None)

        outcome = None

        for i in range(len(df) - 1):
            if df.index[i] <= entry_candle_cutoff:
                continue

            high = float(df["high"].iloc[i])
            low = float(df["low"].iloc[i])
            open_price = float(df["open"].iloc[i])
            close_price = float(df["close"].iloc[i])

            if direction == "LONG":
                hit_tp = high >= tp_price
                hit_sl = low <= sl_price
            else:
                hit_tp = low <= tp_price
                hit_sl = high >= sl_price

            if hit_tp and hit_sl:
                if direction == "LONG":
                    outcome = "win" if close_price >= open_price else "loss"
                else:
                    outcome = "win" if close_price <= open_price else "loss"
                break

            if hit_tp:
                outcome = "win"
                break

            if hit_sl:
                outcome = "loss"
                break

        if outcome is None:
            continue

        pnl = _calculate_pnl_roi(
            direction=direction,
            outcome=outcome,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
        )

        await _close_signal(app, sig, outcome, pnl)


# ── main ──────────────────────────────────────────────────────────

async def main():
    global _RUNTIME_CACHE, _WS_TASK, _WS_CLIENT

    logger.info("Starting MEXC Signal Bot — %s", STRATEGY_NAME)

    db.init_db()

    logger.info("Loading futures-only ranked coin pool...")
    coins = coin_scanner.refresh_coin_list()
    ranked_preview = coin_scanner.get_cached_coin_scores()[:10]

    logger.info("Signal pool: %s ranked futures coins", len(coins))

    if ranked_preview:
        logger.info(
            "[COIN-RANK] startup top ranked: "
            + ", ".join(
                f"{row.get('symbol', '').replace('_USDT', '')}:{row.get('score', 0)}"
                for row in ranked_preview
            )
        )

    _RUNTIME_CACHE = await bootstrap_candle_cache(coins)
    _WS_TASK = await start_market_websocket(_RUNTIME_CACHE, coins)

    app = tg.build_app()

    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        scan_for_setups,
        CronTrigger(minute=SETUP_SCAN_CRON_MINUTES),
        args=[app],
        id="setup_scanner",
    )

    scheduler.add_job(
        monitor_setups,
        IntervalTrigger(minutes=SETUP_MONITOR_MINUTES),
        args=[app],
        id="retest_monitor",
    )

    scheduler.add_job(
        check_outcomes,
        IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
        args=[app],
        id="outcome_checker",
    )

    scheduler.add_job(
        coin_scanner.refresh_coin_list,
        CronTrigger(hour=f"*/{COIN_REFRESH_HOURS}"),
        id="coin_refresh",
    )

    async def _daily(app=app):
        await tg.auto_daily_report(type("ctx", (), {"application": app})())

    async def _weekly(app=app):
        await tg.auto_weekly_report(type("ctx", (), {"application": app})())

    async def _monthly(app=app):
        await tg.auto_monthly_report(type("ctx", (), {"application": app})())

    scheduler.add_job(_daily, CronTrigger(hour=23, minute=55), id="daily_report")
    scheduler.add_job(_weekly, CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7), id="monthly_report")

    scheduler.start()

    logger.info(
        "Scheduler started — setup scan='%s', retest monitor=%sm, outcome=%sm, entry_tf=%s",
        SETUP_SCAN_CRON_MINUTES,
        SETUP_MONITOR_MINUTES,
        OUTCOME_CHECK_MINUTES,
        ENTRY_TF,
    )

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Bot is running. Press Ctrl+C to stop.")

        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler.shutdown(wait=False)

            if _WS_CLIENT is not None:
                await _WS_CLIENT.stop()

            if _WS_TASK is not None:
                _WS_TASK.cancel()
                try:
                    await _WS_TASK
                except asyncio.CancelledError:
                    pass

            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())