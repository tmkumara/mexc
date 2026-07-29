"""
Backtest utility for Ribbon-Flip Trend-Bar Confirmation v1.

Walks 5m candles forward in time, at each completed bar building an
"as-of" view (all 15m/5m/BTC candles up to and including that bar, plus a
duplicated last row standing in for the not-yet-formed candle) and calling
strategy.evaluate_symbol against it -- the exact same function the live
bot uses, so backtest and live share one source of truth and no signal
logic is duplicated here.

History beyond a single REST request's cap (MAX_REST_COUNT) is assembled
by paging backward via `end` cursors (see get_klines_extended). The
exchange may still run out of older data before --days is satisfied; the
script reports what it actually achieved.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy
from mexc_client import get_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    RIBBON_BASELINE_LEN, RIBBON_LOOKBACK_BARS,
)

MAX_REST_COUNT = 2000   # single-request ceiling this script asks MEXC for


def get_klines_extended(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Page backward past MEXC's single-request ceiling via `end` cursors to
    assemble up to `days` of history. Stops early if the exchange runs out
    of older data (returns fewer, or a short final page)."""
    tf_minutes = _TF_MINUTES.get(interval, 5)
    target_start = datetime.utcnow() - timedelta(days=days)

    chunks: list[pd.DataFrame] = []
    cursor_end: datetime | None = None
    seen_earliest: datetime | None = None

    while True:
        end_param = int(cursor_end.timestamp()) if cursor_end is not None else None
        df = get_klines(symbol, interval, count=MAX_REST_COUNT, end=end_param)
        if df.empty:
            break

        chunks.append(df)
        earliest = df.index[0].to_pydatetime()
        if seen_earliest is not None and earliest >= seen_earliest:
            break  # exchange stopped returning older data -- avoid looping forever
        seen_earliest = earliest

        # Note: do NOT treat `len(df) < MAX_REST_COUNT` as "exchange ran out
        # of history" -- MEXC does not reliably return the full requested
        # count even mid-history (observed a first-page request return 1999
        # instead of 2000), which previously caused premature termination
        # after a single page. Only stop on reaching the target date or on
        # the exchange genuinely failing to page further back (checks above).
        if earliest <= target_start:
            break

        cursor_end = earliest - timedelta(minutes=tf_minutes)
        time.sleep(0.25)

    if not chunks:
        return pd.DataFrame()

    combined = pd.concat(chunks)
    combined = combined[~combined.index.duplicated(keep="first")]
    combined.sort_index(inplace=True)
    return combined[combined.index >= target_start]


@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price: float
    tp_price: float
    sl_price: float
    rr: float
    outcome: str            # "win" | "loss" | "expired"
    gross_roi_pct: float
    net_roi_pct: float


@dataclass
class BacktestStats:
    trades: list[Trade] = field(default_factory=list)

    def add(self, trade: Trade) -> None:
        self.trades.append(trade)

    def print_report(self) -> None:
        n = len(self.trades)
        print(f"Total trades:        {n}")
        if n == 0:
            print("No trades generated -- nothing further to report.")
            return

        wins = [t for t in self.trades if t.outcome == "win"]
        losses = [t for t in self.trades if t.outcome == "loss"]
        expired = [t for t in self.trades if t.outcome == "expired"]

        win_rate = len(wins) / n * 100.0
        gross_roi = sum(t.gross_roi_pct for t in self.trades)
        total_fees = sum(t.gross_roi_pct - t.net_roi_pct for t in self.trades)
        net_roi = sum(t.net_roi_pct for t in self.trades)
        avg_roi = net_roi / n

        consecutive = max_consecutive = 0
        running = peak = 0.0
        max_drawdown = 0.0
        for t in self.trades:
            if t.outcome == "loss":
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
            else:
                consecutive = 0
            running += t.net_roi_pct
            peak = max(peak, running)
            max_drawdown = min(max_drawdown, running - peak)

        avg_rr = sum(t.rr for t in self.trades) / n

        longs = [t for t in self.trades if t.direction == "LONG"]
        shorts = [t for t in self.trades if t.direction == "SHORT"]

        print(f"Wins:                {len(wins)}")
        print(f"Losses:              {len(losses)}")
        print(f"Expired trades:      {len(expired)}")
        print(f"Win rate:            {win_rate:.1f}%")
        print(f"Gross ROI:           {gross_roi:+.1f}%")
        print(f"Estimated fees:      {total_fees:.1f}%")
        print(f"Net ROI:             {net_roi:+.1f}%")
        print(f"Average ROI/trade:   {avg_roi:+.2f}%")
        print(f"Max consecutive losses: {max_consecutive}")
        print(f"Max drawdown:        {max_drawdown:.1f}%")
        print(f"Average RR:          {avg_rr:.2f}")

        def _bucket_report(label: str, bucket: list[Trade]) -> None:
            if not bucket:
                print(f"{label} performance:  no trades")
                return
            bwins = sum(1 for t in bucket if t.outcome == "win")
            print(
                f"{label} performance:  {len(bucket)} trades, "
                f"{bwins}/{len(bucket)} wins ({bwins / len(bucket) * 100:.1f}%), "
                f"net ROI {sum(t.net_roi_pct for t in bucket):+.1f}%"
            )

        _bucket_report("LONG", longs)
        _bucket_report("SHORT", shorts)

        print("\nPerformance by symbol:")
        for symbol in sorted({t.symbol for t in self.trades}):
            _bucket_report(f"  {symbol}", [t for t in self.trades if t.symbol == symbol])


def _with_forming_row(df: pd.DataFrame, upto_idx: int, window_count: int) -> pd.DataFrame:
    """Last `window_count` rows ending at upto_idx, plus a duplicated last
    row standing in for the still-forming candle, so evaluate_symbol's
    iloc[:-1] leaves exactly that trailing window as 'completed'.

    Bounded to `window_count` (ENTRY_KLINE_COUNT) to
    match what the live bot's get_market_klines(count=...) actually
    fetches -- an unbounded from-the-start slice would both diverge from
    live behavior and make indicator recomputation cost grow O(n^2) with
    history length."""
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start : upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def _simulate_outcome(
    direction: str, entry: float, tp: float, sl: float,
    df_5m: pd.DataFrame, entry_idx: int,
) -> tuple[str, int]:
    """Walk forward from entry_idx+1, SL-first same-candle tie-break,
    expiring after SIGNAL_EXPIRE_HOURS worth of 5m bars. Returns
    (outcome, bars_held)."""
    max_bars = int(SIGNAL_EXPIRE_HOURS * 60 / CANDLE_MINUTES)
    for offset in range(1, max_bars + 1):
        idx = entry_idx + offset
        if idx >= len(df_5m):
            return "expired", offset
        high = float(df_5m["high"].iloc[idx])
        low = float(df_5m["low"].iloc[idx])

        hit_sl = (low <= sl) if direction == "LONG" else (high >= sl)
        if hit_sl:
            return "loss", offset
        hit_tp = (high >= tp) if direction == "LONG" else (low <= tp)
        if hit_tp:
            return "win", offset

    return "expired", max_bars


def _roi_with_costs(direction: str, entry: float, exit_price: float, outcome: str) -> tuple[float, float]:
    from config import LEVERAGE

    if direction == "LONG":
        price_move_pct = (exit_price - entry) / entry * 100.0
    else:
        price_move_pct = (entry - exit_price) / entry * 100.0

    gross_roi = price_move_pct * LEVERAGE
    cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
    net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi
    return round(gross_roi, 3), round(net_roi, 3)


def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state, since process pool workers
    don't share memory."""
    trades: list[Trade] = []

    df_full = get_klines_extended(symbol, ENTRY_TF, days)

    if df_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(f"[{symbol}] achieved history: {len(df_full)} x {ENTRY_TF} bars", flush=True)

    min_start = RIBBON_BASELINE_LEN + RIBBON_LOOKBACK_BARS + 10
    in_trade_until_idx = -1

    original_get_market_klines = strategy.get_market_klines

    try:
        for i in range(min_start, len(df_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of = _with_forming_row(df_full, i, ENTRY_KLINE_COUNT)

            def _fake(sym: str, interval: str, count: int = 100, _df=as_of):
                if interval == ENTRY_TF:
                    return _df
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            sig = strategy.evaluate_symbol(symbol)

            if sig is None:
                continue

            outcome, bars_held = _simulate_outcome(
                sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, df_full, i,
            )
            exit_price = sig.tp_price if outcome == "win" else (
                sig.sl_price if outcome == "loss" else float(df_full["close"].iloc[min(i + bars_held, len(df_full) - 1)])
            )
            gross_roi, net_roi = _roi_with_costs(sig.direction, sig.entry_price, exit_price, outcome)

            trades.append(Trade(
                symbol=symbol, direction=sig.direction, entry_price=sig.entry_price,
                tp_price=sig.tp_price, sl_price=sig.sl_price, rr=sig.rr,
                outcome=outcome, gross_roi_pct=gross_roi, net_roi_pct=net_roi,
            ))

            in_trade_until_idx = i + bars_held
    finally:
        strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Ribbon-Flip Trend-Bar Confirmation v1")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=30, help="requested lookback in days (best-effort, paginated via start/end)")
    parser.add_argument("--workers", type=int, default=6, help="parallel worker processes, one symbol each")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- paginated via MEXC start/end)")

    stats = BacktestStats()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(backtest_symbol, symbol, args.days): symbol
            for symbol in args.symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                for trade in future.result():
                    stats.add(trade)
            except Exception as e:
                print(f"[{symbol}] FAILED: {e}", flush=True)

    print("\n" + "=" * 60)
    stats.print_report()


if __name__ == "__main__":
    sys.exit(main() or 0)
