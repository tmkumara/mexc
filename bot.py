"""
Telegram bot: commands and signal broadcast for Precision Pullback Scalper v1.

Signal messages use HTML parse mode.
"""

import logging
from datetime import datetime, date, timezone as tz
from html import escape

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import config as cfg
import database as db
import reports
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID, LKT, STRATEGY_NAME

logger = logging.getLogger(__name__)

paused: bool = False


# ── send helpers ──────────────────────────────────────────────────

async def _send_html(app: Application, text: str, chat_id: str = None, reply_to_message_id: int = None):
    target = chat_id or TELEGRAM_CHANNEL_ID
    return await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_to_message_id=reply_to_message_id,
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

    lines = [
        f"{escape(arrow)} — {_bold(coin)} Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    {_code(f'{signal.entry_price:,.6g}')}",
        f"🎯 TP:       {_code(f'{signal.tp_price:,.6g}')}  {_italic(f'+{signal.tp_roi_pct:.1f}% gross ROI')}",
    ]
    lines.append(f"🛑 SL:       {_code(f'{signal.sl_price:,.6g}')}  {_italic(f'-{signal.sl_roi_pct:.1f}% gross ROI')}")
    lines.append(f"📊 RR:       {_code(f'1:{signal.rr:.3g}')}")
    lines.append(f"⚡ Leverage: {_code(f'{signal.leverage}x')}  {_italic('Isolated')}")
    lines.append(f"🧭 Setup:    {_italic(escape(signal.timeframe_summary))}")
    lines.append(f"📈 Strategy: {STRATEGY_NAME}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ {_code(signal.generated_at.astimezone(LKT).strftime('%Y-%m-%d %H:%M LKT'))}")
    lines.append(f"🆔 Signal ID: {_code(signal_id)}")
    lines.append(_italic("⚠️ Not financial advice. Use risk management."))
    return "\n".join(lines)


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
    elif status == "breakeven":
        emoji, label = "⚖️", f"BREAKEVEN STOP {roi:+.1f}%"
    elif status == "loss":
        emoji, label = "❌", f"STOPPED OUT {roi:+.1f}%"
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

    from config import (
        STRATEGY_NAME,
        TREND_TF, ENTRY_TF,
        MIN_SIGNAL_SCORE,
        TP_ROI_PCT, MAX_SL_ROI_PCT, BREAKEVEN_TRIGGER_ROI_PCT,
        NO_CHASE_MAX_DISTANCE_PCT,
        ATR_MIN_PCT, ATR_MAX_PCT,
        PENDING_SIGNAL_EXPIRY_CANDLES,
        SCAN_INTERVAL_MINUTES,
        OUTCOME_CHECK_MINUTES,
        MAX_CONCURRENT_SIGNALS, MAX_ACTIVE_LONG_SIGNALS, MAX_ACTIVE_SHORT_SIGNALS,
        SIGNAL_COOLDOWN_MINUTES,
        MAX_DAILY_SIGNALS, MIN_DAILY_SIGNAL_GAP_MINUTES,
        LEVERAGE, COINGLASS_API_KEY,
        TOP_N_COINS, COIN_POOL_MIN_VOLUME_USD, COIN_POOL_MIN_SELECTED,
        SIGNAL_EXPIRE_HOURS,
    )

    state  = "⏸ PAUSED" if paused else "▶️ RUNNING"
    coins  = coin_scanner.get_cached_coins()
    active_long  = db.count_active_signals_by_direction("LONG")
    active_short = db.count_active_signals_by_direction("SHORT")

    today_start   = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=tz.utc)
    signals_today = db.count_signals_since(today_start)
    last_sig      = db.latest_signal_time()
    last_sig_str  = last_sig.astimezone(LKT).strftime("%H:%M LKT") if last_sig else "none"

    pairs_str = "  ".join(s.replace("_USDT", "") for s in coins[:20])
    cg_status = "SET" if COINGLASS_API_KEY else "not set"

    msg = "\n".join([
        "📡 <b>Scanner Status</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"State:       {_code(state)}",
        f"Strategy:    {_code(STRATEGY_NAME)}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"TF:          {_code(f'{TREND_TF} trend / {ENTRY_TF} entry')}",
        f"Min score:   {_code(f'{MIN_SIGNAL_SCORE:.0f}/100')}",
        f"No-chase:    {_code(f'{NO_CHASE_MAX_DISTANCE_PCT*100:.2f}%')}  {_italic(f'ATR band {ATR_MIN_PCT*100:.2f}-{ATR_MAX_PCT*100:.2f}%')}",
        f"TP / SL:     {_code(f'+{TP_ROI_PCT:.1f}% / -{MAX_SL_ROI_PCT:.1f}% ROI')}  {_italic(f'BE at +{BREAKEVEN_TRIGGER_ROI_PCT:.1f}%')}",
        f"Pending exp: {_code(f'{PENDING_SIGNAL_EXPIRY_CANDLES} candles')}",
        f"Leverage:    {_code(f'{LEVERAGE}x  Isolated')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Scan every:  {_code(f'{SCAN_INTERVAL_MINUTES}min')}",
        f"Outcome chk: {_code(f'every {OUTCOME_CHECK_MINUTES} min')}",
        f"Cooldown:    {_code(f'{SIGNAL_COOLDOWN_MINUTES} min per coin')}",
        f"Expire:      {_code(f'{SIGNAL_EXPIRE_HOURS}h')}",
        f"Daily cap:   {_code(f'{signals_today}/{MAX_DAILY_SIGNALS}  (min gap {MIN_DAILY_SIGNAL_GAP_MINUTES} min)')}",
        f"Active:      {_code(f'{active_long}/{MAX_ACTIVE_LONG_SIGNALS} LONG, {active_short}/{MAX_ACTIVE_SHORT_SIGNALS} SHORT')}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"Pool size:   {_code(f'{len(coins)} / {TOP_N_COINS} (min {COIN_POOL_MIN_SELECTED})')}",
        f"Min volume:  {_code(f'${COIN_POOL_MIN_VOLUME_USD:,.0f}')}",
        f"CoinGlass:   {_code(cg_status)}",
        "━━━━━━━━━━━━━━━━━━━━",
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
