"""
Telegram bot: commands and signal broadcast for MTF Trend Pullback strategy.

Signal messages use HTML parse mode.
"""

import logging
from datetime import datetime, date, timezone as tz
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
    coin  = signal.symbol.replace("_", "/")
    stars = "⭐⭐⭐" if signal.score >= 90 else "⭐⭐" if signal.score >= 75 else "⭐"

    zone_str = (
        f"{signal.entry_low:,.6g} – {signal.entry_high:,.6g}"
        if signal.entry_low > 0 and signal.entry_high > 0
        else "—"
    )

    return "\n".join([
        f"{escape(arrow)} — {_bold(coin)} Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📡 Trigger:  WebSocket Live Price",
        f"📍 Entry:    {_code(f'{signal.entry_price:,.6g}')}",
        f"📦 Zone:     {_code(zone_str)}",
        f"🎯 TP:       {_code(f'{signal.tp_price:,.6g}')}  {_italic(f'+{signal.tp_roi_pct:.1f}% ROI')}",
        f"🛑 SL:       {_code(f'{signal.sl_price:,.6g}')}  {_italic(f'-{signal.sl_roi_pct:.1f}% ROI')}",
        f"📊 RR:       {_code(f'{signal.rr:.2g}')}",
        f"🏅 Score:    {_code(f'{signal.score:.0f}/100')}  {stars}",
        f"🧭 Trend:    {_italic(escape(signal.timeframe_summary))}",
        f"⚡ Strategy: MTF Trend Pullback",
        f"⚡ Leverage: {_code(f'{signal.leverage}x')}  {_italic('Isolated')}",
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
    symbol    = signal_db["symbol"].replace("_", "/")
    status    = signal_db["status"]
    roi       = signal_db.get("pnl_roi") or 0.0

    if status == "win":
        emoji, label = "✅", f"TARGET HIT {roi:+.1f}%"
    elif status == "loss":
        emoji, label = "❌", f"STOP HIT {roi:+.1f}%"
    else:
        emoji, label = "💤", "EXPIRED"

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
    from main import WS_MANAGER

    from config import (
        STRATEGY_NAME,
        MACRO_TF, MAIN_TF, SETUP_TF, ENTRY_TF,
        EMA_MACRO, EMA_MAIN, EMA_SETUP, EMA_ENTRY_FAST, EMA_ENTRY_SLOW,
        VOLUME_PERIOD, VOLUME_MULTIPLIER,
        MIN_RR, MAX_RR, MIN_SETUP_SCORE,
        MAX_ENTRY_DISTANCE_PCT,
        ARMED_SETUP_EXPIRE_MINUTES,
        SETUP_SCAN_CRON_MINUTES, TRIGGER_CHECK_SECONDS,
        MAX_CONCURRENT_SIGNALS, SIGNAL_COOLDOWN_MINUTES,
        LEVERAGE,
    )

    state  = "⏸ PAUSED" if paused else "▶️ RUNNING"
    coins  = coin_scanner.get_cached_coins()
    active = db.count_active_signals()
    armed  = db.count_armed_setups()

    today_start  = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=tz.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig     = db.latest_signal_time()
    last_sig_str = last_sig.astimezone(LKT).strftime("%H:%M LKT") if last_sig else "none"

    ws_status = WS_MANAGER.status_str() if WS_MANAGER is not None else "not started"
    pairs_str = "  ".join(s.replace("_USDT", "") for s in coins[:20])

    msg = "\n".join([
        "📡 <b>Scanner Status</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"State:       {_code(state)}",
        f"Strategy:    {_code(STRATEGY_NAME)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Macro TF:    {_code(f'{MACRO_TF.upper()} close > EMA{EMA_MACRO}')}",
        f"Main TF:     {_code(f'{MAIN_TF.upper()} close > EMA{EMA_MAIN}')}",
        f"Setup TF:    {_code(f'{SETUP_TF.upper()} close > EMA{EMA_SETUP}')}",
        f"Entry TF:    {_code(f'{ENTRY_TF} pullback near EMA{EMA_ENTRY_FAST}/EMA{EMA_ENTRY_SLOW}/SR')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Min score:   {_code(MIN_SETUP_SCORE)}",
        f"Min RR:      {_code(MIN_RR)}",
        f"Max RR:      {_code(MAX_RR)}",
        f"Volume:      {_code(f'>= {VOLUME_MULTIPLIER}x {VOLUME_PERIOD}-bar avg')}",
        f"Max dist:    {_code(f'{MAX_ENTRY_DISTANCE_PCT}% from trigger (late signal guard)')}",
        f"Setup TTL:   {_code(f'{ARMED_SETUP_EXPIRE_MINUTES} min')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan cron:   {_code(SETUP_SCAN_CRON_MINUTES)}",
        f"Trigger:     {_code(f'every {TRIGGER_CHECK_SECONDS}s via WebSocket')}",
        f"Leverage:    {_code(f'{LEVERAGE}x')}",
        f"Cooldown:    {_code(f'{SIGNAL_COOLDOWN_MINUTES} min per coin')}",
        f"Active:      {_code(f'{active}/{MAX_CONCURRENT_SIGNALS} signals')}",
        f"Armed:       {_code(f'{armed} setups waiting for trigger')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"WebSocket:   {_code(ws_status)}",
        f"Today:       {_code(f'{signals_today} signals')}",
        f"Last signal: {_code(last_sig_str)}",
        "━━━━━━━━━━━━━━━━━━━━",
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
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    return app
