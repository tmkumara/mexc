"""
Telegram bot: handles commands and broadcasts signals to the channel.
"""

import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

import reports
from config import TELEGRAM_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)


# ─────────────────────────── helpers ────────────────────────────

async def _send(app: Application, text: str, chat_id: str = None):
    """Send markdown message to the channel (or a specific chat)."""
    target = chat_id or TELEGRAM_CHANNEL_ID
    await app.bot.send_message(
        chat_id=target,
        text=text,
        parse_mode=ParseMode.MARKDOWN,
    )


def format_signal_message(signal) -> str:
    arrow = "🟢 LONG" if signal.direction == "LONG" else "🔴 SHORT"
    coin  = signal.symbol.replace("_", "/")

    lines = [
        f"{arrow} — *{coin}* Futures",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 Entry:     `${signal.entry_price:,.6g}`",
        f"🎯 TP:        `${signal.tp_price:,.6g}`  _(+{signal.tp_roi_pct:.0f}% ROI)_",
        f"🛑 SL:        `${signal.sl_price:,.6g}`  _(-{signal.sl_roi_pct:.0f}% ROI)_",
        f"⚡ Leverage:  `{signal.leverage}x`",
        f"💼 Risk:      `{signal.risk_pct:.0f}% of balance`",
        f"📊 {signal.timeframe_summary}",
        "━━━━━━━━━━━━━━━━━━━━",
        f"⏰ `{signal.generated_at.strftime('%Y-%m-%d %H:%M UTC')}`",
    ]
    return "\n".join(lines)


async def broadcast_signal(app: Application, signal, signal_id: int):
    msg = format_signal_message(signal)
    msg += f"\n🆔 Signal ID: `{signal_id}`"
    await _send(app, msg)


async def notify_outcome(app: Application, signal_db: dict):
    direction = signal_db["direction"]
    symbol    = signal_db["symbol"].replace("_", "/")
    status    = signal_db["status"]
    roi       = signal_db.get("pnl_roi") or 0

    if status == "win":
        emoji = "✅"
        label = f"TARGET HIT  +{roi:.1f}%"
    elif status == "loss":
        emoji = "❌"
        label = f"STOP HIT  {roi:.1f}%"
    else:
        emoji = "💤"
        label = "EXPIRED (no hit in 24h)"

    arrow = "🟢" if direction == "LONG" else "🔴"
    msg = (
        f"{emoji} *Signal Closed*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{arrow} {direction} — *{symbol}*\n"
        f"Result: `{label}`\n"
        f"🆔 ID: `{signal_db['id']}`"
    )
    await _send(app, msg)


# ─────────────────────────── commands ───────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *MEXC Futures Signal Bot*\n\n"
        "*Commands:*\n"
        "/daily — Today's performance\n"
        "/weekly — Last 7 days\n"
        "/monthly — This month\n"
        "/stats — All-time stats\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.daily_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.weekly_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.monthly_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        reports.alltime_report(), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


# ─────────────────── scheduled auto-reports ─────────────────────

async def auto_daily_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.daily_report())


async def auto_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.weekly_report())


async def auto_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    await _send(context.application, reports.monthly_report())


# ─────────────────────── app builder ────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("weekly",  cmd_weekly))
    app.add_handler(CommandHandler("monthly", cmd_monthly))
    app.add_handler(CommandHandler("stats",   cmd_stats))

    return app
