"""Telegram adapter for the Phase 2.11a second-opinion pass (mounted on the existing bot)."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .queue import BUTTONS as FIRST_BUTTONS, LABELS as FIRST_LABELS
from .second_opinion import (BUTTONS, DEFINITIONS, LABELS, REASONS, StaleDecision, needs_reason,
                             parse_callback)


PREFIX = "choch_review2:"
COMMANDS = ("review_second", "review_second_next", "review_second_previous", "review_second_progress",
            "review_second_case", "review_second_help", "review_second_note")
NAMES = {value: BUTTONS[key] for key, value in LABELS.items()}
REASON_NAMES = {value[0]: value[1] for value in REASONS.values()}
FIRST_NAMES = {value: FIRST_BUTTONS[key] for key, value in FIRST_LABELS.items()}
HELP_SUFFIX = "\n\nSecond-opinion pass: /review_second (answers: " + ", ".join(
    BUTTONS[key] for key in LABELS) + "). See /review_second_help for definitions."

HELP = "\n".join([
    "Second-opinion review (6 cases flagged in Phase 2.11)",
    "Your first-pass decisions are kept unchanged; these answers are stored separately.",
    "",
    "Answers:",
    *(f"• {BUTTONS[key]}: {DEFINITIONS[key]}" for key in LABELS),
    "",
    "If your answer differs from both detectors (or you pick Unsure) you will be asked for a one-tap reason.",
    "You can change an answer at any time; every change is kept in an audit history.",
    "",
    "/review_second — open the next unanswered case",
    "/review_second_next — next unanswered case",
    "/review_second_previous — previous case",
    "/review_second_case <id> — open a specific case",
    "/review_second_progress — your second-pass progress",
    "/review_second_note <id> <text> — attach a note to your answer",
    "/review_second_help — this message",
    "",
    "Charts end at the reviewed close; no later candles are shown. Neither detector is assumed correct.",
])


class SecondOpinionReview:
    def __init__(self, service, assets, *, disabled_marker=None):
        self.service = service
        self.assets = assets
        self.disabled_marker = disabled_marker

    def authorize(self, update):
        user, chat = update.effective_user, update.effective_chat
        if not (user and chat and self.service.authorized(user.id, chat.type)):
            return False
        try:
            return not (self.disabled_marker and self.disabled_marker.exists())
        except OSError:
            return False

    def state(self, reviewer, case_id):
        return self.service.storage.effective(reviewer, case_id) or {}

    def keyboard(self, case_id, version):
        def button(code):
            return InlineKeyboardButton(BUTTONS[code], callback_data=f"{PREFIX}d:{code}:{case_id}:{version}")

        return InlineKeyboardMarkup([
            [button("ch"), button("bo")],
            [button("mi"), button("nv")],
            [button("un"), InlineKeyboardButton("Explain", callback_data=f"{PREFIX}x:{case_id}")],
            [InlineKeyboardButton("◀ Previous", callback_data=f"{PREFIX}p:{case_id}"),
             InlineKeyboardButton("Next ▶", callback_data=f"{PREFIX}n:{case_id}")],
        ])

    def reason_keyboard(self, case_id, version):
        codes = list(REASONS)
        rows = [[InlineKeyboardButton(REASONS[code][1], callback_data=f"{PREFIX}r:{code}:{case_id}:{version}")
                 for code in codes[i:i + 2]] for i in range(0, len(codes), 2)]
        return InlineKeyboardMarkup(rows)

    def caption(self, reviewer, case_id):
        text = self.service.manifest[case_id]["caption"]
        state = self.state(reviewer, case_id)
        if state.get("label"):
            text += f"\nYour second-opinion answer: {NAMES[state['label']]}"
            if state.get("reason"):
                text += f" · reason: {REASON_NAMES[state['reason']]}"
        return text[:1024]

    async def show(self, update, context, case):
        chat = update.effective_chat.id
        if case is None:
            await context.bot.send_message(chat_id=chat, text=(
                "All 6 second-opinion cases have an answer. Use /review_second_case <id> to revisit one, "
                "or /review_second_progress for a summary."))
            return
        reviewer = update.effective_user.id
        version = self.state(reviewer, case.case_id).get("version", 0)
        chart = self.assets / self.service.manifest[case.case_id]["chart"]
        with chart.open("rb") as image:
            await context.bot.send_photo(chat_id=chat, photo=image, caption=self.caption(reviewer, case.case_id),
                                         reply_markup=self.keyboard(case.case_id, version))

    def progress_text(self, reviewer):
        rows = self.service.progress(reviewer)
        done = sum(bool(label) for _, label, _, _ in rows)
        pending = sum(flag for *_, flag in rows)
        lines = [f"Second opinion: {done}/{len(rows)} answered · {pending} reason(s) pending.",
                 "First-pass decisions are unchanged."]
        for key, label, reason, flag in rows:
            status = NAMES[label] if label else "not answered"
            if reason:
                status += f" ({REASON_NAMES[reason]})"
            elif flag:
                status += " (reason pending)"
            lines.append(f"• {key}: {status}")
        return "\n".join(lines)

    async def command(self, update, context):
        if not self.authorize(update):
            return
        reviewer, chat = update.effective_user.id, update.effective_chat.id
        pieces = (update.effective_message.text or "").split(maxsplit=2)
        command = pieces[0].split("@")[0].casefold().lstrip("/")
        try:
            if command in ("review_second", "review_second_next"):
                action = "first" if command == "review_second" else "next"
                await self.show(update, context, self.service.navigate(reviewer, action))
                return
            if command == "review_second_previous":
                await self.show(update, context, self.service.navigate(reviewer, "previous"))
                return
            if command == "review_second_case":
                if len(pieces) != 2:
                    raise ValueError("Use /review_second_case <id>.")
                await self.show(update, context, self.service.open_case(reviewer, pieces[1].upper()))
                return
            if command == "review_second_note":
                if len(pieces) != 3:
                    raise ValueError("Use /review_second_note <id> <text>.")
                saved = self.service.note(reviewer, pieces[1].upper(), pieces[2], update.update_id)
                message = "Note saved." if saved else "Note already saved."
            elif command == "review_second_progress":
                message = self.progress_text(reviewer)
            else:
                message = HELP
        except (ValueError, PermissionError) as exc:
            message = str(exc)
        await context.bot.send_message(chat_id=chat, text=message)

    async def callback(self, update, context):
        query = update.callback_query
        if not self.authorize(update):
            await query.answer("Access denied.", show_alert=True)
            return
        reviewer, chat = update.effective_user.id, update.effective_chat.id
        try:
            kind, code, case_id, version = parse_callback(query.data, PREFIX)
            case = self.service.case(reviewer, case_id)
            if kind == "d":
                saved = self.service.decide(reviewer, case_id, code, version, query.id)
                await query.answer("Saved." if saved else "Already saved.")
                if saved is None:
                    return
                await query.edit_message_caption(caption=self.caption(reviewer, case_id),
                                                 reply_markup=self.keyboard(case_id, saved))
                label = LABELS[code]
                first = self.service.first_label(reviewer, case_id)
                note = f"Saved: {BUTTONS[code]}." + (
                    f" (Your first-pass answer was {FIRST_NAMES.get(first, first)}; it is kept unchanged.)"
                    if first else "")
                if needs_reason(label, self.service.answers(case_id)):
                    question = ("What is missing?" if label == "UNSURE"
                                else "Your answer differs from both detectors. Why? (one tap)")
                    await context.bot.send_message(chat_id=chat, text=f"{note}\n{question}",
                                                   reply_markup=self.reason_keyboard(case_id, saved))
                else:
                    await context.bot.send_message(chat_id=chat, text=note + " Use /review_second_next to continue.")
                return
            if kind == "r":
                saved = self.service.reason(reviewer, case_id, code, version, query.id)
                await query.answer("Reason saved." if saved else "Already saved.")
                if saved and code == "ot":
                    await context.bot.send_message(chat_id=chat, text=(
                        f"Send /review_second_note {case_id} <your note> to describe it."))
                elif saved:
                    await context.bot.send_message(chat_id=chat, text=(
                        f"Reason saved: {REASONS[code][1]}. Use /review_second_next to continue."))
                return
            await query.answer()
            if kind == "x":
                await context.bot.send_message(chat_id=chat,
                                               text=self.service.manifest[case_id]["explanation_text"][:4000])
                return
            destination = self.service.navigate(reviewer, "previous" if kind == "p" else "next", case.case_id)
            await self.show(update, context, destination)
        except StaleDecision as exc:
            await query.answer(str(exc), show_alert=True)
        except (ValueError, PermissionError) as exc:
            await query.answer(str(exc), show_alert=True)
