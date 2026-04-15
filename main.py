"""
Main entry point.

Scheduler jobs:
  • Every 1h (candle-aligned)  → scan all pairs for Supertrend signals
  • Every 15 min               → check pending signal outcomes (TP / SL)
  • 23:55 daily                → post daily report
  • Mon 07:00                  → post weekly report
  • 1st 07:00                  → post monthly report
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
import strategy
import bot as tg
import coin_scanner
from mexc_client import get_current_price
from config import (
    SIGNAL_COOLDOWN_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mexc_bot.log"),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── scanner job ───────────────────────────────────────────────────

async def scan_and_signal(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused, skipping")
        return

    pairs          = coin_scanner.get_cached_coins()
    now            = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    signals_found  = 0

    logger.info(f"[SCAN] Scanning {len(pairs)} pairs...")

    for symbol in pairs:
        if db.signal_exists_for_coin(symbol, cooldown_since):
            logger.debug(f"[SCAN] {symbol}: cooldown active, skipping")
            continue

        sig = strategy.analyze_coin(symbol)
        if sig is None:
            continue

        signals_found += 1
        signal_id = db.save_signal(
            symbol       = sig.symbol,
            direction    = sig.direction,
            entry_price  = sig.entry_price,
            tp_price     = sig.tp_price,
            sl_price     = sig.sl_price,
            leverage     = sig.leverage,
            generated_at = sig.generated_at,
        )

        try:
            await tg.broadcast_signal(app, sig, signal_id)
        except Exception as e:
            logger.error(f"Failed to broadcast signal for {symbol}: {e}")

    logger.info(f"[SCAN] Done — {signals_found} signal(s) sent")


# ── outcome checker ───────────────────────────────────────────────

async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now     = datetime.now(timezone.utc)

    for sig in pending:
        symbol    = sig["symbol"]
        direction = sig["direction"]
        tp_price  = sig["tp_price"]
        sl_price  = sig["sl_price"]
        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        # Expire stale signals
        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info(f"Signal {sig['id']} expired ({symbol})")
            try:
                await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
            except Exception as e:
                logger.error(f"Failed to notify expiry for {symbol}: {e}")
            continue

        price = get_current_price(symbol)
        if price is None:
            continue

        hit_tp = hit_sl = False
        if direction == "LONG":
            hit_tp = price >= tp_price
            hit_sl = price <= sl_price
        else:
            hit_tp = price <= tp_price
            hit_sl = price >= sl_price

        if hit_tp:
            pnl = abs(tp_price / sig["entry_price"] - 1) * sig["leverage"] * 100
            if direction == "SHORT":
                pnl = abs(1 - tp_price / sig["entry_price"]) * sig["leverage"] * 100
            db.update_signal_outcome(sig["id"], "win", pnl)
            logger.info(f"Signal {sig['id']} WIN ({symbol}) +{pnl:.1f}%")
            try:
                await tg.notify_outcome(app, {**sig, "status": "win", "pnl_roi": pnl})
            except Exception as e:
                logger.error(f"Failed to notify win for {symbol}: {e}")

        elif hit_sl:
            pnl = -abs(sig["entry_price"] / sl_price - 1) * sig["leverage"] * 100
            if direction == "SHORT":
                pnl = -abs(1 - sl_price / sig["entry_price"]) * sig["leverage"] * 100
            db.update_signal_outcome(sig["id"], "loss", pnl)
            logger.info(f"Signal {sig['id']} LOSS ({symbol}) {pnl:.1f}%")
            try:
                await tg.notify_outcome(app, {**sig, "status": "loss", "pnl_roi": pnl})
            except Exception as e:
                logger.error(f"Failed to notify loss for {symbol}: {e}")


# ── main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot (Supertrend strategy)...")

    db.init_db()

    logger.info("Loading zero-fee coin list...")
    coins = coin_scanner.get_zero_fee_coins()
    logger.info(f"Tracking {len(coins)} pairs: {coins}")

    app = tg.build_app()

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Scan at the top of every hour (1h candle close)
    scheduler.add_job(
        scan_and_signal, CronTrigger(minute=1),
        args=[app], id="scanner",
    )

    # Check outcomes every 15 minutes
    scheduler.add_job(
        check_outcomes, IntervalTrigger(minutes=15),
        args=[app], id="outcome_checker",
    )

    # Refresh zero-fee coin list every N hours
    scheduler.add_job(
        coin_scanner.get_zero_fee_coins,
        CronTrigger(hour=f"*/{COIN_REFRESH_HOURS}"),
        id="coin_refresh",
    )

    async def _daily(app=app):
        await tg.auto_daily_report(type("ctx", (), {"application": app})())

    async def _weekly(app=app):
        await tg.auto_weekly_report(type("ctx", (), {"application": app})())

    async def _monthly(app=app):
        await tg.auto_monthly_report(type("ctx", (), {"application": app})())

    scheduler.add_job(_daily,   CronTrigger(hour=23, minute=55), id="daily_report")
    scheduler.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7), id="monthly_report")

    scheduler.start()
    logger.info(f"Scheduler started. Watching {len(coins)} pairs on 1h.")

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
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
