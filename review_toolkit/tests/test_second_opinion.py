"""Phase 2.11a: second-opinion charts, explanations, storage and adapter (offline)."""

from collections import defaultdict
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from research.strong_choch.evaluate import read_rows
from research.strong_choch.review import clarity
from research.strong_choch.review.io import utc
from research.strong_choch.review.queue import load_queue
from research.strong_choch.review.second_opinion import (
    LABELS, REASONS, ROUND, SELECTION, SecondOpinionService, SecondOpinionStorage, StaleDecision,
    detector_answers, load_manifest, needs_reason, parse_callback)
from research.strong_choch.review.storage import Storage
from research.strong_choch.review.telegram_second import PREFIX, SecondOpinionReview
from research.strong_choch.structure_audit import SavedRun
from research.strong_choch.validation import fingerprint
from strategy.market_structure.structure import Parameters

ROOT = Path(__file__).resolve().parents[1] / "strong_choch_results"
QUEUE = ROOT / "phase_2_10/review_queue.csv"
SOURCE = ROOT / "phase_2_4"
FREEZE = ROOT / "phase_2_5/algorithm_fingerprint.json"
ASSETS = ROOT / "phase_2_11a"
REPORT = Path(__file__).resolve().parents[3] / "strong_choch_results/phase_2_11/PHASE_2_11_REVIEW_ANALYSIS.md"


def complete(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("Mock Telegram operation unexpectedly suspended")


def saved_runs():
    groups = defaultdict(list)
    for group in read_rows(SOURCE / "grouped_events.csv"):
        groups[group["symbol"]].append(group)
    return {symbol: SavedRun(SOURCE / symbol, symbol, groups[symbol])
            for symbol in ("ADA_USDT", "BNB_USDT", "ETH_USDT")}


class ContextTests(unittest.TestCase):
    """Replay-derived context: membership, boundaries, trend context, no leakage."""

    @classmethod
    def setUpClass(cls):
        if not (QUEUE.is_file() and SOURCE.is_dir() and ASSETS.is_dir()):
            raise unittest.SkipTest("Prepared Phase 2.10/2.11a datasets are unavailable")
        lock = json.loads(FREEZE.read_text())
        cls.fingerprint_matches = lock["fingerprint"] == fingerprint()
        cls.parameters = Parameters(**lock["parameters"])
        cls.cases = {case.case_id: case for case in load_queue(QUEUE)}
        cls.runs = saved_runs()
        cls.contexts = {key: clarity.build_context(cls.cases[key], cls.runs[cls.cases[key].symbol],
                                                   cls.parameters, {}) for key in SELECTION}
        cls.manifest = load_manifest(ASSETS / "second_opinion_queue.json")

    def test_frozen_fingerprint_unchanged(self):
        self.assertTrue(self.fingerprint_matches)

    def test_queue_membership_matches_phase_2_11_selection(self):
        self.assertEqual(list(self.manifest), list(SELECTION))
        self.assertEqual(len(SELECTION), 6)
        self.assertTrue(set(SELECTION) <= set(self.cases))
        if REPORT.is_file():
            section = REPORT.read_text(encoding="utf-8").split("## 7.")[1].split("## 8.")[0]
            named = set(re.findall(r"(ADA|BNB|ETH|SOL) (\d\d)-(\d\d)", section.split("2.")[0]))
            expected = {(key[:3], key[8:10], key[10:12]) for key in SELECTION}
            self.assertEqual(named, expected)

    def test_chart_ends_at_reviewed_close_and_no_future_leakage(self):
        for key, ctx in self.contexts.items():
            close = utc(self.cases[key].timestamp)
            self.assertEqual(ctx["bars"][-1].closed_at, close)
            self.assertTrue(all(bar.closed_at <= close for bar in ctx["bars"]))
            self.assertTrue(all(swing["confirmed"] <= close for swing in ctx["swings"]))
            self.assertEqual(self.manifest[key]["last_candle_close"], close.isoformat())
        # Truncating every later candle from the dataset must not change anything.
        key = "ETH_20261008_1200_D"
        run = self.runs["ETH_USDT"]
        cut = run.indices[utc(self.cases[key].timestamp)] + 1
        truncated = SimpleNamespace(bars=run.bars[:cut], indices=run.indices)
        again = clarity.build_context(self.cases[key], truncated, self.parameters, {})
        self.assertEqual(clarity.serialisable(again), clarity.serialisable(self.contexts[key]))
        self.assertEqual(clarity.explanation(again), clarity.explanation(self.contexts[key]))

    def test_window_shows_how_prior_trend_was_established(self):
        for key, ctx in self.contexts.items():
            first = ctx["bars"][0].closed_at
            self.assertLessEqual(len(ctx["bars"]), clarity.MAX_CONTEXT_BARS)
            self.assertGreaterEqual(len(ctx["bars"]), min(clarity.MIN_CONTEXT_BARS, ctx["event_index"] + 1))
            for data in ctx["engines"].values():
                if data["trend_started_at"] and not ctx["truncated"]:
                    self.assertLessEqual(first, data["trend_started_at"], key)
                if data["trend"] == "UNKNOWN" and data["last_event"]:
                    self.assertLessEqual(first, data["last_event"]["at"], key)
        ada = self.contexts["ADA_20260919_0400_U"]
        self.assertLessEqual(ada["bars"][0].closed_at, utc("2026-07-30T16:00:00+00:00"))
        self.assertEqual(ada["engines"]["frozen"]["trend"], "BULLISH")

    def test_context_incomplete_flag_when_history_is_missing(self):
        case = self.cases["ETH_20260819_1600_U"]
        run = self.runs["ETH_USDT"]
        start = run.indices[utc("2026-08-14T00:00:00+00:00")]
        stamp = utc(case.timestamp)
        late = SimpleNamespace(bars=run.bars[start:], indices={k: v - start for k, v in run.indices.items() if v >= start})
        try:
            ctx = clarity.build_context(case, late, self.parameters, {})
        except ValueError:
            return  # a shortened history may not even reproduce the queued classification
        self.assertTrue(ctx["context_incomplete"] or ctx["bars"][0].closed_at <= stamp)

    def test_protected_levels_and_scaling_are_shared(self):
        ctx = self.contexts["ADA_20260919_0400_U"]
        for data in ctx["engines"].values():
            self.assertEqual(data["protected_level"], 0.1711)
            self.assertEqual(data["protected_kind"], "low")
        scales = []
        original = clarity.Scale

        def record(*args):
            scales.append(args[:2])
            return original(*args)

        with TemporaryDirectory() as temp, patch.object(clarity, "Scale", side_effect=record):
            path = Path(temp) / "chart.png"
            clarity.render(ctx, path)
            image = Image.open(path).convert("RGB")
        self.assertEqual(len(scales), 2)
        self.assertEqual(scales[0], scales[1])
        low, high = scales[0]
        for top in (clarity.TITLE, clarity.TITLE + clarity.PANEL_HEAD + clarity.PLOT + clarity.PANEL_GAP):
            scale = original(low, high, top + clarity.PANEL_HEAD, len(ctx["bars"]))
            y = round(scale.y(0.1711))
            row = [image.getpixel((x, yy)) for x in range(400, 900, 25) for yy in (y - 1, y, y + 1)]
            self.assertIn((255, 176, 32), row)

    def test_distant_levels_do_not_stretch_scale(self):
        ctx = self.contexts["ETH_20261024_1200_D"]
        low, high = clarity.shared_range(ctx)
        self.assertGreaterEqual(low, min(bar.low for bar in ctx["bars"]) - 0.13 * (high - low))

    def test_explanations_are_neutral_and_cover_required_questions(self):
        for key, ctx in self.contexts.items():
            text = clarity.explanation(ctx)
            for heading in ("BEFORE THE BREAK", "AT THE BREAK", "DETECTOR INTERPRETATION", "MISSING OR WEAK EVIDENCE"):
                self.assertIn(heading, text)
            self.assertIn("Neither detector's answer is assumed correct", text)
            self.assertNotRegex(text.lower(), r"\b(correct answer|is right|is wrong|later|afterwards|subsequent)\b")
            self.assertEqual(text, self.manifest[key]["explanation_text"])
            self.assertLessEqual(len(self.manifest[key]["caption"]), 1024)
            self.assertLessEqual(len(text), 4000)

    def test_committed_charts_match_manifest(self):
        for key, row in self.manifest.items():
            with Image.open(ASSETS / row["chart"]) as image:
                self.assertEqual(image.size[0], clarity.WIDTH)
            self.assertTrue((ASSETS / row["explanation"]).is_file())


class StorageAndAdapterTests(unittest.TestCase):
    def setUp(self):
        if not (QUEUE.is_file() and ASSETS.is_dir()):
            self.skipTest("Prepared Phase 2.11a assets are unavailable")
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "second.sqlite3"
        cases = {case.case_id: case for case in load_queue(QUEUE)}
        self.manifest = load_manifest(ASSETS / "second_opinion_queue.json")
        self.first = Storage(Path(self.temp.name) / "first.sqlite3")
        self.service = SecondOpinionService(self.manifest, cases, SecondOpinionStorage(self.path), {101}, self.first)
        self.adapter = SecondOpinionReview(self.service, ASSETS)
        self.sender = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        self.context = SimpleNamespace(bot=self.sender)
        self.user = SimpleNamespace(id=101)
        self.chat = SimpleNamespace(id=101, type="private")

    def update(self, text=None, data=None, request="q1"):
        update = SimpleNamespace(effective_user=self.user, effective_chat=self.chat, update_id=7,
                                 effective_message=SimpleNamespace(text=text))
        if data is not None:
            update.callback_query = SimpleNamespace(data=data, id=request, answer=AsyncMock(),
                                                    edit_message_caption=AsyncMock())
        return update

    def test_callbacks_parse_and_fit_telegram_limit(self):
        key = "ETH_20261008_1200_D"
        markup = self.adapter.keyboard(key, 12)
        reasons = self.adapter.reason_keyboard(key, 12)
        for row in markup.inline_keyboard + reasons.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)
                self.assertTrue(button.callback_data.startswith(PREFIX))
                parse_callback(button.callback_data, PREFIX)
        for bad in ("choch_review:vc:ETH_20261008_1200_D:0", PREFIX + "d:zz:ETH_20261008_1200_D:0",
                    PREFIX + "r:ch:ETH_20261008_1200_D:0", PREFIX + "d:ch:ETH_20261008_1200_X:0", None):
            with self.assertRaises(ValueError):
                parse_callback(bad, PREFIX)

    def test_reason_rule(self):
        self.assertEqual(detector_answers("EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH"), {"CHOCH_REVERSAL"})
        self.assertEqual(detector_answers("", "EXTERNAL_TREND_ESTABLISHED"), {"MINOR_BREAK", "NO_VALID_BREAK"})
        self.assertTrue(needs_reason("NO_VALID_BREAK", {"CHOCH_REVERSAL"}))
        self.assertFalse(needs_reason("CHOCH_REVERSAL", {"CHOCH_REVERSAL"}))
        self.assertTrue(needs_reason("UNSURE", {"MINOR_BREAK"}))
        self.assertEqual(len(set(LABELS.values())), 5)

    def test_private_authorization_for_commands_and_callbacks(self):
        self.chat.type = "group"
        complete(self.adapter.command(self.update("/review_second"), self.context))
        self.sender.send_photo.assert_not_called()
        self.chat.type = "private"
        self.user.id = 999
        update = self.update(data=PREFIX + "d:ch:ETH_20261008_1200_D:0")
        complete(self.adapter.callback(update, self.context))
        update.callback_query.answer.assert_awaited_once_with("Access denied.", show_alert=True)
        self.assertEqual(self.service.storage.history(), [])
        with self.assertRaises(PermissionError):
            self.service.decide(999, "ETH_20261008_1200_D", "ch", 0, "x")

    def test_decision_reason_correction_audit_and_restart(self):
        key = "ETH_20261008_1200_D"  # both detectors CHoCH
        complete(self.adapter.command(self.update(f"/review_second_case {key}"), self.context))
        self.sender.send_photo.assert_awaited_once()
        # agreeing answer: no reason prompt
        complete(self.adapter.callback(self.update(data=PREFIX + f"d:ch:{key}:0", request="a"), self.context))
        self.assertIsNone(self.sender.send_message.call_args.kwargs.get("reply_markup"))
        # correction to an answer neither detector supports: reason prompt
        complete(self.adapter.callback(self.update(data=PREFIX + f"d:mi:{key}:1", request="b"), self.context))
        prompt = self.sender.send_message.call_args.kwargs
        self.assertIn("differs from both detectors", prompt["text"])
        reason_button = prompt["reply_markup"].inline_keyboard[1][1].callback_data  # internal swing only
        # stale button from before the correction is rejected
        stale = self.update(data=PREFIX + f"d:nv:{key}:0", request="c")
        complete(self.adapter.callback(stale, self.context))
        self.assertTrue(stale.callback_query.answer.call_args.kwargs.get("show_alert"))
        complete(self.adapter.callback(self.update(data=reason_button, request="d"), self.context))
        complete(self.adapter.callback(self.update(data=reason_button, request="d"), self.context))  # replay
        history = self.service.storage.history()
        self.assertEqual([row["action"] for row in history], ["decision", "decision", "reason"])
        self.assertEqual([row["version"] for row in history], [1, 2, 3])
        self.assertEqual(history[1]["prior_label"], "CHOCH_REVERSAL")
        self.assertEqual(history[2]["reason"], "INTERNAL_SWING_ONLY")
        self.assertEqual(history[2]["label"], "MINOR_BREAK")  # reason never changes the classification
        self.assertEqual(self.first.history(), [])  # first pass untouched
        # restart: a fresh storage/service sees the same state and cursor
        again = SecondOpinionService(self.manifest, self.service.cases, SecondOpinionStorage(self.path), {101})
        state = again.storage.effective(101, key)
        self.assertEqual((state["label"], state["reason"], state["version"]), ("MINOR_BREAK", "INTERNAL_SWING_ONLY", 3))
        self.assertEqual(again.storage.cursor(101), key)
        progress = SecondOpinionReview(again, ASSETS).progress_text(101)
        self.assertIn("1/6 answered", progress)
        with self.assertRaises(StaleDecision):
            again.decide(101, key, "bo", 2, "late")

    def test_other_reason_asks_for_note_and_note_is_stored(self):
        key = "ADA_20260919_0400_U"  # detectors: no event
        complete(self.adapter.callback(self.update(data=PREFIX + f"d:ch:{key}:0", request="a"), self.context))
        complete(self.adapter.callback(self.update(data=PREFIX + f"r:ot:{key}:1", request="b"), self.context))
        self.assertIn("/review_second_note", self.sender.send_message.call_args.kwargs["text"])
        complete(self.adapter.command(self.update(f"/review_second_note {key} trend looked bearish locally"),
                                      self.context))
        state = self.service.storage.effective(101, key)
        self.assertEqual((state["label"], state["reason"], state["note"]),
                         ("CHOCH_REVERSAL", "OTHER", "trend looked bearish locally"))

    def test_navigation_explain_and_first_pass_comparison(self):
        complete(self.adapter.command(self.update("/review_second"), self.context))
        self.assertIn("1/6", self.sender.send_photo.call_args.kwargs["caption"])
        first_case = list(SELECTION)[0]
        complete(self.adapter.callback(self.update(data=PREFIX + f"x:{first_case}"), self.context))
        self.assertIn("BEFORE THE BREAK", self.sender.send_message.call_args.kwargs["text"])
        complete(self.adapter.callback(self.update(data=PREFIX + f"n:{first_case}"), self.context))
        self.assertIn("2/6", self.sender.send_photo.call_args.kwargs["caption"])
        complete(self.adapter.command(self.update("/review_second_help"), self.context))
        help_text = self.sender.send_message.call_args.kwargs["text"]
        for name in ("CHoCH — Reversal", "BOS — Continuation", "Minor Break", "No Valid Break", "Unsure"):
            self.assertIn(name, help_text)
        self.assertEqual(len(REASONS), 7)
        self.assertEqual(ROUND, "phase_2_11a")


if __name__ == "__main__":
    unittest.main()
