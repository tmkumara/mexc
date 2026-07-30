"""
Backtest utility for Binocular Pending-Breakout v1.

Two-phase simulation: an armed pending setup (from
strategy.detect_pending_setup, as-of each bar) waits for a breakout
confirmation (as strategy.check_setup_confirmation would live), then
outcome_check.check_target_ladder walks the 3-target partial-exit ladder
forward from the confirming bar -- the exact same functions the live bot
uses, so backtest and live share one source of truth and no signal logic
is duplicated here.

History beyond a single REST request's cap (MAX_REST_COUNT) is assembled
by paging backward via `end` cursors (see get_klines_extended). The
exchange may still run out of older data before --days is satisfied; the
script reports what it actually achieved.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy
from outcome_check import check_target_ladder
from mexc_client import get_klines
from config import (
    ENTRY_TF, ENTRY_KLINE_COUNT, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD,
    RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH,
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
    final_stage: int = 0
    t1_hit: bool = False
    t2_hit: bool = False
    t3_hit: bool = False
    closed_at: str = ""


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

        t1_rate = sum(1 for t in self.trades if t.t1_hit) / n * 100
        t2_rate = sum(1 for t in self.trades if t.t2_hit) / n * 100
        t3_rate = sum(1 for t in self.trades if t.t3_hit) / n * 100
        print(f"\nT1 hit rate:          {t1_rate:.1f}%")
        print(f"T2 hit rate:          {t2_rate:.1f}%")
        print(f"T3 hit rate:          {t3_rate:.1f}%")

        print("\nMonthly performance:")
        by_month: dict[str, list[Trade]] = defaultdict(list)
        for t in self.trades:
            if t.closed_at:
                month_key = t.closed_at[:7]  # "YYYY-MM"
                by_month[month_key].append(t)
        for month_key in sorted(by_month):
            _bucket_report(f"  {month_key}", by_month[month_key])


def _with_forming_row(df: pd.DataFrame, upto_idx: int, window_count: int) -> pd.DataFrame:
    """Last `window_count` rows ending at upto_idx, plus a duplicated last
    row standing in for the still-forming candle, so
    detect_pending_setup's/check_setup_confirmation's iloc[:-1] leaves
    exactly that trailing window as 'completed'.

    Bounded to `window_count` (ENTRY_KLINE_COUNT) to
    match what the live bot's get_market_klines(count=...) actually
    fetches -- an unbounded from-the-start slice would both diverge from
    live behavior and make indicator recomputation cost grow O(n^2) with
    history length."""
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start : upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state, since process pool workers
    don't share memory. Two-phase simulation: an armed pending setup
    waits for a breakout confirmation, then check_target_ladder walks the
    3-target partial-exit ladder forward from the confirming bar. One
    setup/trade at a time, same as the single-timeframe walk-forward this
    script has always used."""
    trades: list[Trade] = []

    df_full = get_klines_extended(symbol, ENTRY_TF, days)

    if df_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(f"[{symbol}] achieved history: {len(df_full)} x {ENTRY_TF} bars", flush=True)

    min_start = max(
        RIBBON_BASELINE_LEN, BINOCULAR_EMA200_LEN, CHANDELIER_ATR_PERIOD,
        RSI_SLOW_PERIOD, PVT_SIGNAL_LENGTH,
    ) + 10

    original_get_market_klines = strategy.get_market_klines
    pending_setup: dict | None = None
    in_trade_until_idx = -1

    from config import SIGNAL_MODE, CONFIRMATION_TIMEFRAMES
    confirmation_dfs: dict[str, pd.DataFrame] = {}
    if SIGNAL_MODE == "strict":
        for tf in [t.strip() for t in CONFIRMATION_TIMEFRAMES.split(",") if t.strip()]:
            confirmation_dfs[tf] = get_klines_extended(symbol, tf, days)

    try:
        for i in range(min_start, len(df_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of = _with_forming_row(df_full, i, ENTRY_KLINE_COUNT)

            def _fake(sym: str, interval: str, count: int = 100, _df=as_of, _ts=df_full.index[i]):
                if interval == ENTRY_TF:
                    return _df
                if interval in confirmation_dfs and not confirmation_dfs[interval].empty:
                    tf_df = confirmation_dfs[interval]
                    as_of_tf = tf_df[tf_df.index <= _ts]
                    if as_of_tf.empty:
                        return pd.DataFrame()
                    return pd.concat([as_of_tf, as_of_tf.iloc[[-1]]])
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            if pending_setup is not None:
                status, fill_price = strategy.check_setup_confirmation(pending_setup)
                if status == "expired" or status == "invalidated":
                    pending_setup = None
                    continue
                if status == "waiting":
                    continue

                # confirmed
                entry_candle_cutoff = df_full.index[i]
                result = check_target_ladder(
                    pending_setup["direction"], fill_price, pending_setup["sl_price"],
                    pending_setup["tp_price"], pending_setup["tp2_price"], pending_setup["tp3_price"],
                    df_full, entry_candle_cutoff,
                )
                bars_held = 1
                if result is None:
                    # Ran off the end of available history -- treat as expired.
                    outcome, final_stage = "expired", 0
                    gross_roi_pct = 0.0
                    closed_at_str = str(df_full.index[i])
                else:
                    outcome = result["status"]
                    final_stage = result["final_stage"]
                    gross_roi_pct = result["pnl_roi_pct"]
                    closed_idx = df_full.index.get_loc(result["closed_at"])
                    bars_held = max(1, closed_idx - i)
                    closed_at_str = str(result["closed_at"])

                from config import LEVERAGE
                gross_roi = gross_roi_pct * LEVERAGE
                cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
                net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi

                trades.append(Trade(
                    symbol=symbol, direction=pending_setup["direction"], entry_price=fill_price,
                    tp_price=pending_setup["tp_price"], sl_price=pending_setup["sl_price"],
                    rr=pending_setup["rr"], outcome=outcome,
                    gross_roi_pct=round(gross_roi, 3), net_roi_pct=round(net_roi, 3),
                    final_stage=final_stage,
                    t1_hit=final_stage >= 1, t2_hit=final_stage >= 2, t3_hit=final_stage >= 3,
                    closed_at=closed_at_str,
                ))
                in_trade_until_idx = i + bars_held
                pending_setup = None
                continue

            setup = strategy.detect_pending_setup(symbol)
            if setup is not None:
                setup["created_at"] = df_full.index[i].isoformat()
                pending_setup = setup
    finally:
        strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Binocular Pending-Breakout v1")
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
