"""Private long-polling adapter for human review."""

import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from .evaluation import report
from .io import utc
from .queue import BUTTONS, LABELS
from .service import parse_callback


HELP = (
    "Private historical structure review\n"
    "/next opens the next unreviewed case. /previous goes back. /case <id> opens a case.\n"
    "/progress shows your counts. /skip advances without a decision. /undo reverts your last judgment.\n"
    "/reason <id> <text> replaces your notes. Use --clear to remove them.\n"
    "/summary shows current agreement. /help repeats these instructions.\n\n"
    "Review the chart independently. Was the pivot confirmed and usable? Did it belong to "
    "the leg, and was it intact when assigned? Did the candle close beyond it? Was the trend "
    "established? Is this an external reversal, a continuation, or a local break? "
    "Would you make the same judgment without later candles?"
)


class TelegramReview:
    def __init__(self, service, chart_dir, manifest_path, *, integrated=False, disabled_marker=None,
                 help_suffix=""):
        self.service = service
        self.chart_dir = chart_dir
        self.integrated = integrated
        self.disabled_marker = disabled_marker
        self.help_suffix = help_suffix
        self.callback_prefix = "choch_review:" if integrated else "r:"
        self.manifest = {row["case_id"]: row for row in json.loads(manifest_path.read_text(encoding="utf-8"))}
        for case in service.cases.values():
            row = self.manifest.get(case.case_id)
            if (not row or utc(row["last_candle_close"]) != utc(case.timestamp)
                    or any((row["evidence"][engine]["classification"] or "")
                           != case.evidence[f"{engine}_classification"] for engine in ("frozen", "shadow"))):
                raise ValueError("Prepared chart manifest does not match review cases")
            if not (chart_dir / f"{case.case_id}.png").is_file():
                raise ValueError("A prepared review chart is missing")

    def authorize(self, update):
        user, chat = update.effective_user, update.effective_chat
        if not (user and chat and self.service.authorized(user.id, chat.type)):
            return False
        try:
            return not (self.disabled_marker and self.disabled_marker.exists())
        except OSError:
            return False

    def state(self, reviewer, case):
        return self.service.storage.effective(reviewer, case.case_id) or {}

    def keyboard(self, case, state):
        version = state.get("review_version", 0)

        def button(action, name):
            return InlineKeyboardButton(name, callback_data=f"{self.callback_prefix}{action}:{case.case_id}:{version}")

        return InlineKeyboardMarkup([
            [button("vc", BUTTONS["vc"]), button("nc", BUTTONS["nc"])],
            [button("vb", BUTTONS["vb"]), button("ob", BUTTONS["ob"])],
            [button("am", BUTTONS["am"]), button("dt", "View Details")],
            [button("pr", "Previous"), button("nx", "Next")],
        ])

    def caption(self, case, state):
        index = self.service.order.index(case.case_id) + 1
        row = case.evidence
        direction = "BULLISH" if case.direction == "up" else "BEARISH"
        labels = [row[f"{engine}_classification"] or "No external BOS or CHoCH"
                  for engine in ("frozen", "shadow")]
        protection = [row.get(f"{engine}_prior_protected_level") or "none"
                      for engine in ("frozen", "shadow")]
        decision = (state.get("review_label") or "UNREVIEWED").replace("_", " ")
        grouped = row.get("grouped_broken_levels")
        grouped_text = f"Grouped levels: {grouped[:100]}\n" if grouped else ""
        return (
            f"Structure review {index}/{len(self.service.order)} · {case.case_id}\n"
            f"{case.symbol} · 4H · {case.timestamp[:16].replace('T', ' ')} UTC · {direction}\n"
            f"Close {row['close']}\n"
            f"Frozen: {labels[0]} | prior protection {protection[0]}\n"
            f"Shadow: {labels[1]} | prior protection {protection[1]}\n"
            f"Broken level (frozen / shadow): {row.get('frozen_broken_level') or '—'} / "
            f"{row.get('shadow_broken_level') or '—'}\n"
            f"{grouped_text}"
            f"Your review: {decision}\n"
            "What does the structure support at this close?"
        )

    async def show(self, update, context, case):
        if case is None:
            previous = "/review_previous" if self.integrated else "/previous"
            direct = "/review_case" if self.integrated else "/case"
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text=f"No other unreviewed cases. Use {previous} or {direct} to revisit one.")
            return
        reviewer = update.effective_user.id
        state = self.state(reviewer, case)
        with (self.chart_dir / f"{case.case_id}.png").open("rb") as image:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=image,
                                         caption=self.caption(case, state), reply_markup=self.keyboard(case, state))

    def details(self, case):
        output = [f"{case.case_id} — evidence through the reviewed close"]
        for label, item in self.manifest[case.case_id]["evidence"].items():
            prior, current = item["prior"], item["current"]
            classification = item["classification"] or ""
            if "CHOCH" in classification:
                why = "The close crossed assigned opposing protection."
            elif classification == "EXTERNAL_BOS":
                why = "The close crossed the continuation boundary in the existing trend direction."
            elif classification == "EXTERNAL_TREND_ESTABLISHED":
                why = "The close satisfied the directional establishment setup."
            elif prior["protected_level"] is None:
                why = "No assigned protection and no qualifying establishment or continuation on this close."
            else:
                why = "The close retained assigned protection and did not qualify as continuation."
            output.extend((
                f"\n{label.title()}: {item['classification'] or 'no external event'}",
                f"Prior state: {prior['state']}; BOS count: {prior['continuation_count']}",
                f"Prior protection: {prior['protected_level']}; assigned: {prior['protected_since']}",
                f"Protected pivot: {item['protected_pivot']}; confirmed: {item['protected_confirmation']}",
                f"First eligible close: {item['protected_eligible']}",
                f"Latest establishment: {item['latest_establishment']}; target: {item['latest_establishment_target']}",
                f"Current broken pivot: {current['broken_level']}; Strong checks: "
                f"{current['strong_rejection_reasons'] or 'none recorded'}",
                why,
            ))
            for reason in item["protection_decisions"]:
                output.append(f"Setup swing {reason['candidate_price']}: {reason['reason']}")
        output.append(f"\nGrouped broken levels: {case.evidence.get('grouped_broken_levels') or 'none recorded'}")
        output.append("\nChecklist: confirm timing and leg origin, check the strict close, and judge structural scale before using any later evidence.")
        return "\n".join(output)[:3900]

    async def command(self, update, context):
        if not self.authorize(update):
            return
        reviewer, chat = update.effective_user.id, update.effective_chat.id
        text = update.effective_message.text
        pieces = text.split(maxsplit=2)
        command = pieces[0].split("@")[0].casefold()
        help_text = HELP
        if self.integrated:
            help_text = re.sub(r"/(next|previous|progress|case|skip|undo|summary|help|reason)\b",
                               r"/review_\1", HELP) + self.help_suffix
            if command == "/review":
                command = "/next"
            elif command.startswith("/review_"):
                command = "/" + command.removeprefix("/review_")
            else:
                return
        try:
            if command in ("/start", "/help"):
                message = help_text
                if command == "/start":
                    current = self.service.storage.cursor(reviewer)
                    message = "Authorized reviewer.\n" + (f"Last case: {current}\n\n" if current else "\n") + HELP
            elif command in ("/next", "/previous", "/skip"):
                await self.show(update, context, self.service.navigate(reviewer, command[1:]))
                return
            elif command == "/case":
                if len(pieces) != 2:
                    raise ValueError("Use /case <id>.")
                await self.show(update, context, self.service.open_case(reviewer, pieces[1].upper()))
                return
            elif command == "/reason":
                if len(pieces) != 3:
                    raise ValueError("Use /reason <id> <notes> or /reason <id> --clear.")
                notes = "" if pieces[2] == "--clear" else pieces[2]
                changed = self.service.note(reviewer, pieces[1].upper(), notes, update.update_id)
                message = "Notes saved." if changed else "Notes already match."
            elif command == "/undo":
                case_id = self.service.undo(reviewer, update.update_id)
                message = f"Restored the prior judgment for {case_id}." if case_id else "No decision to undo."
            elif command == "/progress":
                labels = [self.state(reviewer, case).get("review_label") for case in self.service.cases.values()]
                message = (f"Your reviewed cases: {sum(bool(label) for label in labels)}/{len(labels)}. "
                           f"Ambiguous: {labels.count('AMBIGUOUS')}.")
            else:
                summary = report(tuple(self.service.cases.values()), self.service.storage.history())
                message = (f"Reviewed: {summary['reviewed_cases']}/{summary['total_cases']}. "
                           f"Definitive: {summary['definitive_cases']}. Ambiguous: {summary['ambiguous_cases']}. "
                           f"Reviewer disagreements: {len(summary['reviewer_disagreements'])}.")
                for name in ("frozen", "shadow"):
                    row = summary[name]
                    precision = "pending" if row["precision"] is None else f"{row['precision']:.1%}"
                    recall = "pending" if row["recall"] is None else f"{row['recall']:.1%}"
                    message += (f"\n{name.title()}: correct {row['correctly_detected']}, missed {row['missed']}, "
                                f"false {row['incorrect']}; precision {precision}, recall {recall}.")
                message += "\nStrong accuracy requires a separate human judgment."
        except ValueError as exc:
            message = str(exc)
            if self.integrated:
                message = re.sub(r"/(next|case|reason)\b", r"/review_\1", message)
        await context.bot.send_message(chat_id=chat, text=message)

    async def callback(self, update, context):
        query = update.callback_query
        if not self.authorize(update):
            if update.effective_chat and update.effective_chat.type == "private":
                await query.answer("Access denied.", show_alert=True)
            return
        reviewer = update.effective_user.id
        try:
            payload = query.data
            if (not isinstance(payload, str) or len(payload.encode("utf-8")) > 64
                    or not payload.startswith(self.callback_prefix)):
                raise ValueError("Invalid review button")
            action, case_id, version = parse_callback("r:" + payload[len(self.callback_prefix):])
            case = self.service.case(reviewer, case_id)
            if action in LABELS:
                changed = self.service.decide(reviewer, case_id, action, version, query.id)
                await query.answer("Review saved." if changed else "Already recorded.")
                if changed:
                    state = self.state(reviewer, case)
                    await query.edit_message_caption(caption=self.caption(case, state),
                                                     reply_markup=self.keyboard(case, state))
                return
        except ValueError as exc:
            message = str(exc)
            if self.integrated:
                message = re.sub(r"/(next|case)\b", r"/review_\1", message)
            await query.answer(message, show_alert=True)
            return
        await query.answer()
        if action == "dt":
            details = self.details(case)
            note = self.state(reviewer, case).get("notes")
            if note:
                details = f"{details}\n\nYour notes: {note}"[:4000]
            await context.bot.send_message(chat_id=update.effective_chat.id, text=details)
        else:
            destination = self.service.navigate(reviewer, "previous" if action == "pr" else "next", case_id)
            await self.show(update, context, destination)


async def safe_error(update, context):
    logging.getLogger(__name__).warning("Telegram interaction failed (%s); retry the command if needed.",
                                       type(context.error).__name__)


def start(token, adapter):
    # HTTP request logs can contain Bot API URLs with credentials.
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    try:
        application = Application.builder().token(token).concurrent_updates(False).build()
        application.add_handler(CommandHandler(
            ("start", "next", "previous", "progress", "case", "skip", "undo", "summary", "help", "reason"),
            adapter.command,
        ))
        application.add_handler(CallbackQueryHandler(adapter.callback))
        application.add_error_handler(safe_error)
        application.run_polling(allowed_updates=("message", "callback_query"), bootstrap_retries=0)
    except (TelegramError, OSError):
        raise SystemExit("Review bot could not connect to Telegram. Check local configuration and connectivity.") from None
