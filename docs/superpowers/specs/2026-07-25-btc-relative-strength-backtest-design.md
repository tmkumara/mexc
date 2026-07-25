# BTC Relative-Strength Continuation — Phase 1 Backtest Design

## Context

This bot has run through 17+ documented strategy generations (see `MEMORY.md` /
project history) plus, in this session alone, three more tested and rejected
on real 6-month MEXC data: Fibonacci MTF (-4754.6% ROI, 1791 trades), a
Nadaraya-Watson kernel strategy (thin samples, negative), and Hull Suite in
seven variants (all converging to a ~66-68% win rate against whatever
breakeven a given TP:SL ratio requires — a consistent ~9-10 point edge
deficit that no amount of multi-timeframe filtering closed).

Every one of those, plus the currently-live Super Scalper v3 (PF≈1.02,
WR≈54.8% over 619 backtested trades — the best result achieved so far), is
built from the same ingredient: OHLCV candle data with an indicator layered
on top. None has used a genuinely different mechanism.

Two candidate "genuinely different" data sources were considered and
rejected on feasibility grounds before this one:
- **Liquidation-cluster / open-interest data** (`liq_estimator.py` already
  exists, built but never wired to a live feed) — MEXC's public API has
  **no historical open-interest endpoint**, confirmed via their official API
  docs. Only a live current snapshot is available. This mechanism cannot be
  backtested on history; it could only be validated by weeks of live
  forward-collection, breaking the "prove it on history before risking
  anything" discipline this whole session has used.
- **Order book depth/imbalance** — same fatal flaw: no historical L2 data
  available for backtesting.

**BTC relative-strength continuation** was chosen instead: it reuses OHLCV
data already fetched (`backtest/data/*.parquet`, 8 symbols, 6 months), so it
can be backtested immediately with the same rigor as everything else this
session, while still testing a mechanism never tried in this repo — relative
(not absolute) price action versus BTC.

## Hypothesis

During a BTC move, altcoins that are outperforming/underperforming BTC over
a recent lookback window tend to keep outperforming/underperforming (a
momentum/relative-strength effect, one of the most replicated anomalies in
traditional-market academic finance; plausible crypto analogue: "alt
season" / risk-on-risk-off dynamics, where strong alts keep leading BTC
rallies and weak alts keep bleeding harder in BTC drops).

## Scope: Phase 1 only (backtest-only prototype, no production code)

Explicitly **not** in scope for this spec: DB schema, live scanning,
Telegram broadcast, DRY_RUN wiring, or any integration with `main.py` /
`scalper_v3_strategy.py`. This is a go/no-go check. Phase 2 (productionize)
is only scoped if Phase 1 shows a clear, consistent edge.

## Design

**Universe:** the 8 already-fetched symbols (BTC_USDT + 1000BONK, ENA, ETH,
SOL, WLD, XPL, XRP), 6 months of 5m data, entry timeframe 5m (matches v3's
cadence, per user preference — keeps this a scalp-style strategy consistent
with the rest of the bot and gets a usable sample size from 6 months of
history).

**Signal, computed independently per alt symbol at each bar (no
cross-sectional ranking needed — only 7 alts in the test universe, and
per-symbol independence keeps the backtest simple and matches how v3's
per-symbol engine already works):**

```
btc_return = BTC's cumulative % return over the lookback window, ending at this bar
alt_return = this alt's cumulative % return over the same window
rs_score   = alt_return - btc_return   (excess return vs BTC)

LONG  when btc_return > +move_threshold   AND  rs_score > +rs_threshold
SHORT when btc_return < -move_threshold   AND  rs_score < -rs_threshold
```

No lookahead: `btc_return`/`alt_return`/`rs_score` computed as of each bar's
CLOSE; entry fills at the next bar's OPEN, matching the no-lookahead
convention already used in `backtest/engine.py` and every ad-hoc script this
session.

**Parameter sweep (a small grid — this is a go/no-go check, not a full
optimization pass — 3×3×3 = 27 combinations, one aggregate backtest each
across all 8 symbols):**
- `lookback_window` ∈ {1h, 2h, 4h} (12, 24, 48 bars at 5m resolution)
- `move_threshold` ∈ {0.5%, 1.0%, 2.0%} (BTC's own move over the lookback)
- `rs_threshold` ∈ {1.0%, 2.0%, 3.0%} (excess return required)

**Sizing (matches the flat convention used throughout this session, for
direct comparability to the PF≈1.02 baseline):** 20x leverage, flat TP/SL in
ROI terms (reuse the current live v3 values — 10% ROI TP / 10% ROI SL — as
the default sizing so any edge found is attributable to the entry signal,
not to picking a favorable TP:SL ratio; SL-first same-bar tie-break; one
position at a time per symbol.

**Output:** aggregate win rate / profit factor / trade count per parameter
combination, printed in a table (same format as the Hull TP-widen sweep),
plus a per-symbol breakdown for the best-performing combination.

## Success criteria

Worth a Phase 2 (productionization) investment **only if** profit factor
clearly and consistently exceeds 1.0 across multiple parameter combinations
(not one cherry-picked lucky setting) with a meaningful aggregate trade
count (roughly 200+, enough to not be noise — smaller samples in this
session's other backtests were explicitly flagged as low-confidence). If the
result lands in the same 0.6-1.05 range everything else has landed in, this
gets recorded as a fourth rejected mechanism and the strategy hunt pauses
there, same as the last conversation's conclusion.

## Out of scope / explicitly not doing

- No cross-sectional ranking across a larger coin universe (would need
  fetching more symbols' history — deferred to Phase 2 if Phase 1 succeeds).
- No production wiring (DB, Telegram, scheduler) — Phase 1 is backtest-only.
- No funding-rate or liquidation-based signals — separate ideas, not part of
  this mechanism.
