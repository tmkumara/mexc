"""
Backtest utility for Precision Pullback Scalper v1.

Two-phase simulation: an armed pending setup (from
strategy.detect_pending_setup, as-of each bar) waits for a breakout
confirmation (as strategy.check_setup_confirmation would live), then
outcome_check.check_tp_sl_with_breakeven walks the fixed single TP/SL
(with its one breakeven step) forward from the confirming bar -- the
exact same functions the live bot uses, so backtest and live share one
source of truth and no signal logic is duplicated here.

Needs both TREND_TF and ENTRY_TF historical data per symbol -- fetch both
with backtest/fetch_data.py first (arbitrary --interval supported).

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
from outcome_check import check_tp_sl_with_breakeven
from mexc_client import get_klines
from config import (
    ENTRY_TF, TREND_TF, ENTRY_KLINE_COUNT, _TF_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    EMA_TREND_LEN, EMA_TREND_SLOPE_LOOKBACK, EMA_SLOW_LEN, RSI_PERIOD,
    VOLUME_MA_PERIOD, ATR_PERIOD, BREAKEVEN_TRIGGER_PRICE_PCT, LEVERAGE,
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
            break
        seen_earliest = earliest

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
    outcome: str            # "win" | "loss" | "breakeven" | "expired"
    gross_roi_pct: float
    net_roi_pct: float
    breakeven_triggered: bool = False
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
        breakevens = [t for t in self.trades if t.outcome == "breakeven"]
        expired = [t for t in self.trades if t.outcome == "expired"]

        closed_for_rate = len(wins) + len(losses)
        win_rate = (len(wins) / closed_for_rate * 100.0) if closed_for_rate else 0.0
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
        print(f"Breakeven:           {len(breakevens)}")
        print(f"Expired trades:      {len(expired)}")
        print(f"Win rate (win/loss): {win_rate:.1f}%")
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

        breakeven_rate = sum(1 for t in self.trades if t.breakeven_triggered) / n * 100
        print(f"\nBreakeven-trigger rate: {breakeven_rate:.1f}%")

        print("\nMonthly performance:")
        by_month: dict[str, list[Trade]] = defaultdict(list)
        for t in self.trades:
            if t.closed_at:
                month_key = t.closed_at[:7]
                by_month[month_key].append(t)
        for month_key in sorted(by_month):
            _bucket_report(f"  {month_key}", by_month[month_key])


def _with_forming_row(df: pd.DataFrame, upto_idx: int, window_count: int) -> pd.DataFrame:
    """Last `window_count` rows ending at upto_idx, plus a duplicated last
    row standing in for the still-forming candle, so
    detect_pending_setup's/check_setup_confirmation's iloc[:-1] leaves
    exactly that trailing window as 'completed'."""
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start : upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state. One setup/trade at a time."""
    trades: list[Trade] = []

    df_entry_full = get_klines_extended(symbol, ENTRY_TF, days)
    df_trend_full = get_klines_extended(symbol, TREND_TF, days)

    if df_entry_full.empty or df_trend_full.empty:
        print(f"[{symbol}] no candle history returned for one or both timeframes -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_entry_full)} x {ENTRY_TF}, "
        f"{len(df_trend_full)} x {TREND_TF} bars", flush=True,
    )

    min_start = max(EMA_TREND_LEN + EMA_TREND_SLOPE_LOOKBACK, EMA_SLOW_LEN, RSI_PERIOD, VOLUME_MA_PERIOD, ATR_PERIOD) + 10

    original_get_market_klines = strategy.get_market_klines
    pending_setup: dict | None = None
    in_trade_until_idx = -1

    try:
        for i in range(min_start, len(df_entry_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of_entry = _with_forming_row(df_entry_full, i, ENTRY_KLINE_COUNT)
            ts = df_entry_full.index[i]
            as_of_trend = df_trend_full[df_trend_full.index <= ts]
            if as_of_trend.empty:
                continue
            as_of_trend = _with_forming_row(as_of_trend, len(as_of_trend) - 1, ENTRY_KLINE_COUNT)

            def _fake(sym: str, interval: str, count: int = 100, _entry=as_of_entry, _trend=as_of_trend):
                if interval == ENTRY_TF:
                    return _entry
                if interval == TREND_TF:
                    return _trend
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            if pending_setup is not None:
                status, fill_price = strategy.check_setup_confirmation(pending_setup)
                if status in ("expired", "invalidated"):
                    pending_setup = None
                    continue
                if status == "waiting":
                    continue

                # confirmed
                entry_candle_cutoff = df_entry_full.index[i]
                direction = pending_setup["direction"]
                breakeven_trigger_price = (
                    fill_price * (1 + BREAKEVEN_TRIGGER_PRICE_PCT) if direction == "LONG"
                    else fill_price * (1 - BREAKEVEN_TRIGGER_PRICE_PCT)
                )
                result = check_tp_sl_with_breakeven(
                    direction, fill_price, pending_setup["sl_price"], pending_setup["tp_price"],
                    breakeven_trigger_price, df_entry_full, entry_candle_cutoff,
                )
                bars_held = 1
                if result is None:
                    outcome = "expired"
                    gross_roi_pct = 0.0
                    breakeven_triggered = False
                    closed_at_str = str(df_entry_full.index[i])
                else:
                    outcome = result["status"]
                    gross_roi_pct = result["pnl_roi_pct"]
                    breakeven_triggered = result["breakeven_triggered_at"] is not None
                    closed_idx = df_entry_full.index.get_loc(result["closed_at"])
                    bars_held = max(1, closed_idx - i)
                    closed_at_str = str(result["closed_at"])

                gross_roi = gross_roi_pct * LEVERAGE
                cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
                net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi

                trades.append(Trade(
                    symbol=symbol, direction=direction, entry_price=fill_price,
                    tp_price=pending_setup["tp_price"], sl_price=pending_setup["sl_price"],
                    rr=pending_setup["rr"], outcome=outcome,
                    gross_roi_pct=round(gross_roi, 3), net_roi_pct=round(net_roi, 3),
                    breakeven_triggered=breakeven_triggered,
                    closed_at=closed_at_str,
                ))
                in_trade_until_idx = i + bars_held
                pending_setup = None
                continue

            setup = strategy.detect_pending_setup(symbol)
            if setup is not None:
                setup["created_at"] = df_entry_full.index[i].isoformat()
                pending_setup = setup
    finally:
        strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Precision Pullback Scalper v1")
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
