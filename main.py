"""
Main entry point — Stateful SMC Liquidity Sweep + Order Block Retest strategy.

Scheduler jobs:
  Every 5 min     — full setup detection scan
  Every 1 min     — monitor pending setups for OB retest entries
  Every 1 min     — check pending signal outcomes
  Every 6h        — refresh coin pool
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
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import database as db
import strategy
import bot as tg
import coin_scanner

try:
    from candle_cache import CandleCache
    from mexc_ws_client import MexcWebSocketClient
except Exception:  # keep REST-only mode safe if optional WS deps are missing
    CandleCache = None
    MexcWebSocketClient = None

from mexc_client import get_klines
from config import (
    LKT,
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
    MAX_NEW_SETUPS_PER_SCAN,
    MAX_SETUPS_SAME_DIRECTION_PER_SCAN,
    MAX_WAITING_SETUPS_TOTAL,
    MAX_WAITING_SETUPS_SAME_DIRECTION,
    SETUP_MONITOR_LIMIT,
    MAX_DAILY_SIGNALS,
    MIN_DAILY_SIGNAL_GAP_MINUTES,
    SCHEDULER_MISFIRE_GRACE_SECONDS,
    SCHEDULER_MAX_INSTANCES,
    LOG_FILE,
    ENABLE_LOG_BACKUP_ON_START,
    LOG_BACKUP_DIR,
    ENABLE_WS_CANDLE_CACHE,
    CANDLE_CACHE_LIMIT,
    WS_MAX_SYMBOLS,
    WS_SEED_KLINE_COUNT,
    MARKET_WINDOW_MINUTES,
    MAX_SAME_DIRECTION_SIGNALS_PER_WINDOW,
    ENABLE_DIRECTION_IMBALANCE_ALERT,
    DIRECTION_IMBALANCE_THRESHOLD,
    CORRELATION_BRAKE_KEEP_WAITING,
    SYMBOL_LOSS_COOLDOWN_HOURS,
    SYMBOL_DIRECTION_MAX_LOSSES_7D,
    SYMBOL_DIRECTION_BLOCK_DAYS,
    DIRECTION_IMBALANCE_MIN_SETUPS,
)


def _backup_log_on_startup() -> None:
    """Backup previous log on each process start and create a fresh log file."""
    if not ENABLE_LOG_BACKUP_ON_START:
        Path(LOG_FILE).touch(exist_ok=True)
        return

    log_path = Path(LOG_FILE)
    archive_dir = Path(LOG_BACKUP_DIR)
    archive_dir.mkdir(parents=True, exist_ok=True)

    if log_path.exists() and log_path.stat().st_size > 0:
        ts = datetime.now(LKT).strftime("%Y%m%d_%H%M%S")
        backup_name = f"{log_path.stem}_{ts}{log_path.suffix or '.log'}"
        shutil.copy2(log_path, archive_dir / backup_name)
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


def _valid_trade_geometry(direction: str, entry: float, tp: float, sl: float) -> bool:
    if entry <= 0 or tp <= 0 or sl <= 0:
        return False
    if direction == "LONG":
        return tp > entry and sl < entry
    if direction == "SHORT":
        return tp < entry and sl > entry
    return False


# ── WebSocket candle cache ────────────────────────────────────────

CANDLE_CACHE = CandleCache(limit=CANDLE_CACHE_LIMIT) if CandleCache is not None else None
WS_CLIENT = None
WS_TASK: asyncio.Task | None = None


def _cached_or_rest_klines(symbol: str, interval: str, count: int):
    """Return candles from WebSocket cache when ready, otherwise REST fallback.

    Requires len(df) >= count: a partial cache (smaller than requested) risks
    indicator warm-up failures (EMA200 needs 200+ bars) so we fall back to REST.
    """
    if CANDLE_CACHE is not None:
        try:
            df = CANDLE_CACHE.get_candles(symbol, interval, limit=count)
            if df is not None and not df.empty and len(df) >= count:
                return df
        except Exception as e:
            logger.debug("[CACHE] %s %s read failed: %s", symbol, interval, e)

    return get_klines(symbol, interval, count=count)


def _seed_candle_cache(symbols: list[str], intervals: list[str], count: int) -> None:
    """
    Seed WebSocket cache with REST history before live updates arrive.

    This keeps the first monitor/outcome cycle usable immediately after startup.
    """
    if CANDLE_CACHE is None:
        return

    for symbol in symbols:
        for interval in intervals:
            try:
                df = get_klines(symbol, interval, count=count)
                if df is not None and not df.empty:
                    CANDLE_CACHE.seed(symbol, interval, df)
            except Exception as e:
                logger.warning("[CACHE] Failed to seed %s %s: %s", symbol, interval, e)


async def _start_ws_cache(coins: list[str]) -> None:
    """
    Start MEXC kline WebSocket for ENTRY_TF candles.

    REST remains fallback. We intentionally subscribe only the top WS_MAX_SYMBOLS coins
    to avoid connection/subscription pressure. Active/waiting symbols outside this pool
    still work through REST fallback.
    """
    global WS_CLIENT, WS_TASK

    if not ENABLE_WS_CANDLE_CACHE:
        logger.info("[WS] Candle cache disabled by ENABLE_WS_CANDLE_CACHE=false")
        return

    if CANDLE_CACHE is None or MexcWebSocketClient is None:
        logger.warning("[WS] Candle cache unavailable; check candle_cache.py, mexc_ws_client.py, websockets package")
        return

    symbols = list(dict.fromkeys(coins[:WS_MAX_SYMBOLS]))

    if not symbols:
        logger.warning("[WS] No symbols available for WebSocket subscription")
        return

    # Make strategy.py use cache first without requiring a strategy.py change.
    # strategy.py imports get_klines directly, so patch that module-level reference.
    strategy.get_klines = _cached_or_rest_klines

    # If a future strategy.py exposes set_candle_cache(), support it too.
    if hasattr(strategy, "set_candle_cache"):
        strategy.set_candle_cache(CANDLE_CACHE)

    logger.info(
        "[WS] Seeding candle cache symbols=%d interval=%s count=%d",
        len(symbols),
        ENTRY_TF,
        WS_SEED_KLINE_COUNT,
    )
    _seed_candle_cache(symbols, [ENTRY_TF], WS_SEED_KLINE_COUNT)

    WS_CLIENT = MexcWebSocketClient(
        candle_cache=CANDLE_CACHE,
        symbols=symbols,
        app_intervals=[ENTRY_TF],
    )
    WS_TASK = asyncio.create_task(WS_CLIENT.start(), name="mexc_ws_client")
    logger.info("[WS] Started kline WebSocket cache for %d symbols on %s", len(symbols), ENTRY_TF)


async def _stop_ws_cache() -> None:
    global WS_CLIENT, WS_TASK

    if WS_CLIENT is not None:
        try:
            await WS_CLIENT.stop()
        except Exception:
            logger.debug("[WS] Error while stopping client", exc_info=True)

    if WS_TASK is not None:
        WS_TASK.cancel()
        try:
            await WS_TASK
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[WS] Task stopped with error", exc_info=True)

    WS_CLIENT = None
    WS_TASK = None


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
    loss_cooldown_since = now - timedelta(hours=SYMBOL_LOSS_COOLDOWN_HOURS)
    direction_block_since = now - timedelta(days=SYMBOL_DIRECTION_BLOCK_DAYS)

    # Pre-filter: skip coins on signal cooldown, already waiting, or on symbol loss cooldown.
    to_scan: list[str] = []
    for symbol in coins:
        if db.signal_exists_for_coin(symbol, cooldown_since):
            continue
        if db.pending_setup_exists(symbol):
            continue
        if SYMBOL_LOSS_COOLDOWN_HOURS > 0:
            if db.count_losses_since(symbol, None, loss_cooldown_since) > 0:
                logger.info(
                    "[SETUP-SCAN] Skip %s — loss cooldown (%dh)",
                    symbol, SYMBOL_LOSS_COOLDOWN_HOURS,
                )
                continue
        to_scan.append(symbol)

    logger.info("[SETUP-SCAN] %d/%d coins after cooldown + waiting-setup + loss-cooldown filters", len(to_scan), len(coins))

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

    # Post-detect: apply per-direction loss block.
    filtered_setups = []
    for setup in results:
        if setup is None:
            continue

        symbol    = setup["symbol"]
        direction = setup["direction"]

        if SYMBOL_DIRECTION_MAX_LOSSES_7D > 0:
            dir_losses = db.count_losses_since(symbol, direction, direction_block_since)
            if dir_losses >= SYMBOL_DIRECTION_MAX_LOSSES_7D:
                logger.info(
                    "[SETUP-SCAN] Skip %s %s — %d %s losses in %dd direction block",
                    symbol, direction, dir_losses, direction, SYMBOL_DIRECTION_BLOCK_DAYS,
                )
                continue

        filtered_setups.append(setup)

    setups = filtered_setups

    if not setups:
        logger.info("[SETUP-SCAN] Done — 0 new setups found")
        return

    setups.sort(key=lambda setup: float(setup.get("score", 0.0)), reverse=True)

    waiting_total = db.count_waiting_setups()
    waiting_by_direction = db.count_waiting_setups_by_direction()
    waiting_slots = max(MAX_WAITING_SETUPS_TOTAL - waiting_total, 0)
    scan_limit = min(MAX_NEW_SETUPS_PER_SCAN, waiting_slots)

    if scan_limit <= 0:
        logger.info(
            "[SETUP-SCAN] Waiting setup limit reached (%d/%d), skipping save",
            waiting_total,
            MAX_WAITING_SETUPS_TOTAL,
        )
        return

    saved = 0
    direction_counts: dict[str, int] = {}

    for setup in setups:
        if saved >= scan_limit:
            break

        direction = setup["direction"]

        if direction_counts.get(direction, 0) >= MAX_SETUPS_SAME_DIRECTION_PER_SCAN:
            logger.info(
                "[SETUP-SCAN] Skip %s %s — same-direction scan limit %d reached",
                setup["symbol"],
                direction,
                MAX_SETUPS_SAME_DIRECTION_PER_SCAN,
            )
            continue

        current_same_direction = waiting_by_direction.get(direction, 0) + direction_counts.get(direction, 0)
        if current_same_direction >= MAX_WAITING_SETUPS_SAME_DIRECTION:
            logger.info(
                "[SETUP-SCAN] Skip %s %s — waiting same-direction cap %d reached",
                setup["symbol"],
                direction,
                MAX_WAITING_SETUPS_SAME_DIRECTION,
            )
            continue

        setup_id = db.save_pending_setup(setup)

        if setup_id:
            saved += 1
            direction_counts[direction] = direction_counts.get(direction, 0) + 1
            logger.info(
                "[SETUP-SCAN] Saved setup #%s %s %s OB=%.6g-%.6g score=%.1f",
                setup_id,
                setup["symbol"],
                setup["direction"],
                float(setup["ob_low"]),
                float(setup["ob_high"]),
                float(setup.get("score", 0.0)),
            )

    logger.info(
        "[SETUP-SCAN] Done — %d/%d setups saved (scan_limit=%d, waiting_before=%d/%d, waiting_dir=%s, saved_dir=%s)",
        saved,
        len(setups),
        scan_limit,
        waiting_total,
        MAX_WAITING_SETUPS_TOTAL,
        waiting_by_direction,
        direction_counts,
    )

    if ENABLE_DIRECTION_IMBALANCE_ALERT and saved > 0:
        final_waiting = db.count_waiting_setups_by_direction()
        total_w = sum(final_waiting.values())
        if total_w >= DIRECTION_IMBALANCE_MIN_SETUPS:
            for dir_name, count in final_waiting.items():
                ratio = count / total_w
                if ratio >= DIRECTION_IMBALANCE_THRESHOLD:
                    logger.warning(
                        "[SETUP-SCAN] Direction imbalance: %d/%d (%.0f%%) waiting setups are %s",
                        count, total_w, ratio * 100, dir_name,
                    )


# ── pending setup monitor ─────────────────────────────────────────

async def monitor_setups(app: Application) -> None:
    if tg.paused:
        logger.info("[SETUP-MONITOR] Paused, skipping")
        return

    active = db.count_active_signals()
    slots = MAX_CONCURRENT_SIGNALS - active

    if slots <= 0:
        logger.info("[SETUP-MONITOR] %d/%d active signals, skipping", active, MAX_CONCURRENT_SIGNALS)
        return

    now = datetime.now(timezone.utc)
    db.expire_old_waiting_setups(now)

    setups = db.get_waiting_setups(limit=SETUP_MONITOR_LIMIT)

    if not setups:
        logger.info("[SETUP-MONITOR] No waiting setups")
        return

    logger.info("[SETUP-MONITOR] Checking top %d waiting setups", len(setups))

    # Evaluate daily cap + gap once upfront. Claiming only happens when we can send —
    # this prevents duplicate [ENTRY] logs across successive monitor cycles for the same setup.
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = db.count_signals_since(today_start)
    remaining_daily_slots = max(MAX_DAILY_SIGNALS - today_count, 0)

    latest = db.latest_signal_time()
    gap_ok = (
        latest is None
        or (now - latest).total_seconds() / 60 >= MIN_DAILY_SIGNAL_GAP_MINUTES
    )

    can_send = remaining_daily_slots > 0 and gap_ok

    fired_signals: list[tuple[dict, strategy.Signal]] = []

    for setup in setups:
        status, sig = strategy.evaluate_pending_setup(setup)

        if status == "EXPIRED":
            db.mark_setup_expired(setup["id"])
            logger.info("[SETUP-MONITOR] Expired setup #%d %s", setup["id"], setup["symbol"])

        elif status == "INVALIDATED":
            db.mark_setup_invalidated(setup["id"])
            logger.info("[SETUP-MONITOR] Invalidated setup #%d %s", setup["id"], setup["symbol"])

        elif status == "FIRE" and sig is not None:
            if not can_send:
                # Daily cap or gap prevents sending — leave setup 'waiting' so
                # the next cycle re-evaluates it rather than logging [ENTRY] forever.
                continue

            # Claim immediately to stop repeated [ENTRY] logs in future cycles.
            if db.claim_setup_for_fire(setup["id"]):
                fired_signals.append((setup, sig))
            else:
                logger.info(
                    "[SETUP-MONITOR] Setup #%d %s already claimed by another cycle",
                    setup["id"], setup["symbol"],
                )

    if not can_send:
        if remaining_daily_slots <= 0:
            logger.info(
                "[SETUP-MONITOR] Daily signal cap %d/%d — no sends this cycle",
                today_count, MAX_DAILY_SIGNALS,
            )
        else:
            gap_min = (now - latest).total_seconds() / 60
            logger.info(
                "[SETUP-MONITOR] Signal gap %.1fm / %dm — no sends this cycle",
                gap_min, MIN_DAILY_SIGNAL_GAP_MINUTES,
            )
        return

    if not fired_signals:
        logger.info("[SETUP-MONITOR] Done — 0 entries fired")
        return

    fired_signals.sort(key=lambda item: item[1].score, reverse=True)

    send_limit = min(SIGNALS_PER_SCAN, slots, remaining_daily_slots)
    to_send = fired_signals[:send_limit]
    overflow = fired_signals[send_limit:]

    # Revert overflow claims so those setups can be re-evaluated next cycle.
    for setup, _ in overflow:
        db.mark_setup_fire_failed(setup["id"])
        logger.info(
            "[SETUP-MONITOR] Reverted overflow claim setup #%d %s",
            setup["id"], setup["symbol"],
        )

    logger.info(
        "[SETUP-MONITOR] %d fired, sending %d (limit=%d slots=%d daily=%d)",
        len(fired_signals), len(to_send), send_limit, slots, remaining_daily_slots,
    )

    # Correlation brake — block same-direction signals within MARKET_WINDOW_MINUTES.
    window_start = now - timedelta(minutes=MARKET_WINDOW_MINUTES)
    sent_directions: dict[str, int] = {}

    for setup, sig in to_send:
        direction = sig.direction

        db_dir_count = db.count_signals_since_by_direction(window_start, direction)
        total_dir = db_dir_count + sent_directions.get(direction, 0)

        if total_dir >= MAX_SAME_DIRECTION_SIGNALS_PER_WINDOW:
            logger.info(
                "[SETUP-MONITOR] Correlation brake %s %s — %d %s signal(s) in %dm window",
                sig.symbol, direction, total_dir, direction, MARKET_WINDOW_MINUTES,
            )
            if CORRELATION_BRAKE_KEEP_WAITING:
                db.mark_setup_fire_failed(setup["id"])
                logger.info(
                    "[SETUP-MONITOR] Setup #%d %s reverted to waiting (CORRELATION_BRAKE_KEEP_WAITING)",
                    setup["id"], sig.symbol,
                )
            else:
                db.mark_setup_fire_failed(setup["id"])
            continue

        if not _valid_trade_geometry(sig.direction, sig.entry_price, sig.tp_price, sig.sl_price):
            logger.error(
                "[SIGNAL-BLOCK] Invalid geometry %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig.symbol, sig.direction,
                sig.entry_price, sig.tp_price, sig.sl_price,
            )
            db.mark_setup_fire_failed(setup["id"])
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

            db.mark_setup_fired(setup["id"], signal_id)

            await tg.broadcast_signal(app, sig, signal_id)

            sent_directions[direction] = sent_directions.get(direction, 0) + 1

            logger.info(
                "[SETUP-MONITOR] Sent signal #%d from setup #%d %s %s score=%.1f",
                signal_id, setup["id"], sig.symbol, sig.direction, sig.score,
            )
        except Exception as e:
            db.mark_setup_fire_failed(setup["id"])
            logger.error("Failed to fire signal for %s: %s", sig.symbol, e, exc_info=True)


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


async def check_outcomes(app: Application) -> None:
    pending = db.get_pending_signals()
    now = datetime.now(timezone.utc)

    for sig in pending:
        symbol = sig["symbol"]
        direction = sig["direction"]
        tp_price = sig["tp_price"]
        sl_price = sig["sl_price"]
        entry_price = sig["entry_price"]

        if not _valid_trade_geometry(direction, entry_price, tp_price, sl_price):
            logger.error(
                "[OUTCOME-BLOCK] Invalid signal geometry #%s %s %s entry=%.8g tp=%.8g sl=%.8g",
                sig["id"],
                symbol,
                direction,
                entry_price,
                tp_price,
                sl_price,
            )
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            continue

        generated = datetime.fromisoformat(sig["generated_at"])

        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)

        if (now - generated).total_seconds() > SIGNAL_EXPIRE_HOURS * 3600:
            db.update_signal_outcome(sig["id"], "expired", 0.0)
            logger.info(f"Signal {sig['id']} expired ({symbol})")

            try:
                await tg.notify_outcome(
                    app,
                    {
                        **sig,
                        "status": "expired",
                        "pnl_roi": 0.0,
                    },
                )
            except Exception as e:
                logger.error(f"Failed to notify expiry for {symbol}: {e}", exc_info=True)

            continue

        elapsed_min = max(
            (now - generated).total_seconds() / 60,
            CANDLE_MINUTES,
        )

        fetch_count = int(elapsed_min / CANDLE_MINUTES) + 3

        try:
            df = _cached_or_rest_klines(symbol, ENTRY_TF, count=fetch_count)

            if df.empty or len(df) < 2:
                continue

        except Exception as e:
            logger.warning(f"Could not fetch candles for {symbol}: {e}")
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

        db.update_signal_outcome(sig["id"], outcome, pnl)

        logger.info(f"Signal {sig['id']} {outcome.upper()} ({symbol}) {pnl:+.1f}%")

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
            logger.error(f"Failed to notify {outcome} for {symbol}: {e}", exc_info=True)


# ── main ──────────────────────────────────────────────────────────

async def main():
    logger.info(
        f"Starting MEXC Signal Bot — "
        f"Stateful SMC Sweep + OB Retest ({ENTRY_TF})"
    )

    db.init_db()

    logger.info("Loading coin pool...")
    coins = coin_scanner.refresh_coin_list()
    logger.info(f"Signal pool: {len(coins)} coins")

    await _start_ws_cache(coins)

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
        scan_for_setups,
        CronTrigger(minute=SETUP_SCAN_CRON_MINUTES),
        args=[app],
        id="setup_scanner",
    )

    scheduler.add_job(
        monitor_setups,
        IntervalTrigger(minutes=SETUP_MONITOR_MINUTES),
        args=[app],
        id="setup_monitor",
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
        f"Scheduler started — setup scan='{SETUP_SCAN_CRON_MINUTES}', "
        f"monitor={SETUP_MONITOR_MINUTES}m, entry_tf={ENTRY_TF}"
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
            await _stop_ws_cache()
            scheduler.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
