from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from research.strong_choch.data import utc
from research.strong_choch.evaluate import read_rows
from research.strong_choch.replay import write_rows
from research.strong_choch.review.__main__ import reviewer_ids
from research.strong_choch.review.evaluation import export, report
from research.strong_choch.review.queue import load_queue
from research.strong_choch.review.service import Service, parse_callback
from research.strong_choch.review.storage import StaleReview, Storage
from research.strong_choch.review.telegram_bot import Application, TelegramReview, start
from strategy.market_structure.models import FOUR_HOURS

ROOT = Path(__file__).resolve().parents[1] / "strong_choch_results"
SOURCE = ROOT / "phase_2_9/manual_review_queue.csv"
PREPARED = ROOT / "phase_2_10"


def complete(coroutine):
    """Mock operations finish without an asyncio network loop."""
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("Mock Telegram operation unexpectedly suspended")


class ReviewTests(unittest.TestCase):
    def setUp(self):
        if not SOURCE.is_file():
            self.skipTest("Saved Phase 2.9 queue is unavailable")
        self.original = SOURCE.read_bytes()
        self.cases = load_queue(SOURCE)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = Storage(self.root / "review.sqlite3")
        self.service = Service(self.cases, self.storage, {101, 202})

    def test_queue_ids_and_prioritization_are_independent_of_file_order(self):
        self.assertEqual(len(self.cases), 22)
        self.assertTrue(self.cases[0].evidence["review_focus"])
        self.assertIn("BNB_20261030_0000_D", self.service.cases)
        rows = read_rows(SOURCE)
        shuffled = self.root / "reordered.csv"
        write_rows(shuffled, tuple(rows[0]), reversed(rows))
        self.assertEqual(load_queue(shuffled), self.cases)
        bad = self.root / "duplicate.csv"
        write_rows(bad, tuple(rows[0]), [rows[0], rows[0]])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_queue(bad)

    def test_authorization_and_callback_validation(self):
        self.assertEqual(reviewer_ids("101, 202"), {101, 202})
        self.assertFalse(self.service.authorized(101, "group"))
        with self.assertRaises(PermissionError):
            self.service.decide(999, self.cases[0].case_id, "vc", 0, "invalid")
        for payload in ("r:vc:../../path:0", "r:bad:BNB_20261030_0000_D:0",
                        "r:vc:BNB_20261030_0000_D:-1", "x" * 65, None):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_callback(payload)
        self.assertEqual(parse_callback("r:vc:BNB_20261030_0000_D:0"),
                         ("vc", "BNB_20261030_0000_D", 0))

    def test_duplicate_delivery_corrections_undo_restart_and_reviewer_separation(self):
        key = self.cases[0].case_id
        self.assertTrue(self.service.decide(101, key, "vc", 0, "one"))
        self.assertFalse(self.service.decide(101, key, "vc", 0, "one"))
        self.assertFalse(self.service.decide(101, key, "vc", 1, "identical"))
        self.assertTrue(self.service.decide(101, key, "nc", 1, "two"))
        with self.assertRaises(StaleReview):
            self.service.decide(101, key, "vb", 1, "old-card")
        self.assertTrue(self.service.decide(202, key, "vb", 0, "other-user"))
        self.assertEqual(Storage(self.storage.path).effective(101, key)["review_label"], "NOT_CHOCH")
        self.assertEqual(self.service.undo(101, 300), key)
        self.assertIsNone(self.service.undo(101, 300))
        self.assertEqual(self.storage.effective(101, key)["review_label"], "VALID_CHOCH")
        self.assertEqual(self.storage.effective(202, key)["review_label"], "VALID_BOS")
        self.assertEqual([row["action"] for row in self.storage.history()],
                         ["decision", "decision", "decision", "undo"])
        self.assertEqual(self.service.undo(101, 301), key)
        self.assertIsNone(self.storage.effective(101, key)["review_label"])

    def test_skip_remains_unreviewed_and_navigation_survives_restart(self):
        current = self.service.navigate(101, "next")
        destination = self.service.navigate(101, "skip")
        self.assertNotEqual(current.case_id, destination.case_id)
        self.assertIsNone(self.storage.effective(101, current.case_id))
        self.assertEqual(Storage(self.storage.path).cursor(101), destination.case_id)
        self.assertEqual(self.service.navigate(101, "previous").case_id, current.case_id)

    def test_notes_and_exports_preserve_distinct_labels(self):
        key = self.cases[0].case_id
        self.assertTrue(self.service.note(101, key, "first line\nsecond line", 1))
        self.assertIsNone(self.storage.effective(101, key)["review_label"])
        self.assertTrue(self.service.decide(101, key, "nc", 1, "decision"))
        self.assertTrue(self.service.note(101, key, "=formula-like text", 2))
        summary = export(self.root / "exports", self.cases, self.storage)
        self.assertEqual(summary["reviewed_cases"], 1)
        decisions = read_rows(self.root / "exports/review_decisions.csv")
        self.assertEqual(decisions[0]["review_label"], "NOT_CHOCH")
        self.assertEqual(decisions[0]["notes"], "'=formula-like text")
        self.assertEqual(self.storage.effective(101, key)["notes"], "=formula-like text")
        self.assertEqual(len(read_rows(self.root / "exports/review_history.csv")), 3)
        self.assertEqual(SOURCE.read_bytes(), self.original)

    def test_accuracy_and_exclusions(self):
        predictions = [("", "EXTERNAL_CHOCH"), ("EXTERNAL_CHOCH", "EXTERNAL_CHOCH"),
                       ("EXTERNAL_CHOCH", ""), ("", ""), ("", ""), ("", ""), ("", "")]
        cases = [replace(case, evidence=MappingProxyType(dict(case.evidence, frozen_classification=frozen,
                                                             shadow_classification=shadow)))
                 for case, (frozen, shadow) in zip(self.cases, predictions)]
        for case, label in zip(cases, ("VALID_CHOCH", "VALID_CHOCH", "NOT_CHOCH",
                                      "ORDINARY_BREAK", "AMBIGUOUS", "VALID_BOS")):
            self.storage.write(case, 101, case.case_id, label=label)
        self.storage.write(cases[5], 202, "disagreement", label="VALID_CHOCH")
        result = report(cases, self.storage.history())
        self.assertEqual((result["reviewed_cases"], result["unreviewed_cases"], result["definitive_cases"]), (6, 1, 4))
        self.assertEqual(result["ambiguous_cases"], 1)
        self.assertEqual(result["reviewer_disagreements"], [cases[5].case_id])
        self.assertEqual(result["frozen"]["precision"], .5)
        self.assertEqual(result["frozen"]["recall"], .5)
        self.assertEqual(result["shadow"]["precision"], 1)
        self.assertEqual(result["shadow"]["recall"], 1)
        self.assertIsNone(result["strong_accuracy"])

    def adapter(self):
        if not (PREPARED / "chart_manifest.json").is_file():
            self.skipTest("Review charts have not been prepared")
        return TelegramReview(self.service, PREPARED / "charts", PREPARED / "chart_manifest.json")

    def test_mocked_telegram_blocks_unauthorized_commands_and_callbacks(self):
        adapter = self.adapter()
        case_id = self.cases[0].case_id
        query = SimpleNamespace(data=f"r:vc:{case_id}:0", id="same", answer=AsyncMock(),
                                edit_message_caption=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=99),
                                 effective_user=SimpleNamespace(id=999), callback_query=query,
                                 effective_message=SimpleNamespace(text="/next"))
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        complete(adapter.command(update, SimpleNamespace(bot=bot)))
        complete(adapter.callback(update, SimpleNamespace(bot=bot)))
        query.answer.assert_awaited_once_with("Access denied.", show_alert=True)
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_not_awaited()
        self.assertFalse(self.storage.history())
        update.effective_user.id = 101
        update.effective_chat.type = "supergroup"
        complete(adapter.callback(update, SimpleNamespace(bot=bot)))
        self.assertEqual(query.answer.await_count, 1)
        self.assertFalse(self.storage.history())

    def test_mocked_telegram_sends_chart_and_edits_caption_once(self):
        adapter = self.adapter()
        case_id = self.cases[0].case_id
        query = SimpleNamespace(data=f"r:vc:{case_id}:0", id="first", answer=AsyncMock(),
                                edit_message_caption=AsyncMock())
        update = SimpleNamespace(effective_chat=SimpleNamespace(type="private", id=99),
                                 effective_user=SimpleNamespace(id=101), callback_query=query,
                                 effective_message=SimpleNamespace(text="/next"))
        bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
        context = SimpleNamespace(bot=bot)
        complete(adapter.command(update, context))
        bot.send_photo.assert_awaited_once()
        self.assertFalse(self.storage.history())
        complete(adapter.callback(update, context))
        complete(adapter.callback(update, context))
        query.edit_message_caption.assert_awaited_once()
        self.assertEqual(len(self.storage.history()), 1)

    def test_polling_registration_without_network(self):
        adapter = self.adapter()
        with patch.object(Application, "run_polling", autospec=True) as polling:
            start("123456:LocalMockTokenWithNoNetwork", adapter)
        application = polling.call_args.args[0]
        self.assertEqual(polling.call_args.kwargs["allowed_updates"], ("message", "callback_query"))
        self.assertIn("undo", application.handlers[0][0].commands)
        self.assertIn("reason", application.handlers[0][0].commands)

    def test_prepared_charts_end_at_case_close_and_preserve_classifications(self):
        adapter = self.adapter()
        prepared = load_queue(PREPARED / "review_queue.csv")
        self.assertEqual(len(prepared), len(self.cases))
        for old, case in zip(self.cases, prepared):
            self.assertEqual(old.event_id, case.event_id)
            manifest = adapter.manifest[case.case_id]
            close = utc(case.timestamp)
            self.assertEqual(utc(manifest["last_candle_close"]), close)
            self.assertEqual(utc(manifest["last_candle_open"]) + FOUR_HOURS, close)
            with Image.open(PREPARED / "charts" / f"{case.case_id}.png") as image:
                self.assertEqual(image.format, "PNG")
                image.verify()
            for engine in ("frozen", "shadow"):
                self.assertEqual(old.evidence[f"{engine}_classification"],
                                 case.evidence[f"{engine}_classification"])
