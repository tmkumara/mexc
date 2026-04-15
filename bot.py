"""
Telegram bot: handles commands and broadcasts signals to the channel.
"""

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import reports
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)

# ── module-level pause flag ───────────────────────────────────────
paused: bool = False


async def _send(app: Application, text: str, chat_id: str = None):
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id    = target,
        text       = text,
        parse_mode = ParseMode.MARKDOWN,
    )


# ── signal formatting ─────────────────────────────────────────────

def format_signal(signal) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin  = signal.symbol.replace("_", "/")

    return "\n".join([
        f"{arrow} — *{coin}* Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:    `${signal.entry_price:,.6g}`",
        f"🎯 TP:       `${signal.tp_price:,.6g}`  _(+{signal.tp_roi_pct:.1f}% ROI)_",
        f"🛑 SL:       `${signal.sl_price:,.6g}`  _(-{signal.sl_roi_pct:.1f}% ROI)_",
        f"⚡ Leverage: `{signal.leverage}x`  _(Isolated)_",
        f"📊 {signal.timeframe_summary}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ `{signal.generated_at.strftime('%Y-%m-%d %H:%M UTC')}`",
        "_⚠️ Not financial advice. Use risk management._",
    ])


async def broadcast_signal(app: Application, signal, signal_id: int) -> None:
    msg = format_signal(signal)
    msg += f"\n🆔 Signal ID: `{signal_id}`"
    await _send(app, msg)


async def notify_outcome(app: Application, signal_db: dict) -> None:
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    status    = signal_db["status"]
    roi       = signal_db.get("pnl_roi") or 0.0

    if status == "win":
        emoji = "✅"
        label = f"TARGET HIT  +{roi:.1f}%"
    elif status == "loss":
        emoji = "❌"
        label = f"STOP HIT  {roi:.1f}%"
    else:
        emoji = "💤"
        label = "EXPIRED"

    arrow = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"{emoji} *Signal Closed*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow} {direction} — *{symbol}*\n"
        f"Result: `{label}`\n"
        f"🆔 ID: `{signal_db['id']}`"
    )
    await _send(app, msg)


# ── commands ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *MEXC Futures Signal Bot*\n\n"
        "*Performance:*\n"
        "/daily — Today's report\n"
        "/weekly — Last 7 days\n"
        "/monthly — This month\n"
        "/stats — All-time stats\n\n"
        "*Scanner:*\n"
        "/status — Scanner state\n"
        "/pause — Pause signals\n"
        "/resume — Resume signals\n\n"
        "/help — This message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
    from config import TRADING_PAIRS, TIMEFRAME, ST_LENGTH, ST_MULTIPLIER, EMA_TREND_PERIOD
    state = "⏸ PAUSED" if paused else "▶️ RUNNING"
    pairs = ", ".join(p.replace("_USDT", "") for p in TRADING_PAIRS)
    msg = (
        "📡 *Scanner Status*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"State:      `{state}`\n"
        f"Strategy:   `Supertrend({ST_LENGTH},{ST_MULTIPLIER}) + EMA{EMA_TREND_PERIOD}`\n"
        f"Timeframe:  `{TIMEFRAME}`\n"
        f"Pairs:      `{pairs}`\n"
        f"Time (UTC): `{datetime.now(timezone.utc).strftime('%H:%M')}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = True
    await update.message.reply_text(
        "⏸ Signal sending *paused*.", parse_mode=ParseMode.MARKDOWN
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global paused
    paused = False
    await update.message.reply_text(
        "▶️ Signal sending *resumed*.", parse_mode=ParseMode.MARKDOWN
    )


# ── scheduled report helpers ─────────────────────────────────────

async def auto_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.daily_report())


async def auto_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.weekly_report())


async def auto_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.monthly_report())


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
