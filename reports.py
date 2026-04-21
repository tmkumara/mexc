"""
Report formatter for daily / weekly / monthly / all-time stats.
Only placed signals (placed=1) are counted in all statistics.
"""

from datetime import datetime, timezone, timedelta
from database import get_signals_in_range, get_all_signals


def _stats(signals: list[dict]) -> dict:
    # Only count signals the user actually placed
    placed  = [s for s in signals if s.get("placed", 0) == 1]

    total   = len(placed)
    wins    = [s for s in placed if s["status"] == "win"]
    losses  = [s for s in placed if s["status"] == "loss"]
    pending = [s for s in placed if s["status"] == "pending"]
    expired = [s for s in placed if s["status"] == "expired"]

    win_count  = len(wins)
    loss_count = len(losses)
    closed     = win_count + loss_count
    win_rate   = (win_count / closed * 100) if closed else 0

    net_roi = sum(s["pnl_roi"] or 0 for s in placed if s["status"] in ("win", "loss"))

    best  = max((s["pnl_roi"] or 0 for s in wins),   default=0)
    worst = min((s["pnl_roi"] or 0 for s in losses),  default=0)

    longs  = [s for s in placed if s["direction"] == "LONG"]
    shorts = [s for s in placed if s["direction"] == "SHORT"]

    sent = len(signals)  # total signals sent (including unplaced)

    return {
        "total": total, "sent": sent,
        "wins": win_count, "losses": loss_count,
        "pending": len(pending), "expired": len(expired),
        "win_rate": win_rate, "net_roi": net_roi,
        "best": best, "worst": worst,
        "longs": len(longs), "shorts": len(shorts),
    }


def _bar(win_rate: float, width: int = 10) -> str:
    filled = round(win_rate / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _format_report(title: str, signals: list[dict]) -> str:
    s = _stats(signals)

    if s["total"] == 0:
        sent_note = f" ({s['sent']} sent, none placed)" if s["sent"] else ""
        return f"📊 *{title}*\n\nNo placed trades recorded yet.{sent_note}"

    sign  = "+" if s["net_roi"] >= 0 else ""
    emoji = "🟢" if s["net_roi"] >= 0 else "🔴"

    lines = [
        f"📊 *{title}*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📡 Signals sent:   `{s['sent']}`",
        f"✔️ Placed trades:  `{s['total']}`",
        f"✅ Wins:           `{s['wins']}`",
        f"❌ Losses:         `{s['losses']}`",
        f"⏳ Pending:        `{s['pending']}`",
        f"💤 Expired:        `{s['expired']}`",
        "",
        f"🎯 Win rate:  `{s['win_rate']:.1f}%`  {_bar(s['win_rate'])}",
        f"{emoji} Net ROI:   `{sign}{s['net_roi']:.1f}%`",
        "",
        f"📈 Longs:   `{s['longs']}`",
        f"📉 Shorts:  `{s['shorts']}`",
        "",
        f"🔥 Best trade:   `+{s['best']:.1f}%`",
        f"💀 Worst trade:  `{s['worst']:.1f}%`",
        "━━━━━━━━━━━━━━━━━━━━",
        f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
    ]
    return "\n".join(lines)


def daily_report() -> str:
    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    sigs  = get_signals_in_range(start, end)
    return _format_report(f"Daily Report — {now.strftime('%Y-%m-%d')}", sigs)


def weekly_report() -> str:
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=7)
    sigs  = get_signals_in_range(start, now)
    week_label = f"{start.strftime('%b %d')} – {now.strftime('%b %d, %Y')}"
    return _format_report(f"Weekly Report — {week_label}", sigs)


def monthly_report() -> str:
    now   = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sigs  = get_signals_in_range(start, now)
    return _format_report(f"Monthly Report — {now.strftime('%B %Y')}", sigs)


def alltime_report() -> str:
    sigs = get_all_signals()
    return _format_report("All-Time Stats", sigs)
