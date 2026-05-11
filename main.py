"""
Main entry point — NWE-RQK strategy.

Scheduler jobs:
  Every 1H (:01) — scan top-40 coin pool for NWE slope-flip signals
  Every 5 min    — check pending signal outcomes (TP/SL hit)
  Every 6h       — refresh coin pool
  23:55 daily    — daily report
  Mon 07:00      — weekly report
  1st 07:00      — monthly report
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
from mexc_client import get_klines
from config import (
    LKT,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    MAX_CONCURRENT_SIGNALS,
    LEVERAGE,
    NWE_TF,
    SCAN_CRON_MINUTES,
    SIGNALS_PER_SCAN,
    OUTCOME_CHECK_MINUTES,
    CANDLE_MINUTES,
    SCAN_WORKERS,
)

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mexc_bot.log"),
    ],
)
logging.Formatter.converter = lambda *args: datetime.now(LKT).timetuple()
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

    active = db.count_active_signals()
    slots  = MAX_CONCURRENT_SIGNALS - active
    if slots <= 0:
        logger.info(f"[SCAN] {active}/{MAX_CONCURRENT_SIGNALS} active signals, skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool, skipping")
        return

    now            = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)

    to_scan = [s for s in coins if not db.signal_exists_for_coin(s, cooldown_since)]
    logger.info(f"[SCAN] {len(to_scan)}/{len(coins)} coins after cooldown filter")

    def _analyze(symbol: str):
        try:
            return strategy.analyze_coin(symbol)
        except Exception as e:
            logger.error(f"[SCAN] {symbol} analysis error: {e}")
            return None

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        results = await loop.run_in_executor(
            None,
            lambda: list(ex.map(_analyze, to_scan)),
        )

    candidates = [s for s in results if s is not None]

    if not candidates:
        logger.info("[SCAN] Done — 0 signals found")
        return

    candidates.sort(key=lambda s: s.score, reverse=True)
    to_send = candidates[:min(SIGNALS_PER_SCAN, slots)]
    logger.info(
        f"[SCAN] {len(candidates)} signal(s) found, sending {len(to_send)} — "
        + ", ".join(f"{s.symbol}({s.score})" for s in candidates)
    )

    for sig in to_send:
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
            logger.info(f"[SCAN] Sent {sig.symbol} {sig.direction} score={sig.score}")
        except Exception as e:
            logger.error(f"Failed to broadcast {sig.symbol}: {e}")


# ── outcome checker ───────────────────────────────────────────────

async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now     = datetime.now(timezone.utc)

    for sig in pending:
        symbol    = sig["symbol"]
        direction = sig["direction"]
        tp_price  = sig["tp_price"]
        sl_price  = sig["sl_price"]
        entry_p   = sig["entry_price"]
        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info(f"Signal {sig['id']} expired ({symbol})")
            try:
                await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
            except Exception as e:
                logger.error(f"Failed to notify expiry for {symbol}: {e}")
            continue

        elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_klines(symbol, NWE_TF, count=fetch_count)
            if df.empty or len(df) < 2:
                continue
        except Exception as e:
            logger.warning(f"Could not fetch candles for {symbol}: {e}")
            continue

        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)

        outcome = None
        for i in range(len(df) - 1):
            if df.index[i] <= entry_candle_cutoff:
                continue

            h = float(df["high"].iloc[i])
            l = float(df["low"].iloc[i])
            o = float(df["open"].iloc[i])
            c = float(df["close"].iloc[i])

            if direction == "LONG":
                hit_tp = h >= tp_price
                hit_sl = l <= sl_price
            else:
                hit_tp = l <= tp_price
                hit_sl = h >= sl_price

            if hit_tp and hit_sl:
                outcome = (
                    ("win" if c >= o else "loss") if direction == "LONG"
                    else ("win" if c <= o else "loss")
                )
                break
            elif hit_tp:
                outcome = "win"
                break
            elif hit_sl:
                outcome = "loss"
                break

        if outcome is None:
            continue

        pnl = (
            sig["tp_price"] - sig["entry_price"]
        ) / sig["entry_price"] * LEVERAGE * 100 * (1 if outcome == "win" else -1)

        db.update_signal_outcome(sig["id"], outcome, pnl)
        logger.info(f"Signal {sig['id']} {outcome.upper()} ({symbol}) {pnl:+.1f}%")
        try:
            await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
        except Exception as e:
            logger.error(f"Failed to notify {outcome} for {symbol}: {e}")


# ── main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot — NWE Rational Quadratic Kernel (1H closes, scan every 5m)...")

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info(f"Signal pool: {len(coins)} coins")

    app = tg.build_app()

    scheduler = AsyncIOScheduler(timezone="UTC")

    # Scan every 5 minutes (catches each new 15M candle close promptly)
    scheduler.add_job(
        scan_and_signal,
        CronTrigger(minute=SCAN_CRON_MINUTES),
        args=[app], id="scanner",
    )

    scheduler.add_job(
        check_outcomes,
        IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
        args=[app], id="outcome_checker",
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

    scheduler.add_job(_daily,   CronTrigger(hour=23, minute=55), id="daily_report")
    scheduler.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7), id="monthly_report")

    scheduler.start()
    logger.info(f"Scheduler started — scanning {len(coins)} coins on {NWE_TF} every 5 minutes")

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
