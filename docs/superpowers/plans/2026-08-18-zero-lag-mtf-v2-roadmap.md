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
| 1 | [`2026-08-18-crypto-only-classification-fix.md`](2026-08-18-crypto-only-classification-fix.md) | 8 | Ready to execute |
| 2 | [`2026-08-18-score-component-instrumentation.md`](2026-08-18-score-component-instrumentation.md) | 3 | Ready to execute |
| 3 | [`2026-08-18-entry-quality-diagnostics.md`](2026-08-18-entry-quality-diagnostics.md) | 7 (diagnostics half) | Ready to execute |
| 4 | [`2026-08-18-walkforward-backtest-and-report.md`](2026-08-18-walkforward-backtest-and-report.md) | 9 | Ready to execute |

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
