"""
Phase 1 backtest: BTC relative-strength continuation. See
docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md
for the full design and success criteria.

Sweeps a 27-combination grid (lookback_bars x move_threshold_pct x
rs_threshold_pct) across the 8 already-fetched symbols, using the same
20x/10%-ROI-TP/10%-ROI-SL flat sizing as the current live v3 strategy, so
any edge found is attributable to the entry signal rather than a
favorable TP:SL ratio. SL-first same-bar tie-break, one position at a
time per symbol, no lookahead (signal as of a bar's close, entry at the
next bar's open) -- matches backtest/engine.py's conventions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.relative_strength import compute_returns, rs_signal

DATA_DIR = Path(__file__).resolve().parent / "data"

LEVERAGE = 20
TP_PCT = 0.10 / LEVERAGE  # 10% ROI target -> 0.5% price move
SL_PCT = 0.10 / LEVERAGE  # 10% ROI stop   -> 0.5% price move

LOOKBACK_BARS = [12, 24, 48]          # 1h, 2h, 4h at 5m resolution
MOVE_THRESHOLDS = [0.5, 1.0, 2.0]     # BTC's own move, percent
RS_THRESHOLDS = [1.0, 2.0, 3.0]       # excess return required, percent


def load_symbol(symbol: str) -> pd.DataFrame:
    return pd.read_parquet(DATA_DIR / f"{symbol}_5m.parquet")


def simulate(
    alt_close: pd.Series,
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
    n = len(alt_close)
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

        exit_price = None
        exit_reason = None
        j_final = n - 1
        for j in range(entry_idx, n):
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
            open_until = i  # never resolved within available data
            continue

        roi_pct = (
            (exit_price - entry_price) / entry_price * LEVERAGE * 100.0
            if direction == "LONG"
            else (entry_price - exit_price) / entry_price * LEVERAGE * 100.0
        )
        trades.append({"direction": direction, "roi_pct": roi_pct, "win": exit_reason == "tp"})
        open_until = j_final

    return trades


def main():
    symbols = sorted(p.stem.replace("_5m", "") for p in DATA_DIR.glob("*_5m.parquet"))
    if "BTC_USDT" not in symbols:
        print("BTC_USDT data not found in backtest/data/ -- cannot compute relative strength.")
        return
    alt_symbols = [s for s in symbols if s != "BTC_USDT"]

    btc_df = load_symbol("BTC_USDT")
    alt_dfs = {s: load_symbol(s) for s in alt_symbols}

    print(f"Symbols: BTC_USDT (reference) + {alt_symbols}\n")

    results = []
    for lookback_bars in LOOKBACK_BARS:
        btc_returns = compute_returns(btc_df["close"], lookback_bars)
        for move_threshold_pct in MOVE_THRESHOLDS:
            for rs_threshold_pct in RS_THRESHOLDS:
                all_trades = []
                for sym, df in alt_dfs.items():
                    aligned_btc = btc_returns.reindex(df.index, method="ffill")
                    alt_returns = compute_returns(df["close"], lookback_bars)
                    trades = simulate(
                        alt_close=df["close"], alt_high=df["high"], alt_low=df["low"], alt_open=df["open"],
                        btc_returns=aligned_btc, alt_returns=alt_returns,
                        move_threshold_pct=move_threshold_pct, rs_threshold_pct=rs_threshold_pct,
                    )
                    all_trades.extend(trades)

                n = len(all_trades)
                if n == 0:
                    results.append((lookback_bars, move_threshold_pct, rs_threshold_pct, 0, 0.0, None))
                    continue
                wins = sum(1 for t in all_trades if t["win"])
                wr = wins / n * 100.0
                gains = sum(t["roi_pct"] for t in all_trades if t["roi_pct"] > 0)
                losses = -sum(t["roi_pct"] for t in all_trades if t["roi_pct"] <= 0)
                pf = gains / losses if losses > 0 else float("inf")
                results.append((lookback_bars, move_threshold_pct, rs_threshold_pct, n, wr, pf))

    results.sort(key=lambda r: (r[5] is None, -(r[5] if r[5] is not None else 0)))

    print(f"{'lookback':>8}  {'move%':>6}  {'rs%':>5}  {'trades':>7}  {'WR%':>7}  {'PF':>8}")
    for lookback_bars, move_threshold_pct, rs_threshold_pct, n, wr, pf in results:
        pf_str = f"{pf:.3f}" if isinstance(pf, float) else str(pf)
        print(f"{lookback_bars:>8}  {move_threshold_pct:>6.1f}  {rs_threshold_pct:>5.1f}  {n:>7}  {wr:>6.2f}%  {pf_str:>8}")

    qualifying = [r for r in results if r[5] is not None and r[5] > 1.0 and r[3] >= 200]
    print(f"\n{len(qualifying)}/{len(results)} combinations clear the Phase 1 bar (PF>1.0, n>=200 trades).")


if __name__ == "__main__":
    main()
