"""
Main entry point.
  - Initialises DB
  - Builds the Telegram bot application
  - Registers APScheduler jobs:
      • every 5 min  → scan coins for signals
      • every 5 min  → check pending signal outcomes (TP / SL hit)
      • every 6 h    → refresh zero-fee coin list
      • 23:55 daily  → post daily report
      • Mon 07:00    → post weekly report
      • 1st 07:00    → post monthly report
  - Starts polling
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import coin_scanner
import strategy
import bot as tg
from mexc_client import get_current_price
from config import (
    SCAN_INTERVAL_SECONDS,
    COIN_REFRESH_HOURS,
    SIGNAL_EXPIRE_HOURS,
    SIGNAL_COOLDOWN_MINUTES,
    TP_PCT,
    SL_PCT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mexc_bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# ─────────────────────── scanner job ────────────────────────────

async def scan_and_signal(app: Application):
    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("No coins to scan")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)

    for symbol in coins:
        # Skip if we already have a recent pending signal for this coin
        if db.signal_exists_for_coin(symbol, cooldown_since):
            continue

        signal = strategy.analyze_coin(symbol)
        if signal is None:
            continue

        signal_id = db.save_signal(
            symbol      = signal.symbol,
            direction   = signal.direction,
            entry_price = signal.entry_price,
            tp_price    = signal.tp_price,
            sl_price    = signal.sl_price,
            leverage    = signal.leverage,
            generated_at= signal.generated_at,
        )

        logger.info(f"Signal generated: {signal.direction} {symbol} @ {signal.entry_price}")

        try:
            await tg.broadcast_signal(app, signal, signal_id)
        except Exception as e:
            logger.error(f"Failed to send signal for {symbol}: {e}")


# ────────────────── outcome checker job ─────────────────────────

async def check_outcomes(app: Application):
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol      = sig["symbol"]
        direction   = sig["direction"]
        tp_price    = sig["tp_price"]
        sl_price    = sig["sl_price"]
        generated   = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        # Expire after SIGNAL_EXPIRE_HOURS
        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info(f"Signal {sig['id']} expired ({symbol})")
            try:
                await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
            except Exception as e:
                logger.error(f"Failed to notify expiry for {symbol}: {e}")
            continue

        current_price = get_current_price(symbol)
        if current_price is None:
            continue

        hit_tp = hit_sl = False
        if direction == "LONG":
            hit_tp = current_price >= tp_price
            hit_sl = current_price <= sl_price
        else:
            hit_tp = current_price <= tp_price
            hit_sl = current_price >= sl_price

        if hit_tp:
            pnl = TP_PCT * sig["leverage"] * 100
            db.update_signal_outcome(sig["id"], "win", pnl)
            logger.info(f"Signal {sig['id']} WIN ({symbol}) +{pnl:.1f}%")
            try:
                await tg.notify_outcome(app, {**sig, "status": "win", "pnl_roi": pnl})
            except Exception as e:
                logger.error(f"Failed to notify win for {symbol}: {e}")

        elif hit_sl:
            pnl = -SL_PCT * sig["leverage"] * 100
            db.update_signal_outcome(sig["id"], "loss", pnl)
            logger.info(f"Signal {sig['id']} LOSS ({symbol}) {pnl:.1f}%")
            try:
                await tg.notify_outcome(app, {**sig, "status": "loss", "pnl_roi": pnl})
            except Exception as e:
                logger.error(f"Failed to notify loss for {symbol}: {e}")


# ─────────────────────── scheduler setup ────────────────────────

def setup_scheduler(app: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Scan every 5 minutes
    scheduler.add_job(
        scan_and_signal, IntervalTrigger(seconds=SCAN_INTERVAL_SECONDS),
        args=[app], id="scanner", replace_existing=True,
    )

    # Check outcomes every 5 minutes
    scheduler.add_job(
        check_outcomes, IntervalTrigger(seconds=SCAN_INTERVAL_SECONDS),
        args=[app], id="outcome_checker", replace_existing=True,
    )

    # Refresh coin list every N hours
    scheduler.add_job(
        coin_scanner.get_zero_fee_coins,
        IntervalTrigger(hours=COIN_REFRESH_HOURS),
        id="coin_refresh", replace_existing=True,
    )

    # Daily report at 23:55 UTC
    scheduler.add_job(
        tg.auto_daily_report, CronTrigger(hour=23, minute=55),
        args=[None], id="daily_report", replace_existing=True,
    )

    # Weekly report every Monday at 07:00 UTC
    scheduler.add_job(
        tg.auto_weekly_report, CronTrigger(day_of_week="mon", hour=7, minute=0),
        args=[None], id="weekly_report", replace_existing=True,
    )

    # Monthly report on 1st of month at 07:00 UTC
    scheduler.add_job(
        tg.auto_monthly_report, CronTrigger(day=1, hour=7, minute=0),
        args=[None], id="monthly_report", replace_existing=True,
    )

    return scheduler


# ─────────────────────────── main ───────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot...")

    # Initialise DB
    db.init_db()

    # Initial coin list load
    logger.info("Loading zero-fee coin list...")
    coins = coin_scanner.get_zero_fee_coins()
    logger.info(f"Tracking coins: {coins}")

    # Build telegram app
    app = tg.build_app()

    # Patch auto-report jobs to have the real app context
    # (APScheduler doesn't support passing Application easily through cron args,
    #  so we wrap them here using closures)
    scheduler = AsyncIOScheduler(timezone="UTC")

    scheduler.add_job(
        lambda: asyncio.ensure_future(scan_and_signal(app)),
        IntervalTrigger(seconds=SCAN_INTERVAL_SECONDS),
        id="scanner",
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(check_outcomes(app)),
        IntervalTrigger(seconds=SCAN_INTERVAL_SECONDS),
        id="outcome_checker",
    )
    scheduler.add_job(
        coin_scanner.get_zero_fee_coins,
        IntervalTrigger(hours=COIN_REFRESH_HOURS),
        id="coin_refresh",
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(tg.auto_daily_report(
            type("ctx", (), {"application": app})()
        )),
        CronTrigger(hour=23, minute=55),
        id="daily_report",
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(tg.auto_weekly_report(
            type("ctx", (), {"application": app})()
        )),
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="weekly_report",
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(tg.auto_monthly_report(
            type("ctx", (), {"application": app})()
        )),
        CronTrigger(day=1, hour=7, minute=0),
        id="monthly_report",
    )

    scheduler.start()
    logger.info("Scheduler started")

    # Start Telegram polling (blocks until shutdown)
    logger.info("Bot is running. Press Ctrl+C to stop.")
    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        try:
            await asyncio.Event().wait()  # run forever
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
