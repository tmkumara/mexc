"""
Phase 1 backtest: BTC relative-strength continuation. See
docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md
for the full design and success criteria.

Sweeps a 27-combination grid (lookback_bars x move_threshold_pct x
rs_threshold_pct) across the 8 already-fetched symbols, using the same
20x/10%-ROI-TP/10%-ROI-SL flat sizing as the current live v3 strategy (via
config.py's SCALPER_V3_TP_PRICE_PCT / SCALPER_V3_MAX_SL_PRICE_PCT / LEVERAGE),
so any edge found is attributable to the entry signal rather than a
favorable TP:SL ratio. SL-first same-bar tie-break, one position at a
time per symbol, no lookahead (signal as of a bar's close, entry at the
next bar's open), entry-bar-EXCLUSIVE TP/SL scan (the exit scan starts at
entry_idx + 1, never checking the entry bar itself) -- matches
backtest/engine.py's _simulate_trade conventions
(bars_after = computed.iloc[entry_idx + 1:], scalper_v3_strategy.walk_trade's
"strictly after the entry candle" contract).

Caveats not modeled here:
  - The 7 alt symbols all key off the same BTC return series, so trades
    across symbols are correlated, not fully independent observations --
    a large pooled trade count should not be read as N independent bets.
  - Frictionless: no fees, no slippage. A real deployment would only do
    worse than what's reported here, never better.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.relative_strength import compute_returns, rs_signal
from config import LEVERAGE, SCALPER_V3_MAX_SL_PRICE_PCT, SCALPER_V3_TP_PRICE_PCT

DATA_DIR = Path(__file__).resolve().parent / "data"

TP_PCT = SCALPER_V3_TP_PRICE_PCT  # 10% ROI target -> 0.5% price move at 20x
SL_PCT = SCALPER_V3_MAX_SL_PRICE_PCT  # 10% ROI stop   -> 0.5% price move at 20x

LOOKBACK_BARS = [12, 24, 48]          # 1h, 2h, 4h at 5m resolution
MOVE_THRESHOLDS = [0.5, 1.0, 2.0]     # BTC's own move, percent
RS_THRESHOLDS = [1.0, 2.0, 3.0]       # excess return required, percent


def load_symbol(symbol: str) -> pd.DataFrame:
    return pd.read_parquet(DATA_DIR / f"{symbol}_5m.parquet")


def simulate(
    alt_high: pd.Series,
    alt_low: pd.Series,
    alt_open: pd.Series,
    btc_returns: pd.Series,
    alt_returns: pd.Series,
    move_threshold_pct: float,
    rs_threshold_pct: float,
) -> list[dict]:
    """Walk forward bar by bar, open at most one position at a time,
    SL-first same-bar tie-break. Returns a list of closed-trade dicts."""
    n = len(alt_open)
    trades: list[dict] = []
    open_until = -1

    for i in range(n - 1):
        if i <= open_until:
            continue

        direction = rs_signal(
            btc_return=btc_returns.iloc[i],
            alt_return=alt_returns.iloc[i],
            move_threshold_pct=move_threshold_pct,
            rs_threshold_pct=rs_threshold_pct,
        )
        if direction is None:
            continue

        entry_idx = i + 1
        entry_price = float(alt_open.iloc[entry_idx])

        if direction == "LONG":
            tp_price = entry_price * (1 + TP_PCT)
            sl_price = entry_price * (1 - SL_PCT)
        else:
            tp_price = entry_price * (1 - TP_PCT)
            sl_price = entry_price * (1 + SL_PCT)

        # Entry-bar-EXCLUSIVE: the scan starts at entry_idx + 1, never checking
        # the entry bar itself. Matches engine.py's _simulate_trade, which feeds
        # walk_trade only computed.iloc[entry_idx + 1:] -- "one row per CLOSED
        # candle strictly after the entry candle" (scalper_v3_strategy.walk_trade).
        # If entry_idx is the last (or second-to-last) usable bar, this range is
        # empty and the trade falls through to the same "never resolved within
        # available data" handling used below.
        exit_price = None
        exit_reason = None
        j_final = n - 1
        for j in range(entry_idx + 1, n):
            high = float(alt_high.iloc[j])
            low = float(alt_low.iloc[j])
            if direction == "LONG":
                hit_sl = low <= sl_price
                hit_tp = high >= tp_price
            else:
                hit_sl = high >= sl_price
                hit_tp = low <= tp_price
            if hit_sl:
                exit_price, exit_reason, j_final = sl_price, "sl", j
                break
            if hit_tp:
                exit_price, exit_reason, j_final = tp_price, "tp", j
                break

        if exit_price is None:
            open_until = n  # still open at data end -- book stays flat (matches engine.py:306)
            continue

        roi_pct = (
            (exit_price - entry_price) / entry_price * LEVERAGE * 100.0
            if direction == "LONG"
            else (entry_price - exit_price) / entry_price * LEVERAGE * 100.0
        )
        trades.append({"direction": direction, "roi_pct": roi_pct, "win": exit_reason == "tp"})
        # engine.py's gate is `entry_idx > open_until` with entry_idx = i + 1, and open_until
        # is set to the exit bar's index (j_final) -- i.e. reentry allowed once i >= j_final.
        # This script's gate is `i <= open_until: continue`, so storing j_final - 1 makes it
        # block i <= j_final - 1 (i.e. i < j_final) and allow i >= j_final, matching engine.py.
        open_until = j_final - 1

    return trades


def _wr_pf(trades: list[dict]) -> tuple[int, float, float | None]:
    """(n, win_rate_pct, profit_factor) for a list of trade dicts. PF is None
    for n==0, inf when there are gains but zero losses."""
    n = len(trades)
    if n == 0:
        return 0, 0.0, None
    wins = sum(1 for t in trades if t["win"])
    wr = wins / n * 100.0
    gains = sum(t["roi_pct"] for t in trades if t["roi_pct"] > 0)
    losses = -sum(t["roi_pct"] for t in trades if t["roi_pct"] <= 0)
    pf = gains / losses if losses > 0 else float("inf")
    return n, wr, pf


def main():
    symbols = sorted(p.stem.replace("_5m", "") for p in DATA_DIR.glob("*_5m.parquet"))
    if "BTC_USDT" not in symbols:
        print("BTC_USDT data not found in backtest/data/ -- cannot compute relative strength.")
        return
    alt_symbols = [s for s in symbols if s != "BTC_USDT"]

    btc_df = load_symbol("BTC_USDT")
    alt_dfs = {s: load_symbol(s) for s in alt_symbols}

    print(f"Symbols: BTC_USDT (reference) + {alt_symbols}")
    print(
        "Caveats: all alt symbols share one BTC return series -- trades are not fully "
        "independent observations. No fees/slippage modeled -- a live result would be "
        "at least as bad as this, not better.\n"
    )

    results = []
    trades_by_combo: dict[tuple[int, float, float], list[dict]] = {}
    for lookback_bars in LOOKBACK_BARS:
        btc_returns = compute_returns(btc_df["close"], lookback_bars)
        for move_threshold_pct in MOVE_THRESHOLDS:
            for rs_threshold_pct in RS_THRESHOLDS:
                all_trades = []
                for sym, df in alt_dfs.items():
                    aligned_btc = btc_returns.reindex(df.index, method="ffill")
                    alt_returns = compute_returns(df["close"], lookback_bars)
                    trades = simulate(
                        alt_high=df["high"], alt_low=df["low"], alt_open=df["open"],
                        btc_returns=aligned_btc, alt_returns=alt_returns,
                        move_threshold_pct=move_threshold_pct, rs_threshold_pct=rs_threshold_pct,
                    )
                    for t in trades:
                        t["symbol"] = sym
                    all_trades.extend(trades)

                combo_key = (lookback_bars, move_threshold_pct, rs_threshold_pct)
                trades_by_combo[combo_key] = all_trades

                n, wr, pf = _wr_pf(all_trades)
                results.append((lookback_bars, move_threshold_pct, rs_threshold_pct, n, wr, pf))

    results.sort(key=lambda r: (r[5] is None, -(r[5] if r[5] is not None else 0)))

    print(f"{'lookback':>8}  {'move%':>6}  {'rs%':>5}  {'trades':>7}  {'WR%':>7}  {'PF':>8}")
    for lookback_bars, move_threshold_pct, rs_threshold_pct, n, wr, pf in results:
        pf_str = f"{pf:.3f}" if isinstance(pf, float) else str(pf)
        print(f"{lookback_bars:>8}  {move_threshold_pct:>6.1f}  {rs_threshold_pct:>5.1f}  {n:>7}  {wr:>6.2f}%  {pf_str:>8}")

    qualifying = [r for r in results if r[5] is not None and r[5] > 1.0 and r[3] >= 200]
    print(f"\n{len(qualifying)}/{len(results)} combinations clear the Phase 1 bar (PF>1.0, n>=200 trades).")

    if not results:
        return

    best_lookback, best_move, best_rs, best_n, best_wr, best_pf = results[0]
    best_pf_str = f"{best_pf:.3f}" if isinstance(best_pf, float) else str(best_pf)
    print(
        f"\nBest combination: lookback={best_lookback}  move%={best_move:.1f}  "
        f"rs%={best_rs:.1f}  (n={best_n}, WR={best_wr:.2f}%, PF={best_pf_str})"
    )
    best_trades = trades_by_combo[(best_lookback, best_move, best_rs)]

    print(f"\nPer-symbol breakdown for the best combination:")
    print(f"{'symbol':>12}  {'trades':>7}  {'WR%':>7}  {'PF':>8}")
    for sym in alt_symbols:
        sym_trades = [t for t in best_trades if t["symbol"] == sym]
        n, wr, pf = _wr_pf(sym_trades)
        pf_str = f"{pf:.3f}" if isinstance(pf, float) else str(pf)
        print(f"{sym:>12}  {n:>7}  {wr:>6.2f}%  {pf_str:>8}")

    print(f"\nPer-direction breakdown for the best combination:")
    print(f"{'direction':>12}  {'trades':>7}  {'WR%':>7}  {'PF':>8}")
    for direction in ("LONG", "SHORT"):
        dir_trades = [t for t in best_trades if t["direction"] == direction]
        n, wr, pf = _wr_pf(dir_trades)
        pf_str = f"{pf:.3f}" if isinstance(pf, float) else str(pf)
        print(f"{direction:>12}  {n:>7}  {wr:>6.2f}%  {pf_str:>8}")


if __name__ == "__main__":
    main()
