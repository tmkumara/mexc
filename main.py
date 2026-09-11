"""
Main entry point — SMC Structure + Squeeze Momentum v1.

Scheduler jobs / background tasks:
  Every SCAN_INTERVAL_MINUTES (default 15m) — scan_and_fire_signals: scans
  the hardcoded coin whitelist (config.WHITELISTED_COINS) for a fresh
  structure-break + squeeze-momentum confluence on ENTRY_TF (30m), and
  fires signals directly (no pending-setup state machine, unlike the
  retired Zero-Lag MTF Pullback v1) within the daily/gap/concurrent/
  direction limits.
  Every OUTCOME_CHECK_MINUTES — outcome checker (plain single TP/SL, no
  breakeven).
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
from outcome_check import check_tp_sl
from market_data import get_market_klines
from config import (
    LKT,
    LEVERAGE,
    TREND_TF,
    ENTRY_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    SCAN_INTERVAL_MINUTES,
    OUTCOME_CHECK_MINUTES,
    MAX_CONCURRENT_SIGNALS,
    SIGNAL_COOLDOWN_MINUTES,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCAN_WORKERS,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    STRATEGY_NAME,
    SIGNAL_SCORE_THRESHOLD,
    RISK_REWARD_RATIO,
    MIN_WIN_ROI_PCT,
    WHITELISTED_COINS,
    DRY_RUN,
    DRY_RUN_SAVE_SIGNALS,
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


# ── Signal scanner (15m) ──────────────────────────────────────────

async def scan_and_fire_signals(app: Application) -> None:
    if tg.paused:
        logger.info("[SCAN] Paused — skipping")
        return

    loop = asyncio.get_event_loop()
    pairs = await loop.run_in_executor(None, coin_scanner.get_whitelisted_pairs)
    if not pairs:
        logger.warning("[SCAN] Whitelist resolved to 0 pairs — skipping")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    to_scan = [s for s in pairs if not db.signal_exists_for_coin(s, cooldown_since)]

    reject_maps = [dict() for _ in to_scan]
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(
                lambda i: strategy.detect_signal(to_scan[i], reject_sink=reject_maps[i]),
                range(len(to_scan)),
            )),
        )

    candidates = [s for s in results if s is not None]
    candidates.sort(key=lambda s: s.score, reverse=True)

    reject_counts: dict[str, int] = {}
    for m in reject_maps:
        for k, v in m.items():
            reject_counts[k] = reject_counts.get(k, 0) + v
    reject_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(reject_counts.items(), key=lambda kv: -kv[1])
    ) or "none"

    logger.info(
        "[SCAN] Done — %d/%d pairs scanned, %d candidates | rejects: %s",
        len(to_scan), len(pairs), len(candidates), reject_summary,
    )

    if not candidates:
        return

    active_signals = db.count_active_signals()
    active_long = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")
    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig = db.latest_signal_time()

    for sig in candidates:
        slots = MAX_CONCURRENT_SIGNALS - active_signals
        daily_ok = signals_today < MAX_DAILY_SIGNALS
        gap_ok = (
            last_sig is None
            or (now - last_sig).total_seconds() >= MIN_DAILY_SIGNAL_GAP_MINUTES * 60
        )
        direction_ok = strategy.direction_slot_available(sig.direction, active_long, active_short)

        if slots <= 0 or not daily_ok or not gap_ok or not direction_ok:
            logger.info(
                "[SIGNAL-SKIP] %s %s budget/slot unavailable (slots=%d daily_ok=%s gap_ok=%s direction_ok=%s)",
                sig.symbol, sig.direction, slots, daily_ok, gap_ok, direction_ok,
            )
            continue

        if not strategy.valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            continue

        if DRY_RUN and not DRY_RUN_SAVE_SIGNALS:
            logger.info(
                "[DRY-RUN] Would fire | %s %s @ %.6g TP=%.6g SL=%.6g score=%.1f",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, sig.score,
            )
            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1
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
                strategy_name=STRATEGY_NAME,
                score=sig.score,
                rr=sig.rr,
                entry_timeframe=ENTRY_TF,
                trend_timeframe=TREND_TF,
                setup_reason=sig.timeframe_summary,
            )

            if not DRY_RUN:
                await tg.broadcast_signal(app, sig, signal_id)

            active_signals += 1
            signals_today += 1
            last_sig = now
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1

            logger.info(
                "[SIGNAL] Fired #%d %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=1:%.0f",
                signal_id, sig.symbol, sig.direction, sig.score,
                sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )
        except Exception as e:
            logger.error("[SCAN] Failed to fire signal for %s: %s", sig.symbol, e, exc_info=True)


# ── Outcome checker ───────────────────────────────────────────────

async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        entry_price = sig["entry_price"]
        sl_price = sig["sl_price"]
        tp_price = sig["tp_price"]

        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info("Signal %s expired (%s)", sig["id"], symbol)
            if not DRY_RUN:
                try:
                    await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
                except Exception as e:
                    logger.error("Failed to notify expiry for %s: %s", symbol, e)
            continue

        elapsed_min = max((now - generated).total_seconds() / 60, CANDLE_MINUTES)
        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = get_market_klines(symbol, ENTRY_TF, count=fetch_count)
            if df is None or df.empty or len(df) < 2:
                continue
        except Exception as e:
            logger.warning("Could not fetch candles for %s: %s", symbol, e)
            continue

        entry_candle_cutoff = (generated - timedelta(minutes=CANDLE_MINUTES)).replace(tzinfo=None)

        result = check_tp_sl(direction, entry_price, sl_price, tp_price, df, entry_candle_cutoff)
        if result is None:
            continue

        pnl = result["pnl_roi_pct"] * LEVERAGE

        db.update_signal_outcome(sig["id"], result["status"], pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], result["status"].upper(), symbol, pnl)

        if not DRY_RUN:
            try:
                await tg.notify_outcome(app, {**sig, "status": result["status"], "pnl_roi": pnl})
            except Exception as e:
                logger.error("Failed to notify %s for %s: %s", result["status"], symbol, e)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot")
    logger.info("Strategy: %s", STRATEGY_NAME)
    logger.info("Entry/Trend TF: %s / %s", ENTRY_TF, TREND_TF)
    logger.info("Whitelisted coins: %s", ", ".join(WHITELISTED_COINS))
    logger.info("Min signal score: %.1f/10", SIGNAL_SCORE_THRESHOLD)
    logger.info("RR: 1:%.0f  Min win ROI: %.0f%%  Leverage: %dx", RISK_REWARD_RATIO, MIN_WIN_ROI_PCT, LEVERAGE)
    logger.info("Dry run: %s", "enabled" if DRY_RUN else "disabled")

    db.init_db()

    app = tg.build_app()

    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": SCHEDULER_MAX_INSTANCES,
            "misfire_grace_time": SCHEDULER_MISFIRE_GRACE_SECONDS,
        },
    )

    scheduler.add_job(
        scan_and_fire_signals,
        IntervalTrigger(minutes=SCAN_INTERVAL_MINUTES),
        args=[app],
        id="signal_scanner",
    )

    scheduler.add_job(
        check_outcomes,
        IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
        args=[app],
        id="outcome_checker",
    )

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
        "Scheduler started — scan every %dm, outcome every %dm",
        SCAN_INTERVAL_MINUTES, OUTCOME_CHECK_MINUTES,
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
