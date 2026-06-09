"""
Telegram bot: handles commands and broadcasts Fresh Trend Meter + Stoch MTM signals.

Important:
    Signal messages use HTML parse mode, not Markdown.
    This avoids Telegram parse errors caused by symbols/strategy text containing underscores,
    e.g. H_USDT.
"""

import logging
from datetime import datetime
from html import escape

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import database as db
import reports
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, LKT

logger = logging.getLogger(__name__)

paused: bool = False


# ── send helpers ──────────────────────────────────────────────────

async def _send_html(app: Application, text: str, chat_id: str = None):
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _send_markdown(app: Application, text: str, chat_id: str = None):
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


def _code(value) -> str:
    return f"<code>{escape(str(value))}</code>"


def _bold(value) -> str:
    return f"<b>{escape(str(value))}</b>"


def _italic(value) -> str:
    return f"<i>{escape(str(value))}</i>"


# ── signal formatting ─────────────────────────────────────────────

def format_signal(signal, signal_id: int) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin = signal.symbol.replace("_", "/")
    stars = "⭐⭐⭐" if signal.score >= 80 else "⭐⭐" if signal.score >= 65 else "⭐"

    return "\n".join([
        f"{escape(arrow)} — {_bold(coin)} Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    {_code(f'{signal.entry_price:,.6g}')}",
        f"🎯 TP:       {_code(f'{signal.tp_price:,.6g}')}  {_italic(f'+{signal.tp_roi_pct:.1f}% ROI')}",
        f"🛑 SL:       {_code(f'{signal.sl_price:,.6g}')}  {_italic(f'-{signal.sl_roi_pct:.1f}% ROI')}",
        f"⚡ Leverage: {_code(f'{signal.leverage}x')}  {_italic('Isolated')}",
        f"📊 {escape(signal.timeframe_summary)}",
        f"🏅 Score:    {_code(f'{signal.score}/100')}  {stars}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ {_code(signal.generated_at.astimezone(LKT).strftime('%Y-%m-%d %H:%M LKT'))}",
        f"🆔 Signal ID: {_code(signal_id)}",
        _italic("⚠️ Not financial advice. Use risk management."),
    ])


async def broadcast_signal(app: Application, signal, signal_id: int) -> None:
    msg = format_signal(signal, signal_id)
    await _send_html(app, msg)


async def notify_outcome(app: Application, signal_db: dict) -> None:
    direction = signal_db["direction"]
    symbol = signal_db["symbol"].replace("_", "/")
    status = signal_db["status"]
    roi = signal_db.get("pnl_roi") or 0.0

    if status == "win":
        emoji = "✅"
        label = f"TARGET HIT +{roi:.1f}%"
    elif status == "loss":
        emoji = "❌"
        label = f"STOP HIT {roi:.1f}%"
    else:
        emoji = "💤"
        label = "EXPIRED"

    arrow = "🟢" if direction == "LONG" else "🔴"

    msg = "\n".join([
        f"{emoji} {_bold('Signal Closed')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"{arrow} {escape(direction)} — {_bold(symbol)}",
        f"Result: {_code(label)}",
        f"🆔 ID: {_code(signal_db['id'])}",
    ])

    await _send_html(app, msg)


# ── commands ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>MEXC Futures Signal Bot</b>\n\n"
        "<b>Performance:</b>\n"
        "/daily — Today's report\n"
        "/weekly — Last 7 days\n"
        "/monthly — This month\n"
        "/stats — All-time stats\n\n"
        "<b>Scanner:</b>\n"
        "/status — Scanner state\n"
        "/pause — Pause signals\n"
        "/resume — Resume signals\n\n"
        "/help — This message"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.daily_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.weekly_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.monthly_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(reports.alltime_report(), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import coin_scanner

    from config import (
        STRATEGY_NAME,
        MACRO_TF,
        HTF_TREND_TF,
        STRUCTURE_TF,
        ENTRY_TF,
        REQUIRE_MTF_ALIGNMENT,
        ENABLE_HTF_FILTER,
        ENABLE_ENTRY_EMA_FILTER,
        ENABLE_ATR_FILTER,
        ENABLE_VOLUME_FILTER,
        ENABLE_BTC_FILTER,
        MIN_ATR_PCT,
        MAX_ATR_PCT,
        MIN_SL_PCT,
        MAX_SL_PCT,
        MIN_STRUCTURE_RR,
        MAX_STRUCTURE_RR,
        MIN_SETUP_SCORE,
        MAX_OB_DISTANCE_ATR,
        MAX_OB_DISTANCE_PCT,
        MAX_DISPLACEMENT_AGE_CANDLES,
        MAX_SWEEP_AGE_CANDLES,
        MAX_OB_AGE_CANDLES,
        REVALIDATE_BEFORE_FIRE,
        OB_ENTRY_QUALITY_CHECK,
        REQUIRE_MSS_BREAK_ENTRY,
        MSS_BREAK_LOOKBACK_CANDLES,
        MSS_BREAK_BUFFER_PCT,
        ENABLE_ATR_STOP_FLOOR,
        ATR_STOP_FLOOR_MULTIPLIER,
        REQUIRE_TREND_CANDLE_CONFIRMATION,
        TREND_CONFIRM_TF,
        LEVERAGE,
        SIGNAL_COOLDOWN_MINUTES,
        SIGNALS_PER_SCAN,
        MAX_CONCURRENT_SIGNALS,
        SCAN_WORKERS,
        SETUP_SCAN_CRON_MINUTES,
        SETUP_MONITOR_MINUTES,
        MAX_NEW_SETUPS_PER_SCAN,
        MAX_SETUPS_SAME_DIRECTION_PER_SCAN,
        MAX_WAITING_SETUPS_TOTAL,
        SETUP_MONITOR_LIMIT,
    )

    state = "⏸ PAUSED" if paused else "▶️ RUNNING"
    coins = coin_scanner.get_cached_coins()
    active = db.count_active_signals()
    waiting = db.count_waiting_setups()

    pairs_str = "  ".join(s.replace("_USDT", "") for s in coins[:20])

    filters = []
    if ENABLE_HTF_FILTER:
        filters.append("4H trend")
    if ENABLE_ENTRY_EMA_FILTER:
        filters.append(f"{ENTRY_TF} EMA")
    if ENABLE_ATR_FILTER:
        filters.append("ATR")
    if ENABLE_VOLUME_FILTER:
        filters.append("Volume")
    if ENABLE_BTC_FILTER:
        filters.append("BTC")

    msg = "\n".join([
        "📡 <b>Scanner Status</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"State:       {_code(state)}",
        f"Strategy:    {_code(STRATEGY_NAME)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Macro TF:    {_code(MACRO_TF.upper())} regime (EMA50/200)",
        f"HTF Trend:   {_code(HTF_TREND_TF.upper())} trend (EMA50/200)",
        f"Structure:   {_code(STRUCTURE_TF.upper())} bias (swing structure)",
        f"Entry TF:    {_code(ENTRY_TF)} sweep / OB retest",
        f"MTF align:   {_code('required' if REQUIRE_MTF_ALIGNMENT else 'optional')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Min score:   {_code(MIN_SETUP_SCORE)}",
        f"MSS break:   {_code('on' if REQUIRE_MSS_BREAK_ENTRY else 'off')}"
            f"  {_code(f'window {MSS_BREAK_LOOKBACK_CANDLES}c / buf {MSS_BREAK_BUFFER_PCT:g}%')}",
        f"Freshness:   {_code(f'disp ≤{MAX_DISPLACEMENT_AGE_CANDLES}c  sweep ≤{MAX_SWEEP_AGE_CANDLES}c  OB ≤{MAX_OB_AGE_CANDLES}c')}",
        f"ATR:         {_code(f'{MIN_ATR_PCT:g}%–{MAX_ATR_PCT:g}%')}",
        f"SL limit:    {_code(f'{MIN_SL_PCT:g}%–{MAX_SL_PCT:g}%')}",
        f"RR:          {_code(f'{MIN_STRUCTURE_RR:g}–{MAX_STRUCTURE_RR:g}')}",
        f"OB distance: {_code(f'≤{MAX_OB_DISTANCE_PCT:g}% or ≤{MAX_OB_DISTANCE_ATR:g}ATR')}",
        f"ATR floor:   {_code(f'on × {ATR_STOP_FLOOR_MULTIPLIER:g}' if ENABLE_ATR_STOP_FLOOR else 'off')}",
        f"Revalidate:  {_code('on' if REVALIDATE_BEFORE_FIRE else 'off')}",
        f"OB quality:  {_code('on' if OB_ENTRY_QUALITY_CHECK else 'off')}",
        f"Confirm TF:  {_code(f'on ({TREND_CONFIRM_TF})' if REQUIRE_TREND_CANDLE_CONFIRMATION else 'off')}",
        f"Filters:     {_code(', '.join(filters) if filters else 'none')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan:        {_code(SETUP_SCAN_CRON_MINUTES)}",
        f"Monitor:     {_code(f'every {SETUP_MONITOR_MINUTES} min, top {SETUP_MONITOR_LIMIT}')}",
        f"Workers:     {_code(SCAN_WORKERS)}",
        f"Leverage:    {_code(f'{LEVERAGE}x')}",
        f"Entries:     {_code(f'top {SIGNALS_PER_SCAN} fires/monitor')}",
        f"Save limit:  {_code(f'{MAX_NEW_SETUPS_PER_SCAN}/scan, {MAX_SETUPS_SAME_DIRECTION_PER_SCAN}/direction')}",
        f"Wait cap:    {_code(f'{waiting}/{MAX_WAITING_SETUPS_TOTAL} setups')}",
        f"Cooldown:    {_code(f'{SIGNAL_COOLDOWN_MINUTES} min per coin')}",
        f"Active:      {_code(f'{active}/{MAX_CONCURRENT_SIGNALS} signals')}",
        f"Pool ({len(coins)}):  {_code(pairs_str)}",
        f"Time (LKT):  {_code(datetime.now(LKT).strftime('%H:%M'))}",
    ])

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = True
    await update.message.reply_text("⏸ Signal sending <b>paused</b>.", parse_mode=ParseMode.HTML)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = False
    await update.message.reply_text("▶️ Signal sending <b>resumed</b>.", parse_mode=ParseMode.HTML)


# ── scheduled report helpers ──────────────────────────────────────

async def auto_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_markdown(context.application, reports.daily_report())


async def auto_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_markdown(context.application, reports.weekly_report())


async def auto_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send_markdown(context.application, reports.monthly_report())


# ── app builder ───────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("weekly", cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    return app
