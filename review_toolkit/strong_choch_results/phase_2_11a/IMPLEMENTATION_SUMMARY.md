# Phase 2.11a — Clearer CHoCH review charts and a second-opinion pass

Status: **implemented and verified locally only.** Nothing is committed, pushed, deployed or restarted. The production database has not been touched.

## 1. Files changed (release checkout `.phase210-release`, based on production `f975e0b`)

| File | Change |
|---|---|
| `review_toolkit/research/strong_choch/review/clarity.py` | **New.** Replays only the candles up to each reviewed close, then builds the new charts, neutral explanations and the manifest. |
| `review_toolkit/research/strong_choch/review/second_opinion.py` | **New.** The six-case selection, the five answers and their definitions, the seven reasons, the separate SQLite storage, the service and the export. |
| `review_toolkit/research/strong_choch/review/telegram_second.py` | **New.** The Telegram adapter for `/review_second*`, using the `choch_review2:` button namespace. |
| `review_toolkit/research/strong_choch/review/__main__.py` | Adds the `prepare-second` and `export-second` subcommands. |
| `review_toolkit/research/strong_choch/review/telegram_bot.py` | Adds an optional `help_suffix`, so `/review_help` can point to the second pass. Nothing else changes. |
| `telegram_review_integration.py` | Adds `attach_second_opinion()`, mounted on the same `Application`. It is skipped if the first pass is disabled. |
| `review_toolkit/tests/test_second_opinion.py` | **New.** 15 toolkit tests. |
| `tests/test_integrated_second_opinion.py` | **New.** 5 bot-integration tests. |
| `review_toolkit/strong_choch_results/phase_2_11a/` | **New.** The generated artifacts (listed below). |

Not touched:
- The frozen detector (`strategy/market_structure/*`) and the shadow detector (`shadow_protection.py`).
- The live strategy, scanner, config and `main.py`.
- The Phase 2.10 and 2.11 artifacts. The main checkout's unrelated changes are preserved.

## 2. The six second-opinion cases

The source of truth is §7 of `strong_choch_results/phase_2_11/PHASE_2_11_REVIEW_ANALYSIS.md`. A test checks this list against that report.

| # | Case | Why it was selected | Frozen | Shadow | First pass |
|---|---|---|---|---|---|
| 1 | ADA_20260919_0400_U | A "Valid CHoCH" label on an upward break inside an external uptrend. The prior trend start was off the old chart. | no event | no event | Valid CHoCH |
| 2 | ETH_20261008_1200_D | Both detectors report CHoCH (shadow says Strong), but the first pass said Not CHoCH. It is the only Strong case. | CHoCH | Strong CHoCH | Not CHoCH |
| 3 | BNB_20261020_2000_U | A "Valid CHoCH" label while both detectors were in an unknown state; possibly a missed CHoCH. | no event | no event | Valid CHoCH |
| 4 | ETH_20260819_1600_U | The detectors disagree on CHoCH: a range breakout, or a reversal? | new trend | CHoCH | Not CHoCH |
| 5 | ETH_20261024_1200_D | The first-pass label was Ambiguous. | no event | no event | Ambiguous |
| 6 | ETH_20260821_0400_U | A three-way disagreement. | BOS | new trend | Ordinary Break |

**Correction to Phase 2.11.** The report says BNB's state was reset by a CHoCH on "Oct 7". The replay shows that the reset CHoCH closed at **2026-10-08 16:00 UTC**, breaking 746.30. The Phase 2.11 report itself is left unchanged.

## 3. Chart improvements (`charts/*.png`, 1280×1350, made for phone screens)

- **Same scale:** one shared time axis and one shared price scale for the frozen and shadow panels.
- **Trend context:** the window always reaches back to the prior trend's start, or to the CHoCH that reset an unknown state, and has at least 60 candles. For ADA the chart now starts 07-27 and shows the 07-30 uptrend start.
- **Previous trend** is written in each panel's header, for example "BULLISH since 09-21 12:00 · 0 BOS · protected low 2562.96" or "UNKNOWN since CHoCH bearish at 10-08 16:00".
- **Swings:** the last 8 significant confirmed swings are labelled HH/HL/LH/LL with their prices. Older significant swings are plain dots. Minor confirmed pivots are hollow rings.
- **Not-yet-confirmable swings:** the last 3 candles are shaded, because a swing there cannot be confirmed yet (it needs 3 candles on its right).
- **Levels:** the protected level (orange), the trend boundary (blue, dashed) and the broken level (green or red, marked "BROKEN = PROTECTED" when they are the same) all carry their exact price. When no detector event fired, the minor swing the close crossed is shown in grey.
- **Break candle:** highlighted with a band and an arrow, with the close price tagged on the axis.
- **Trend markers:** a vertical marker shows where the prior trend was set, or where the trend was reset.
- **Readable labels:** labels are placed so they don't cover candles or each other, and axis tags have collision handling.
- **No stretched scale:** levels far from the visible candles are not drawn across the chart; they get an edge note saying "↑/↓ … (off chart)".
- **No future candles:** the title says "ENDS at the reviewed close — no later candles". A test proves that removing all later candles from the dataset changes nothing.
- **Incomplete history:** if the trend setup needs candles from before the dataset starts, the chart prints a warning. None of the six cases needed it.

## 4. Telegram — what the reviewer sees

New private-chat-only commands, on the existing bot:

| Command | Action |
|---|---|
| `/review_second` | Opens the first unanswered case (or the case you were on) |
| `/review_second_next` / `/review_second_previous` | Moves to the next unanswered case / the previous case |
| `/review_second_case <id>` | Opens a specific case |
| `/review_second_progress` | Shows answered count, pending reasons and the status of each case |
| `/review_second_note <id> <text>` | Adds a free-text note to your current answer |
| `/review_second_help` | Shows the definitions of the five answers |

Each case arrives as **one photo** (the paired chart). The caption gives:
- the symbol, close time, break direction and close;
- frozen's and shadow's classification and prior trend;
- the main thing they differ on.

The buttons are:

```
[ CHoCH — Reversal ] [ BOS — Continuation ]
[ Minor Break      ] [ No Valid Break     ]
[ Unsure           ] [ Explain            ]
[ ◀ Previous       ] [ Next ▶             ]
```

- **Explain** sends the plain-English walkthrough (Before the break / At the break / Detector interpretation / Missing evidence).
- After an answer, the bot confirms it and shows the **first-pass** answer for comparison. The first-pass answer is never shown before you answer, and never changed.
- If the answer is consistent with **neither** detector, or is **Unsure**, the bot asks for a one-tap reason:
  - Previous trend unclear
  - Protected level incorrect
  - Break in same direction as trend
  - Only an internal swing broken
  - Candle did not close beyond level
  - Insufficient chart history
  - Other / custom note (this one prompts `/review_second_note`)
- A reason never changes the chosen answer.
- `/review_help` (first pass) now ends with a pointer to `/review_second` and lists the five answers.
- The original `/review` flow, its five buttons and its 22 decisions are unchanged.

How each detector result maps to the answers it is consistent with, for the reason prompt:
- CHoCH / Strong CHoCH → CHoCH — Reversal.
- BOS → BOS — Continuation.
- No event, or a new trend without a protected break → Minor Break or No Valid Break.

## 5. Storage design

- **Separate file:** `/var/lib/mexc-choch-review/second_opinion.sqlite3`. It is created with mode 0600 on first start in the existing private 0700 directory. It can be overridden with `MEXC_REVIEW_SECOND_STORAGE_PATH`, but must be an absolute path outside the checkout and different from the first-pass file.
- **Tables:**
  - `second_history`, append-only. Each row holds the round, case, reviewer, label, reason, note, action (decision/reason/note), version, the prior label and prior reason, and a UTC timestamp. `UNIQUE(round, case, reviewer, version)`, and `request_id` is unique, so repeated taps are idempotent.
  - `second_navigation`, which stores the cursor.
- **Corrections** add a new version. Older rows are never updated or deleted. A stale button (from an older version) is rejected.
- **First pass:** `review.sqlite3` is only **read**, to show the first-pass answer after you respond. A test checks that its SHA-256 is unchanged.
- **Export:** `python -m research.strong_choch.review export-second --storage <copy> --output <dir>` writes `second_opinion_history.csv` and `first_vs_second.csv`. Run it on a copy of the database, because opening it ensures the tables exist.

## 6. Backward compatibility

- **Live signal commands:** unchanged. Handlers 0–8 are the same `cmd_*` functions; a test checks this.
- **First-pass handlers:** handlers 9–10 are unchanged. The second pass adds handlers 11–12.
- **Separate button namespaces:** the existing `^choch_review:` pattern does not match `choch_review2:`, and the reverse is also true. A test checks both.
- **No new bot process:** no new Telegram bot, token, `Application`, poller or `getUpdates`. Tests check that neither `run_polling` nor `start_polling` is called while building the app.
- **Failure isolation:** if the second-pass assets or config are missing or invalid, only the second pass is disabled and a warning is logged. If the first pass is disabled, the second pass doesn't attach.
- **Runtime dependencies:** none added. Pillow is only needed to *generate* charts, and the generated PNGs ship with the code.

## 7. Tests

See `test_results.txt` for the full output.
- **Toolkit:** 15 Phase 2.11a tests and 10 existing Phase 2.10 tests, all passing.
- **Bot integration:** 5 new and 5 existing tests, all passing.
- **Not run:** 9 other repository test modules can't be imported in this offline environment (`pytest` is not installed). That is environmental and unchanged from the Phase 2.10.1 baseline.

## 8. Detector fingerprint

- Frozen lock `ccd1f9bc…9999` = current `ccd1f9bc…9999` (**MATCH**).
- `git diff f975e0b` is empty for the detector, strategy, scanner, config and `main.py`.
- Each case's replay reproduces the queued frozen and shadow classifications. Generation aborts otherwise.

## 9. Deployment requirements (each step needs approval)

1. Back up the server's first-pass database first, for example `sqlite3 /var/lib/mexc-choch-review/review.sqlite3 ".backup /root/review-pre-2.11a.sqlite3"`, or a read-only copy.
2. Commit only the paths in §1 on the release branch, then push to `main`. The existing **Deploy to Server** workflow will restart `mexc-bot` and `mexc-dashboard`.
3. No new environment variables are required. The existing allowlist drop-in is reused. Optional flags: `MEXC_REVIEW_SECOND_ENABLED=0`, `MEXC_REVIEW_SECOND_STORAGE_PATH`, `MEXC_REVIEW_SECOND_MANIFEST_PATH`.
4. Verify:
   - the deployed revision;
   - both services are active;
   - the log shows `Private CHoCH review enabled (22 cases)` **and** `Private CHoCH second-opinion review enabled (6 cases)`;
   - `second_opinion.sqlite3` is mode 0600;
   - `review.sqlite3` is unchanged (22 decisions);
   - there is still one `main.py` poller.
5. The reviewer then runs `/review_second` in a private chat. No automatic decisions are made.

## 10. Rollback

- **Without a restart:** `touch /var/lib/mexc-choch-review/disabled` hides both review passes immediately. Live signals keep running.
- **Second pass only:** add `MEXC_REVIEW_SECOND_ENABLED=0` to the `mexc-bot` drop-in. It takes effect at the next planned restart.
- **Full:** redeploy `f975e0b`. The old code ignores `second_opinion.sqlite3`, and both databases stay on disk untouched.

## Artifacts in this folder

| Item | Contents |
|---|---|
| `second_opinion_queue.json` | Round, fingerprint, and per case: detector context, swings, caption, explanation, first-pass label, selection reason |
| `charts/<case>.png` | Six improved frozen/shadow chart pairs |
| `explanations/<case>.md` | Six neutral plain-English explanations |
| `first_vs_second_template.csv` | First-pass label pre-filled; second-pass columns empty |
| `test_results.txt` | Focused test output and the fingerprint check |
| `IMPLEMENTATION_SUMMARY.md` | This file |

Reproduce the whole folder from the existing datasets (from `review_toolkit/`):

```
python -m research.strong_choch.review prepare-second --first-pass <phase_2_11 review_decisions.csv> --output strong_choch_results/phase_2_11a
```
