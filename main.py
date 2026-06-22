"""
Main entry point — MTF Trend Pullback + Volume Confirmation + WebSocket Trigger.

Scheduler jobs:
  Every 15 min   — REST setup scanner: detect armed setups
  Every 2 sec    — WebSocket trigger engine: fire when price enters entry zone
  Every 1 min    — outcome checker
  Every 6h       — coin pool refresh
  23:55 daily    — daily report
  Mon 07:00      — weekly report
  1st 07:00      — monthly report
"""

import asyncio
import logging
import shutil
import sys
from pathlib import Path
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
from ws_manager import WsPriceManager
from trigger_engine import check_triggers
from mexc_client import get_klines
from config import (
    LKT,
    LEVERAGE,
    ENTRY_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SETUP_SCAN_CRON_MINUTES,
    TRIGGER_CHECK_SECONDS,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    TOP_N_COINS,
    COIN_POOL_MIN_VOLUME_USD,
    COIN_POOL_MIN_SELECTED,
    COINGLASS_API_KEY,
    MIN_SETUP_SCORE,
    MIN_SIGNAL_SCORE,
    MIN_ATR_SL_MULTIPLIER,
    MIN_SL_ROI_PCT,
    MAX_SL_ROI_PCT,
    REQUIRE_CONFIRMATION_AFTER_TOUCH,
)


def _backup_log_on_startup() -> None:
    if not ENABLE_LOG_BACKUP_ON_START:
        Path(LOG_FILE).touch(exist_ok=True)
        return
    log_path  = Path(LOG_FILE)
    archive   = Path(LOG_BACKUP_DIR)
    archive.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > 0:
        ts = datetime.now(LKT).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(log_path, archive / f"{log_path.stem}_{ts}{log_path.suffix or '.log'}")
        log_path.write_text("", encoding="utf-8")
    else:
        log_path.touch(exist_ok=True)


_backup_log_on_startup()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)

logging.Formatter.converter = lambda *args: datetime.now(LKT).timetuple()
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── WebSocket price manager (module-level singleton) ──────────────

WS_MANAGER: WsPriceManager | None = None
WS_TASK: asyncio.Task | None = None


async def _start_ws_manager(coins: list[str]) -> None:
    global WS_MANAGER, WS_TASK
    symbols = list(dict.fromkeys(coins[:TOP_N_COINS]))
    if not symbols:
        logger.warning("[WS-PRICE] No symbols for WebSocket — price-triggered signals disabled")
        return
    WS_MANAGER = WsPriceManager(symbols)
    WS_TASK = asyncio.create_task(WS_MANAGER.start(), name="ws_price_manager")
    logger.info("[WS-PRICE] Started price manager for %d symbols", len(symbols))


async def _stop_ws_manager() -> None:
    global WS_MANAGER, WS_TASK
    if WS_MANAGER is not None:
        try:
            await WS_MANAGER.stop()
        except Exception:
            pass
    if WS_TASK is not None:
        WS_TASK.cancel()
        try:
            await WS_TASK
        except (asyncio.CancelledError, Exception):
            pass
    WS_MANAGER = None
    WS_TASK = None


# ── REST setup scanner ────────────────────────────────────────────

async def scan_for_armed_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)

    # Skip coins that already have an active armed setup or are on signal cooldown
    to_scan: list[str] = []
    for symbol in coins:
        if db.armed_setup_exists(symbol):
            continue
        if db.signal_exists_for_coin(symbol, cooldown_since):
            continue
        to_scan.append(symbol)

    logger.info("[SCAN] %d/%d coins eligible after filters", len(to_scan), len(coins))

    if not to_scan:
        return

    def _detect(symbol: str):
        try:
            return strategy.detect_armed_setup(symbol)
        except Exception as e:
            logger.error("[SCAN] %s error: %s", symbol, e, exc_info=True)
            return None

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None, lambda: list(executor.map(_detect, to_scan))
        )

    saved = 0
    for setup in results:
        if setup is None:
            continue
        setup_id = db.save_armed_setup(setup)
        if setup_id:
            saved += 1
            logger.info(
                "[SCAN] Saved armed setup #%d | %s %s zone=%.6g-%.6g score=%.1f",
                setup_id, setup["symbol"], setup["direction"],
                setup["entry_low"], setup["entry_high"], setup["score"],
            )

    logger.info("[SCAN] Done — %d/%d setups saved", saved, len([r for r in results if r]))


# ── WebSocket trigger dispatcher ──────────────────────────────────

async def run_trigger_check(app: Application) -> None:
    if WS_MANAGER is None:
        return
    try:
        await check_triggers(app, WS_MANAGER)
    except Exception as e:
        logger.error("[TRIGGER-DISPATCH] %s", e, exc_info=True)


# ── Outcome checker ───────────────────────────────────────────────

def _calculate_pnl_roi(
    direction: str,
    outcome: str,
    entry_price: float,
    tp_price: float,
    sl_price: float,
) -> float:
    if outcome == "win":
        price_move_pct = (
            (tp_price - entry_price) / entry_price * 100
            if direction == "LONG"
            else (entry_price - tp_price) / entry_price * 100
        )
    else:
        price_move_pct = (
            (sl_price - entry_price) / entry_price * 100
            if direction == "LONG"
            else (entry_price - sl_price) / entry_price * 100
        )
    return price_move_pct * LEVERAGE


def _valid_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol     = sig["symbol"]
        direction  = sig["direction"]
        tp_price   = sig["tp_price"]
        sl_price   = sig["sl_price"]
        entry_price = sig["entry_price"]

        if not _valid_geometry(direction, entry_price, tp_price, sl_price):
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            continue

        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info("Signal %s expired (%s)", sig["id"], symbol)
            try:
                await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
            except Exception as e:
                logger.error("Failed to notify expiry for %s: %s", symbol, e)
            continue

        elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_klines(symbol, ENTRY_TF, count=fetch_count)
            if df is None or df.empty or len(df) < 2:
                continue
        except Exception as e:
            logger.warning("Could not fetch candles for %s: %s", symbol, e)
            continue

        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)
        outcome = None

        for i in range(len(df) - 1):
            if df.index[i] <= entry_candle_cutoff:
                continue
            high  = float(df["high"].iloc[i])
            low   = float(df["low"].iloc[i])
            open_ = float(df["open"].iloc[i])
            close = float(df["close"].iloc[i])

            hit_tp = (high >= tp_price) if direction == "LONG" else (low <= tp_price)
            hit_sl = (low  <= sl_price) if direction == "LONG" else (high >= sl_price)

            if hit_tp and hit_sl:
                outcome = "win" if (
                    (direction == "LONG"  and close >= open_) or
                    (direction == "SHORT" and close <= open_)
                ) else "loss"
                break
            if hit_tp:
                outcome = "win"
                break
            if hit_sl:
                outcome = "loss"
                break

        if outcome is None:
            continue

        pnl = _calculate_pnl_roi(direction, outcome, entry_price, tp_price, sl_price)
        db.update_signal_outcome(sig["id"], outcome, pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], outcome.upper(), symbol, pnl)

        try:
            await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
        except Exception as e:
            logger.error("Failed to notify %s for %s: %s", outcome, symbol, e)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot — MTF Trend Pullback + WebSocket Trigger")
    logger.info(
        "[CONFIG] coin pool: TOP_N=%s MIN_SELECTED=%s MIN_VOL=$%.0f COINGLASS=%s",
        TOP_N_COINS, COIN_POOL_MIN_SELECTED, COIN_POOL_MIN_VOLUME_USD,
        "SET" if COINGLASS_API_KEY else "EMPTY",
    )
    logger.info(
        "[CONFIG] signal quality: SETUP_SCORE>=%s SIGNAL_SCORE>=%s "
        "ATR_SL>=%.1fx SL_ROI=[%.0f%%,%.0f%%] CONFIRM=%s",
        MIN_SETUP_SCORE, MIN_SIGNAL_SCORE,
        MIN_ATR_SL_MULTIPLIER, MIN_SL_ROI_PCT, MAX_SL_ROI_PCT,
        REQUIRE_CONFIRMATION_AFTER_TOUCH,
    )

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info("Coin pool: %d coins", len(coins))

    await _start_ws_manager(coins)

    app = tg.build_app()

    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": SCHEDULER_MAX_INSTANCES,
            "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_SECONDS,
        },
    )

    # Setup scanner every 15 minutes
    scheduler.add_job(
        scan_for_armed_setups,
        CronTrigger(minute=SETUP_SCAN_CRON_MINUTES),
        args=[app],
        id="setup_scanner",
    )

    # WebSocket trigger check every 2 seconds
    scheduler.add_job(
        run_trigger_check,
        IntervalTrigger(seconds=TRIGGER_CHECK_SECONDS),
        args=[app],
        id="trigger_engine",
    )

    # Outcome checker every minute
    scheduler.add_job(
        check_outcomes,
        IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
        args=[app],
        id="outcome_checker",
    )

    # Coin refresh every 6 hours
    scheduler.add_job(
        coin_scanner.refresh_coin_list,
        CronTrigger(hour=f"*/{COIN_REFRESH_HOURS}"),
        id="coin_refresh",
    )

    # Reports
    async def _daily(app=app):
        await tg.auto_daily_report(type("ctx", (), {"application": app})())

    async def _weekly(app=app):
        await tg.auto_weekly_report(type("ctx", (), {"application": app})())

    async def _monthly(app=app):
        await tg.auto_monthly_report(type("ctx", (), {"application": app})())

    scheduler.add_job(_daily,   CronTrigger(hour=23, minute=55),             id="daily_report")
    scheduler.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=7),      id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7),                  id="monthly_report")

    scheduler.start()

    logger.info(
        "Scheduler started — scan=%s trigger=%ds outcome=%dm",
        SETUP_SCAN_CRON_MINUTES, TRIGGER_CHECK_SECONDS, OUTCOME_CHECK_MINUTES,
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
            await _stop_ws_manager()
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
