# Zero-Lag MTF Pullback v2 Optimization Roadmap

> This is a roadmap, not an executable plan — it sequences the nine
> priorities from the 2026-08-18 optimization request (`architecture.txt`)
> into individually executable plan documents and records why they're
> ordered this way. Each linked plan follows the standard
> `superpowers:writing-plans` format and can be executed on its own via
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`.

## Source diagnosis

Full diagnosis delivered in-conversation on 2026-08-18, based on the live
log (`mexc_bot.log`, 2026-08-12 16:43 → 2026-08-18 16:46, 53 completed
trades) and a fresh read of `strategy.py`, `config.py`, `database.py`,
`outcome_check.py`, `coin_scanner.py`, `main.py`,
`scripts/backtest_simple_strategy.py`, and the dead `backtest/` package.
Key verified findings (recomputed independently from the log, not just
trusted from the request doc):

- 53 completed trades, 29W/24L (54.7%), net -37 ROI points at equal
  sizing — matches `architecture.txt` exactly.
- LONG 28 trades, 13W/15L (46.4%, -59 pts). SHORT 25 trades, 16W/9L
  (64.0%, +22 pts) — also matches exactly.
- Score is **inversely** correlated with outcome in this sample: 80-84.9
  score → 58.3% WR (n=36), 85-89.9 → 50.0% (n=6), 90-100 → 45.5% (n=11).
  Small n, but consistent with the request's premise that the score
  weights are miscalibrated, not just noisy.
- 7/24 losses hit SL in **exactly ~3.98 minutes** (one 5m candle) after
  confirmation — a precise breakout-chase pattern, not "approximately
  four minutes." 5 of 7 are LONG.
- `CRYPTO_FUTURES_ONLY=true` is leaking tokenized-stock contracts
  (`TESLA_USDT`, `SOXL_USDT` both fired live signals) because
  `coin_scanner._NON_CRYPTO_KEYWORDS` only covers index/commodity/metal
  keywords, never individual equity tickers.
- The only backtest artifact on disk (`backtest/tpsl_walkforward.py`,
  empty `backtestfull.log`) is dead code — it imports
  `scalper_v3_strategy`, a module deleted with the retired Super Scalper
  v3 strategy. It cannot run. `scripts/backtest_simple_strategy.py` is
  the harness actually wired to the live Zero-Lag MTF Pullback v1 code
  (calls `strategy.detect_pending_setup`/`check_setup_confirmation`
  directly, lookahead-safe per commit `7296b5c`), but per CLAUDE.md's own
  note it has never been run for a real historical pass.

## Why this order

`architecture.txt` Priority 9 explicitly forbids tuning parameters
against the 53 live trades and asks for 500-1000+ backtested signals
before accepting any strategy modification. That makes the backtest
harness a hard prerequisite for every priority that picks a concrete
threshold (1, 2, 4's filter cutoffs, 5, 6). Writing bite-sized
implementation steps with specific numbers for those now — before the
walk-forward data exists — would violate the plan-writing "no
placeholders" rule the same way it would violate the request's own
anti-overfitting instruction: the number would just be a guess wearing a
plan's clothing.

So the roadmap runs in two phases.

## Phase 1 — buildable now, no backtest dependency

| # | Plan | Priority | Status |
|---|---|---|---|
| 1 | [`2026-08-18-crypto-only-classification-fix.md`](2026-08-18-crypto-only-classification-fix.md) | 8 | **Done** — implemented, tested, committed (`36ed398`) |
| 2 | [`2026-08-18-score-component-instrumentation.md`](2026-08-18-score-component-instrumentation.md) | 3 | **Done** — implemented, tested, committed (`19cb99c`, `4ac449b`, `031842b`) |
| 3 | [`2026-08-18-entry-quality-diagnostics.md`](2026-08-18-entry-quality-diagnostics.md) | 7 (diagnostics half) | **Done** — implemented, tested, committed (`20b2a2d`, `733ec3f`) |
| 4 | [`2026-08-18-walkforward-backtest-and-report.md`](2026-08-18-walkforward-backtest-and-report.md) | 9 | **Done** — Tasks 1-4 (`9b4baeb`, `9185d07`, `cc25f03`) plus Task 5 (`96de397`): real backtest run, 1136 trades / 4 months / 40 symbols, report at `backtest/reports/2026-08-19-zero-lag-v1-baseline-120d.log`. |

**Phase 1 is fully complete.** All code is on `main`, `python -m pytest -v` is green (94 tests), `py_compile` clean across every touched file. Contrary to this doc's original assumption, this sandbox *does* have live MEXC network access (verified directly) — Plan 4's Task 5 ran here rather than needing the production server. Scope was 120 days (not the originally-planned 270) so the run would finish within a single tool call rather than crossing a session/wakeup boundary, where a first attempt at 270 days/40 symbols was killed mid-run (background bash processes did not survive a `ScheduleWakeup`-triggered re-invocation in this environment — a real operational constraint for any future long-running background work here, not just this backtest). 120 days still cleared the 500+ signal target by more than 2x.

## Phase 1 results (2026-08-19)

`backtest/reports/2026-08-19-zero-lag-v1-baseline-120d.log` — 1136 trades, 2026-06 through 2026-08, 40 symbols (live coin-pool ranking), fees+slippage modeled, lookahead-safe.

- **58.5% win rate but -1202.9% net ROI-points.** The fixed +7%/-10% ROI TP/SL (RR 0.70:1) needs **~64.7% WR** to break even once the ~1% modeled fee+slippage cost lands on both sides (winner nets +6.0%, loser nets -11.0%) — not the raw 58.8% CLAUDE.md quotes, which ignores costs. Falling 6.2 points short of that compounds into -1.06%/trade expectancy despite winning the majority of trades. **This reframes the whole optimization effort: Priority 5 (RR/exit sizing) is likely the dominant lever, not score/regime filtering.**
- **LONG/SHORT asymmetry reversed vs. the 6-day live sample.** Live: LONG 46.4% WR / SHORT 64.0% WR. Backtest (n=1136): LONG 60.2% WR (n=594) / SHORT 56.6% WR (n=542) — LONG is actually *better* here. The live sample's asymmetry looks like short-window noise, not a stable structural edge. **Priority 1's directional-gate plan should not assume LONG is the weaker side without re-checking against this larger sample.**
- **Score miscalibration confirmed at scale.** 80-84.9 → 59.9% WR (n=703), 85-89.9 → 60.0% WR (n=201), 90-100 → **52.8% WR (n=232)**. The inverse pattern from the 53-trade live sample holds up 20x larger — the score is not just noisy, it's actively backwards at the top end.
- **Negative every single month** (June -273, July -620, August -310 ROI-points) — not a one-off bad stretch.
- Wide by-symbol dispersion (GPS_USDT +72%/79.3% WR vs. PUMPFUN_USDT -107%/52.8% WR) and by-hour dispersion (16:00 UTC +2.26% expectancy vs. 23:00 UTC -4.20%) exist but on modest per-bucket samples (n=17-64) — worth a second look once more data exists, not yet actionable alone.

**Recommended Phase 2 entry point given this data:** start with Priority 5 (ATR-based/higher-RR exits, backtested against this same 40-symbol/4-month window) rather than Priority 1 (directional gate) or Priority 2 (regime filter) — the RR shortfall is the largest, clearest, most robustly-evidenced lever, and the LONG/SHORT reversal means Priority 1 needs its own fresh backtest-driven look rather than inheriting the live sample's framing.

Plans 1-3 are mechanical/instrumentation — they change what gets
measured and logged, not the strategy's actual decisions, so they carry
no overfitting risk and can land independently in any order. Plan 4
(the walk-forward harness + a real run) is the long pole: it needs
Plan 2's score-component columns to exist first so the backtest output
can be sliced by score component the way the required evaluation report
asks (score range, regime, symbol, direction), so it should land last
within Phase 1.

## Phase 2 — blocked on Plan 4's output

These will each get their own plan document, written after Plan 4
produces real walk-forward numbers, because their concrete parameter
values (which regime measure, which score weights, which RR ratio,
whether breakeven helps) are exactly what that backtest is meant to
determine:

- Priority 1 — `LONG_MIN_SCORE`/`SHORT_MIN_SCORE` directional gate
  (the plumbing is mechanical and could be written now, but the values
  are meaningless without backtest calibration — bundling them avoids a
  throwaway first pass).
- Priority 2 — regime-strength filter (ADX / normalized slope / HH-HL
  structure) — which of these actually correlates with outcome is an
  empirical question Plan 4 answers.
- Priority 4 — entry-quality filter *thresholds* (Plan 3 above only adds
  the diagnostic logging; turning it into a reject filter needs to know
  which logged features actually predicted the fast losses).
- Priority 5 — ATR-based volatility-normalized exits vs. the fixed
  7%/10% ROI, compared at RR 1.0/1.2/1.3/1.5.
- Priority 6 — optional trade-management framework (breakeven / partial
  tightening / ATR trailing / time-based exit), each backtested
  independently before any is enabled live.

## Execution

Each Phase 1 plan is independently executable now via
`superpowers:subagent-driven-development` (recommended) or
`superpowers:executing-plans`. Recommended order: crypto-filter fix
first (smallest, zero risk, immediate live benefit), then score
instrumentation and diagnostics (either order), then the walk-forward
harness/run last.
