# Super Scalper v3 backtest — runbook

## Why this has to run somewhere other than the session that wrote it

The code in this directory was written and unit-tested (against synthetic
data only) in a sandboxed Claude Code session whose network egress policy
blocks `contract.mexc.com` (confirmed with a live request — 403 policy
denial). That means **no real MEXC kline data was fetched and no real
backtest numbers were produced.** Every function here is exercised by
`tests/test_scalper_v3_strategy.py` and `tests/test_backtest_engine.py`
using hand-built or synthetic OHLCV, which validates the *mechanics*
(no-lookahead entry timing, SL-first same-bar tie-break, TP1→breakeven,
trailing stop, position sizing, walk-forward window construction, grid
search rejection logic, report/plot generation) but says nothing about
whether this strategy is actually profitable on real BTC/ETH/SOL-style
price action.

Run the three steps below on a host that can actually reach MEXC — the
production server (`68.168.222.74`, see the root `CLAUDE.md`) works, or
any machine with unrestricted internet.

## 0. Setup

```bash
cd /opt/signals   # or wherever this repo lives on the host you're using
source venv/bin/activate
pip install -r requirements.txt   # now includes pyarrow + matplotlib
```

## 1. Fetch 6 months of data (Phase 2)

**Important, learned the hard way:** MEXC's `/contract/kline` REST
endpoint only retains ~30 days of history for the `Min1` interval —
requesting anything older silently returns 0 bars (no error). 30 days
isn't enough for even one 8-week walk-forward window. Fetch at **5m**
instead (the strategy's actual entry timeframe — `SCALPER_V3_TIMEFRAME`
in config.py), which MEXC retains much further back. `--interval 5m` is
now the default.

Pick your top-3 traded symbols. If this host has the bot's real
`signals.db` (i.e. you're on the production server), you can derive them
automatically:

```bash
python backtest/fetch_data.py --from-db --top-n 3 --months 6 --interval 5m
```

Otherwise name them explicitly:

```bash
python backtest/fetch_data.py --symbols BTC_USDT,ETH_USDT,SOL_USDT --months 6 --interval 5m
```

This takes a while (paginated 1-day windows with a 0.35s sleep between
requests to stay well under MEXC's rate limit). At 5m you can safely
widen `--chunk-minutes` (e.g. `--chunk-minutes 43200` for 30-day windows
per request) to cut the request count dramatically, since a 30-day
window is only ~8,600 bars at 5m vs. ~43,000 at 1m. It writes:

- `backtest/data/<SYMBOL>_5m.parquet` — the OHLCV history
- `backtest/data/<SYMBOL>_5m_gaps.json` — any missing-bar ranges found
  (small numbers of gaps are normal — brief exchange downtime, etc.;
  pass `--strict` to fail hard instead if you want zero tolerance)
- `backtest/data/fetch_report.json` — a summary across all symbols

**Check the gap reports before trusting the backtest.** A parquet file
with large gaps will silently understate how many signals fired in that
period. If you specifically need 1m granularity for a shorter window
(e.g. the last ~30 days), pass `--interval 1m --months 1`.

## 2. Sanity-check the plumbing before the full run

The full grid (243 combinations × 4 windows × N symbols) takes real time.
Run `--quick` first (a single parameter combination) to confirm the data
loads, windows are built correctly, and nothing crashes:

```bash
python backtest/optimize.py --symbols BTC_USDT,ETH_USDT,SOL_USDT --data-interval 5m --quick
```

`--data-interval` must match whatever `--interval` you fetched with (both
default to `5m`).

If a window logs `no param set met the >=30 train-trade minimum`, that's
not a bug — it means confluence_ok() genuinely didn't fire >=30 times in
that 6-week slice at that parameter setting. It's expected to happen
sometimes even in the full grid; those windows are excluded from the
report rather than reported on thin, overfit-prone samples.

## 3. Full walk-forward optimization (Phase 4)

```bash
python backtest/optimize.py --symbols BTC_USDT,ETH_USDT,SOL_USDT --data-interval 5m
```

This can take a long time (the SuperTrend calc has an O(n) Python loop
per parameter combination; a 6-week 5m train window is ~12k bars, times
243 combos, times up to 4 windows, times N symbols). Consider running it
under `nohup`/`tmux`/`screen` and checking back.

Outputs land in `backtest/reports/`:

- `phase4_report.md` — the full walk-forward table per symbol (train
  window/test window/chosen params/out-of-sample win rate, profit
  factor, max drawdown), the SuperTrend-only baseline comparison, the
  skipped-signal expectancy comparison (flags a window if skipped trades
  outperformed the ones actually taken — a signal the regime filter is
  too tight there), and the recommended param set per symbol.
- `<SYMBOL>_equity_curve.png` — out-of-sample equity curve, v3-filtered
  vs. SuperTrend-only baseline, stitched across all accepted test
  windows.
- `phase4_summary.json` — machine-readable version of the recommendations.

**Read `phase4_report.md` before doing anything else with it.** In
particular check:
1. Trade counts per test window (the table flags `LOW (<30 trades)` —
   treat those numbers as low-confidence).
2. Whether the v3-filtered profit factor actually beats the
   SuperTrend-only baseline. If it doesn't, the regime/channel filter
   isn't earning its keep.
3. Any ⚠️ warning that skipped signals outperformed accepted ones.

## 4. Persisting the recommended params (still not live)

Only after you've reviewed the report:

```bash
python backtest/optimize.py --symbols BTC_USDT,ETH_USDT,SOL_USDT --data-interval 5m --write-config
```

This patches the `SCALPER_V3_*` default fallbacks in `config.py` in
place, using the most-recently-selected window's params (config.py has
no per-symbol override mechanism today, so this is one shared param set
across symbols — a `config.py` schema change would be needed for true
per-symbol params). It does **not** touch `LIVE_ENABLED` or
`SCALPER_V3_ENABLED` — both stay `false` by default. To paper-trade:

```bash
# .env or environment
SCALPER_V3_ENABLED=true    # turns on the scan/outcome-tracking jobs, logs to DB + Telegram
LIVE_ENABLED=false          # stays false -- no real orders are ever placed by this codebase either way; this flag exists purely as your own go/no-go gate for whatever order-placement layer you build on top
```

Restart `mexc-bot` and watch `skipped_signals` and `signals` (filtered by
`strategy_name = 'Super Scalper v3'`) accumulate for a while before
considering `LIVE_ENABLED=true`.

## What this backtest does NOT model

- **Funding-rate and liq_estimator filters are not applied in the
  backtest.** Phase 2 only fetches OHLCV; funding-rate history isn't
  part of this data pipeline, so `backtest/engine.py` measures the
  regime/channel confluence layer in isolation. Live signals pass
  through one more gate (`scalper_v3_strategy._funding_filter_ok`) that
  the backtest can't replicate without a funding-rate history fetch.
- **Slippage and tick size are approximated** as a flat
  `--tick-size-pct`-style fraction of price (`BacktestParams.tick_pct`,
  default 2bps), not the symbol's real exchange tick size. Look up the
  real tick size from `/contract/detail` per symbol if you want tighter
  fidelity.
- **Fees default to zero** — `coin_scanner.py` already restricts the
  bot's coin pool to zero-fee USDT perpetuals, so this is the realistic
  default for symbols this bot actually trades. Pass
  `BacktestParams(taker_fee_pct=...)` if backtesting a fee-bearing pair.
- **Position sizing is 1% equity risk, no leverage multiplier baked in**
  (unlike the v1 strategy's ROI/leverage-scaled TP/SL) — pnl is modeled
  in raw price-risk terms.
