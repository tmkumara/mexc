"""
Main entry point — VP-OB Confluence (4H Volume Profile + 1H Order Block).

Scheduler jobs:
  Hourly at :01   — scanner: arm/monitor Order Block setups, fire signal on retest
  Every 1 min     — outcome checker
  Every 6h        — coin pool refresh
  23:55 daily     — daily report
  Mon 07:00       — weekly report
  1st 07:00       — monthly report
"""

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date

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
    LEVERAGE,
    SIGNAL_TF,
    OB_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SETUP_SCAN_CRON_MINUTES,
    SETUP_SCAN_CRON_HOURS,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    SIGNALS_PER_SCAN,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
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
    STRATEGY_NAME,
)


def _backup_log_on_startup() -> None:
    if not ENABLE_LOG_BACKUP_ON_START:
        Path(LOG_FILE).touch(exist_ok=True)
        return
    log_path = Path(LOG_FILE)
    archive  = Path(LOG_BACKUP_DIR)
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

logger = logging.getLogger(__name__)


# ── Geometry guard ────────────────────────────────────────────────

def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry > sl
    if direction == "SHORT":
        return tp < entry < sl
    return False


# ── Signal scanner ────────────────────────────────────────────────

async def scan_and_fire_signals(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    db.expire_old_armed_setups(now)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    today_start    = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)

    # Daily cap
    signals_today = db.count_signals_since(today_start)
    if signals_today >= MAX_DAILY_SIGNALS:
        logger.info("[SCAN] Daily cap reached (%d/%d) — skipping", signals_today, MAX_DAILY_SIGNALS)
        return

    # Min gap between signals
    last_sig = db.latest_signal_time()
    if last_sig is not None:
        gap_seconds = (now - last_sig).total_seconds()
        if gap_seconds < MIN_DAILY_SIGNAL_GAP_MINUTES * 60:
            logger.info(
                "[SCAN] Min gap not met (%.0f / %d min) — skipping",
                gap_seconds / 60, MIN_DAILY_SIGNAL_GAP_MINUTES,
            )
            return

    # Concurrent signal cap
    active_signals = db.count_active_signals()
    slots = MAX_CONCURRENT_SIGNALS - active_signals
    if slots <= 0:
        logger.info("[SCAN] %d/%d active signals — no slots", active_signals, MAX_CONCURRENT_SIGNALS)
        return

    # Skip coins on cooldown
    to_scan: list[str] = []
    for symbol in coins:
        if db.signal_exists_for_coin(symbol, cooldown_since):
            continue
        to_scan.append(symbol)

    logger.info("[SCAN] Scanning %d/%d coins (active=%d slots=%d today=%d/%d)",
                len(to_scan), len(coins), active_signals, slots, signals_today, MAX_DAILY_SIGNALS)

    if not to_scan:
        return

    def _scan(symbol: str):
        try:
            return strategy.scan_symbol(symbol)
        except Exception as e:
            logger.error("[SCAN] %s error: %s", symbol, e, exc_info=True)
            return None

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None, lambda: list(executor.map(_scan, to_scan))
        )

    fired = 0
    signals_capped = MAX_DAILY_SIGNALS - signals_today

    for sig in results:
        if sig is None:
            continue
        if fired >= min(slots, SIGNALS_PER_SCAN, signals_capped):
            break

        # Re-check cooldown (race guard for parallel results)
        if db.signal_exists_for_coin(sig.symbol, cooldown_since):
            logger.debug("[SCAN] %s cooldown hit after parallel scan", sig.symbol)
            if sig.armed_setup_id is not None:
                db.mark_armed_setup_missed(sig.armed_setup_id, "cooldown hit after parallel scan")
            continue

        # Geometry validation (skill requirement)
        if not _valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            if sig.armed_setup_id is not None:
                db.mark_armed_setup_missed(sig.armed_setup_id, "geometry invalid post-scan")
            continue

        try:
            signal_id = db.save_signal(
                symbol=sig.symbol,
                direction=sig.direction,
                entry_price=sig.entry_price,
                tp_price=sig.tp_price,
                sl_price=sig.sl_price,
                leverage=sig.leverage,
                generated_at=sig.generated_at,
            )

            if sig.armed_setup_id is not None:
                db.mark_armed_setup_fired(sig.armed_setup_id, signal_id)

            await tg.broadcast_signal(app, sig, signal_id)
            fired += 1

            logger.info(
                "[SCAN] Fired #%d | %s %s @ %.6g TP=%.6g SL=%.6g RR=%.1f",
                signal_id, sig.symbol, sig.direction,
                sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )

        except Exception as e:
            logger.error("[SCAN] Failed to fire signal for %s: %s", sig.symbol, e, exc_info=True)
            if sig.armed_setup_id is not None:
                db.mark_armed_setup_missed(sig.armed_setup_id, f"post-scan save failed: {e}")

    logger.info("[SCAN] Done — %d signal(s) fired", fired)


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


async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol      = sig["symbol"]
        direction   = sig["direction"]
        tp_price    = sig["tp_price"]
        sl_price    = sig["sl_price"]
        entry_price = sig["entry_price"]

        # Geometry guard before outcome check (skill requirement)
        if not _valid_trade_geometry(direction, entry_price, tp_price, sl_price):
            logger.error(
                "[OUTCOME-BLOCK] Invalid signal geometry #%s %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig["id"], symbol, direction, entry_price, tp_price, sl_price,
            )
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
            df = get_klines(symbol, OB_TF, count=fetch_count)
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

            hit_tp = (high >= tp_price) if direction == "LONG" else (low  <= tp_price)
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
    logger.info("Starting MEXC Signal Bot — %s", STRATEGY_NAME)
    logger.info(
        "[CONFIG] coin pool: TOP_N=%s MIN_SELECTED=%s MIN_VOL=$%.0f COINGLASS=%s",
        TOP_N_COINS, COIN_POOL_MIN_SELECTED, COIN_POOL_MIN_VOLUME_USD,
        "SET" if COINGLASS_API_KEY else "EMPTY",
    )
    logger.info(
        "[CONFIG] OB TF=%s scan=%s/%s daily_cap=%d gap=%dmin cooldown=%dmin slots=%d",
        OB_TF, SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        SIGNAL_COOLDOWN_MINUTES, MAX_CONCURRENT_SIGNALS,
    )

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info("Coin pool: %d coins", len(coins))

    app = tg.build_app()

    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": SCHEDULER_MAX_INSTANCES,
            "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_SECONDS,
        },
    )

    # Signal scanner (hourly at :02 by default, aligns to 1h candle close)
    scheduler.add_job(
        scan_and_fire_signals,
        CronTrigger(hour=SETUP_SCAN_CRON_HOURS, minute=SETUP_SCAN_CRON_MINUTES),
        args=[app],
        id="signal_scanner",
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

    scheduler.add_job(_daily,   CronTrigger(hour=23, minute=55),        id="daily_report")
    scheduler.add_job(_weekly,  CronTrigger(day_of_week="mon", hour=7), id="weekly_report")
    scheduler.add_job(_monthly, CronTrigger(day=1, hour=7),             id="monthly_report")

    scheduler.start()

    logger.info(
        "Scheduler started — scan=%s/%s outcome=%dm",
        SETUP_SCAN_CRON_MINUTES, SETUP_SCAN_CRON_HOURS, OUTCOME_CHECK_MINUTES,
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
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
