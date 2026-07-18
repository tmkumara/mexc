"""
Main entry point — Simple Supertrend Pullback v1.

Scheduler jobs / background tasks:
  Every SCAN_INTERVAL_MINUTES (default 5m), a few seconds after candle
  close — scanner: evaluate every pooled coin against the 15m/5m strategy,
  apply the BTC safety filter, score, rank, and fire signals within the
  daily/gap/concurrent/direction limits.
  Every OUTCOME_CHECK_MINUTES — outcome checker (plain SL-first TP/SL).
  Every COIN_REFRESH_HOURS — coin pool refresh.
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

import pandas as pd

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import strategy
import bot as tg
import coin_scanner
import scalper_v3_strategy as v3
from outcome_check import check_tp_sl
from market_data import get_market_klines
from config import (
    LKT,
    LEVERAGE,
    TREND_TF,
    ENTRY_TF,
    CANDLE_MINUTES,
    SIGNAL_EXPIRE_HOURS,
    COIN_REFRESH_HOURS,
    SCAN_INTERVAL_MINUTES,
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
    TARGET_ROI_PCT,
    MAX_SL_ROI_PCT,
    DRY_RUN,
    DRY_RUN_SAVE_SIGNALS,
    SCALPER_V3_ENABLED,
    SCALPER_V3_TIMEFRAME,
    SCALPER_V3_SCAN_INTERVAL_MINUTES,
    SCALPER_V3_MAX_CONCURRENT_SIGNALS,
    SCALPER_V3_SIGNAL_COOLDOWN_MINUTES,
    SCALPER_V3_EXPIRE_HOURS,
    STRATEGY_NAME_V3,
    LIVE_ENABLED,
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
    cooldown_since = now - timedelta(minutes=SIGNAL_COOLDOWN_MINUTES)
    today_start    = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)

    signals_today = db.count_signals_since(today_start)
    if signals_today >= MAX_DAILY_SIGNALS:
        logger.info("[SCAN] Daily cap reached (%d/%d) — skipping", signals_today, MAX_DAILY_SIGNALS)
        return

    last_sig = db.latest_signal_time()
    if last_sig is not None and (now - last_sig).total_seconds() < MIN_DAILY_SIGNAL_GAP_MINUTES * 60:
        logger.info("[SCAN] Min signal gap not met — skipping")
        return

    active_signals = db.count_active_signals()
    slots = MAX_CONCURRENT_SIGNALS - active_signals
    if slots <= 0:
        logger.info("[SCAN] %d/%d active signals — no slots", active_signals, MAX_CONCURRENT_SIGNALS)
        return

    btc_context = strategy.build_btc_context()

    to_scan = [s for s in coins if not db.signal_exists_for_coin(s, cooldown_since)]

    # One private reject-reason dict per symbol -- each is written by exactly
    # one worker thread, so no shared-state locking is needed.
    reject_maps = [dict() for _ in to_scan]

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        results = await loop.run_in_executor(
            None,
            lambda: list(executor.map(
                lambda i: strategy.evaluate_symbol(to_scan[i], btc_context, reject_sink=reject_maps[i]),
                range(len(to_scan)),
            )),
        )

    candidates = sorted(
        (sig for sig in results if sig is not None),
        key=lambda sig: sig.score,
        reverse=True,
    )

    reject_counts: dict[str, int] = {}
    for m in reject_maps:
        for k, v in m.items():
            reject_counts[k] = reject_counts.get(k, 0) + v
    reject_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(reject_counts.items(), key=lambda kv: -kv[1])
    ) or "none"

    if not candidates:
        logger.info(
            "[SCAN] Done — %d coins scanned, no candidates | rejects: %s",
            len(to_scan), reject_summary,
        )
        return

    active_long  = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")

    fired = 0
    max_fire = min(slots, SIGNALS_PER_SCAN, MAX_DAILY_SIGNALS - signals_today)

    for sig in candidates:
        if fired >= max_fire:
            break

        if not strategy.direction_slot_available(sig.direction, active_long, active_short):
            logger.debug("[SCAN] %s %s blocked by direction limit", sig.symbol, sig.direction)
            continue

        if db.signal_exists_for_coin(sig.symbol, cooldown_since):
            logger.debug("[SCAN] %s cooldown hit after parallel scan", sig.symbol)
            continue

        if not strategy.valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price,
            )
            continue

        if DRY_RUN and not DRY_RUN_SAVE_SIGNALS:
            logger.info(
                "[DRY-RUN] Would fire | %s %s @ %.6g TP=%.6g SL=%.6g RR=%.2f score=%.1f",
                sig.symbol, sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, sig.rr, sig.score,
            )
            fired += 1
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

            fired += 1
            if sig.direction == "LONG":
                active_long += 1
            else:
                active_short += 1

            logger.info(
                "[SIGNAL] Fired #%d %s %s score=%.1f entry=%.6g tp=%.6g sl=%.6g rr=%.2f",
                signal_id, sig.symbol, sig.direction, sig.score,
                sig.entry_price, sig.tp_price, sig.sl_price, sig.rr,
            )

        except Exception as e:
            logger.error("[SCAN] Failed to fire signal for %s: %s", sig.symbol, e, exc_info=True)

    logger.info(
        "[SCAN] Done — %d/%d coins scanned, %d candidate(s), %d fired | rejects: %s",
        len(to_scan), len(coins), len(candidates), fired, reject_summary,
    )


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

        if not strategy.valid_trade_geometry(direction, entry_price, tp_price, sl_price):
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

        outcome = check_tp_sl(direction, entry_price, tp_price, sl_price, df, entry_candle_cutoff)
        if outcome is None:
            continue

        pnl = _calculate_pnl_roi(direction, outcome, entry_price, tp_price, sl_price)
        db.update_signal_outcome(sig["id"], outcome, pnl)
        logger.info("Signal %s %s (%s) %+.1f%%", sig["id"], outcome.upper(), symbol, pnl)

        if not DRY_RUN:
            try:
                await tg.notify_outcome(app, {**sig, "status": outcome, "pnl_roi": pnl})
            except Exception as e:
                logger.error("Failed to notify %s for %s: %s", outcome, symbol, e)


# ── Super Scalper v3 scanner (OFF by default — SCALPER_V3_ENABLED) ──
#
# Additive and independent of the v1 scan/outcome loop above: separate
# strategy_name, separate cooldown/concurrency limits, separate DB rows.
# LIVE_ENABLED gates real order intent the same way DRY_RUN gates v1 --
# with LIVE_ENABLED=false this only ever logs candidates/skips and (if
# DRY_RUN is also false) sends a paper-trade Telegram alert, never more.

async def scan_and_fire_signals_v3(app: Application) -> None:
    if not SCALPER_V3_ENABLED:
        return
    if tg.paused:
        logger.info("[SCAN-V3] Paused — skipping")
        return

    coins = coin_scanner.get_cached_coins()
    if not coins:
        logger.warning("[SCAN-V3] Empty coin pool — skipping")
        return

    now = datetime.now(timezone.utc)
    cooldown_since = now - timedelta(minutes=SCALPER_V3_SIGNAL_COOLDOWN_MINUTES)

    active = db.count_active_signals_by_strategy(STRATEGY_NAME_V3)
    slots = SCALPER_V3_MAX_CONCURRENT_SIGNALS - active
    if slots <= 0:
        logger.info("[SCAN-V3] %d/%d active v3 signals — no slots", active, SCALPER_V3_MAX_CONCURRENT_SIGNALS)
        return

    to_scan = [s for s in coins if not db.signal_exists_for_coin_strategy(s, cooldown_since, STRATEGY_NAME_V3)]

    fired = 0
    skipped = 0
    for symbol in to_scan:
        if fired >= slots:
            break
        try:
            result = v3.evaluate_symbol_v3(symbol)
        except Exception as e:
            logger.error("[SCAN-V3] %s eval failed: %s", symbol, e, exc_info=True)
            continue

        if result is None:
            continue

        if isinstance(result, v3.SkippedSignal):
            skipped += 1
            db.save_skipped_signal(
                symbol=result.symbol, reason=result.reason, generated_at=result.generated_at,
                direction=result.direction, strategy_name=STRATEGY_NAME_V3,
                trend=result.details.get("trend"), strength=result.details.get("strength"),
                ao=result.details.get("ao"), kc_pos=result.details.get("kc_pos"),
                regime=result.details.get("regime"), regime_votes=result.details.get("regime_votes"),
                adx=result.details.get("adx"), chop=result.details.get("chop"),
            )
            logger.debug("[SCAN-V3-BLOCKED] %s %s: %s", symbol, result.direction, result.reason)
            continue

        signal_id = db.save_v3_signal(
            symbol=result.symbol, direction=result.direction,
            entry_price=result.entry_price, sl_price=result.sl_price,
            tp1_price=result.tp1_price, tp2_price=result.tp2_price,
            generated_at=result.generated_at, strategy_name=STRATEGY_NAME_V3,
            trend=result.trend, strength=result.strength, ao=result.ao, kc_pos=result.kc_pos,
            regime=result.regime, regime_votes=result.regime_votes, adx=result.adx, chop=result.chop,
            setup_reason=result.setup_reason,
        )

        if not DRY_RUN:
            try:
                await tg.broadcast_v3_signal(app, result, signal_id)
            except Exception as e:
                logger.error("[SCAN-V3] Telegram broadcast failed for %s: %s", symbol, e)

        fired += 1
        logger.info(
            "[SIGNAL-V3] Fired #%d %s %s entry=%.6g tp1=%.6g tp2=%.6g sl=%.6g live_enabled=%s",
            signal_id, result.symbol, result.direction,
            result.entry_price, result.tp1_price, result.tp2_price, result.sl_price, LIVE_ENABLED,
        )

    logger.info(
        "[SCAN-V3] Done — %d coins scanned, %d fired, %d skipped-and-logged",
        len(to_scan), fired, skipped,
    )


async def check_outcomes_v3(app: Application) -> None:
    if not SCALPER_V3_ENABLED:
        return

    pending = db.get_pending_signals_by_strategy(STRATEGY_NAME_V3)
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        entry_price = sig["entry_price"]
        tp1_price = sig["tp1_price"]
        tp2_price = sig["tp2_price"]
        sl_price = sig["sl_price"]
        generated = datetime.fromisoformat(sig["generated_at"])
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SCALPER_V3_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info("[V3] Signal %s expired (%s)", sig["id"], symbol)
            if not DRY_RUN:
                try:
                    await tg.notify_outcome(app, {**sig, "status": "expired", "pnl_roi": 0.0})
                except Exception as e:
                    logger.error("[V3] Failed to notify expiry for %s: %s", symbol, e)
            continue

        try:
            df = v3.update_rolling_history(symbol)
            if df is None or df.empty:
                continue
            computed = v3.get_engine(symbol).compute(df)
        except Exception as e:
            logger.warning("[V3] Could not compute candles for %s: %s", symbol, e)
            continue

        bars_after_entry = computed[computed.index > pd.Timestamp(generated).tz_localize(None)]
        if bars_after_entry.empty:
            continue

        result = v3.walk_trade(direction, entry_price, sl_price, tp1_price, tp2_price, bars_after_entry)

        if result["tp1_hit"] and sig.get("tp1_hit_at") is None:
            db.mark_signal_tp1_hit(sig["id"], now, max(sl_price, entry_price) if direction == "LONG" else min(sl_price, entry_price))

        if result["status"] == "pending":
            continue

        pnl_pct = (
            (result["exit_price"] - entry_price) / entry_price * 100.0
            if direction == "LONG"
            else (entry_price - result["exit_price"]) / entry_price * 100.0
        )
        db.update_signal_outcome(sig["id"], result["status"], round(pnl_pct, 4))
        logger.info(
            "[V3] Signal %s %s (%s) exit=%s reason=%s %+.2f%%",
            sig["id"], result["status"].upper(), symbol, result["exit_price"], result["exit_reason"], pnl_pct,
        )
        if not DRY_RUN:
            try:
                await tg.notify_outcome(app, {**sig, "status": result["status"], "pnl_roi": pnl_pct})
            except Exception as e:
                logger.error("[V3] Failed to notify %s for %s: %s", result["status"], symbol, e)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    logger.info("Starting MEXC Signal Bot")
    logger.info("Strategy: %s", STRATEGY_NAME)
    logger.info("Trend TF: %s", TREND_TF)
    logger.info("Entry TF: %s", ENTRY_TF)
    logger.info("Target ROI: %.0f%%", TARGET_ROI_PCT)
    logger.info("Max SL ROI: %.0f%%", MAX_SL_ROI_PCT)
    logger.info("Leverage: %dx", LEVERAGE)
    logger.info("Dry run: %s", "enabled" if DRY_RUN else "disabled")
    logger.info(
        "[CONFIG] coin pool: TOP_N=%s MIN_SELECTED=%s MIN_VOL=$%.0f COINGLASS=%s",
        TOP_N_COINS, COIN_POOL_MIN_SELECTED, COIN_POOL_MIN_VOLUME_USD,
        "SET" if COINGLASS_API_KEY else "EMPTY",
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

    # Signal scanner -- every SCAN_INTERVAL_MINUTES, a few seconds after
    # candle close so MEXC has finalized the candle.
    scheduler.add_job(
        scan_and_fire_signals,
        CronTrigger(minute=f"*/{SCAN_INTERVAL_MINUTES}", second=5),
        args=[app],
        id="signal_scanner",
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

    if SCALPER_V3_ENABLED:
        logger.info(
            "[V3] Super Scalper v3 scanner ENABLED (paper-trade only, live_enabled=%s)",
            LIVE_ENABLED,
        )
        scheduler.add_job(
            scan_and_fire_signals_v3,
            CronTrigger(minute=f"*/{SCALPER_V3_SCAN_INTERVAL_MINUTES}", second=10),
            args=[app],
            id="signal_scanner_v3",
        )
        scheduler.add_job(
            check_outcomes_v3,
            IntervalTrigger(minutes=OUTCOME_CHECK_MINUTES),
            args=[app],
            id="outcome_checker_v3",
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
