"""Prepare, export, or explicitly start the historical review bot."""

import argparse
import os
from pathlib import Path
import re

from dotenv import load_dotenv

from .evaluation import export
from .queue import load_queue
from .service import Service
from .storage import Storage


ROOT = Path("strong_choch_results")
DEFAULT = ROOT / "phase_2_10"


def reviewer_ids(raw):
    pieces = re.split(r"[,\s]+", raw.strip()) if raw.strip() else []
    if not pieces or any(not re.fullmatch(r"[1-9]\d{0,15}", value) for value in pieces):
        raise ValueError("Set MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS to positive Telegram user IDs.")
    return {int(value) for value in pieces}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, default=ROOT / "phase_2_4")
    prep.add_argument("--queue", type=Path, default=ROOT / "phase_2_9/manual_review_queue.csv")
    prep.add_argument("--freeze", type=Path, default=ROOT / "phase_2_5/algorithm_fingerprint.json")
    prep.add_argument("--output", type=Path, default=DEFAULT)
    for action in ("bot", "export"):
        command = commands.add_parser(action)
        command.add_argument("--queue", type=Path)
        command.add_argument("--storage", type=Path)
        if action == "export":
            command.add_argument("--output", type=Path, required=True)
    second = commands.add_parser("prepare-second")
    second.add_argument("--source", type=Path, default=ROOT / "phase_2_4")
    second.add_argument("--queue", type=Path, default=DEFAULT / "review_queue.csv")
    second.add_argument("--freeze", type=Path, default=ROOT / "phase_2_5/algorithm_fingerprint.json")
    second.add_argument("--first-pass", type=Path, help="first-pass review_decisions.csv export (read only)")
    second.add_argument("--output", type=Path, default=ROOT / "phase_2_11a")
    second_export = commands.add_parser("export-second")
    second_export.add_argument("--manifest", type=Path, default=ROOT / "phase_2_11a/second_opinion_queue.json")
    second_export.add_argument("--storage", type=Path, required=True)
    second_export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        from .chart import prepare
        count = prepare(args.source, args.queue, args.freeze, args.output)
        print(f"Prepared {count} review cases and paired charts in {args.output}")
        return
    if args.action == "prepare-second":
        from .clarity import prepare_second
        count = prepare_second(args.source, args.queue, args.freeze, args.first_pass, args.output)
        print(f"Prepared {count} second-opinion cases in {args.output}")
        return
    if args.action == "export-second":
        from .second_opinion import SecondOpinionStorage, export_second, load_manifest
        if not args.storage.is_file():
            parser.error("Second-opinion storage file not found")
        count = export_second(load_manifest(args.manifest), SecondOpinionStorage(args.storage), args.output)
        print(f"Exported {count} second-opinion history rows to {args.output}")
        return
    load_dotenv()
    queue_path = args.queue or Path(os.getenv("MEXC_REVIEW_QUEUE_PATH") or DEFAULT / "review_queue.csv")
    storage_path = args.storage or Path(os.getenv("MEXC_REVIEW_STORAGE_PATH") or DEFAULT / "review.sqlite3")
    cases = load_queue(queue_path)
    if args.action == "export":
        result = export(args.output, cases, Storage(storage_path))
        print(f"Exported {result['reviewed_cases']} reviewed cases of {result['total_cases']}.")
        return
    token = os.getenv("MEXC_REVIEW_TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"[1-9]\d{0,15}:[A-Za-z0-9_-]{20,100}", token):
        parser.error("Set MEXC_REVIEW_TELEGRAM_BOT_TOKEN to the review bot token.")
    if token == os.getenv("TELEGRAM_TOKEN"):
        parser.error("Use a dedicated review bot token.")
    try:
        allowed = reviewer_ids(os.getenv("MEXC_REVIEW_TELEGRAM_ALLOWED_USER_IDS", ""))
    except ValueError as exc:
        parser.error(str(exc))
    from .telegram_bot import TelegramReview, start
    service = Service(cases, Storage(storage_path), allowed)
    start(token, TelegramReview(service, queue_path.parent / "charts", queue_path.parent / "chart_manifest.json"))


if __name__ == "__main__":
    main()
