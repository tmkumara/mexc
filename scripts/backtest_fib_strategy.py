"""
Backtest utility for the Fibonacci Multi-Timeframe strategy.

Walks 1H candles forward in time, at each completed bar building an
"as-of" view (all 4H/1H/BTC candles up to and including that bar, plus a
duplicated last row standing in for the not-yet-formed candle) and calling
fib_strategy.evaluate_symbol_fib against it -- the exact same function a
live runtime would use, so backtest and any future live wiring share one
source of truth.

Reuses get_klines_extended's start/end pagination directly from
scripts/backtest_simple_strategy.py rather than duplicating it.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fib_strategy
import config_fib
from backtest_simple_strategy import get_klines_extended

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


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
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start: upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def _find_as_of_index(df: pd.DataFrame, timestamp) -> int | None:
    eligible = df.index[df.index <= timestamp]
    if len(eligible) == 0:
        return None
    return int(df.index.get_loc(eligible[-1]))


def _simulate_outcome(
    direction: str, entry: float, tp: float, sl: float,
    df_1h: pd.DataFrame, entry_idx: int,
) -> tuple[str, int]:
    max_bars = int(config_fib.SIGNAL_EXPIRE_HOURS)  # 1 bar == 1 hour on the 1H entry TF
    for offset in range(1, max_bars + 1):
        idx = entry_idx + offset
        if idx >= len(df_1h):
            return "expired", offset
        high = float(df_1h["high"].iloc[idx])
        low = float(df_1h["low"].iloc[idx])

        hit_sl = (low <= sl) if direction == "LONG" else (high >= sl)
        if hit_sl:
            return "loss", offset
        hit_tp = (high >= tp) if direction == "LONG" else (low <= tp)
        if hit_tp:
            return "win", offset

    return "expired", max_bars


def _roi_with_costs(direction: str, entry: float, exit_price: float, outcome: str) -> tuple[float, float]:
    if direction == "LONG":
        price_move_pct = (exit_price - entry) / entry * 100.0
    else:
        price_move_pct = (entry - exit_price) / entry * 100.0

    gross_roi = price_move_pct * config_fib.LEVERAGE
    cost_pct = (
        config_fib.ESTIMATED_ENTRY_FEE_PCT + config_fib.ESTIMATED_EXIT_FEE_PCT + config_fib.ESTIMATED_SLIPPAGE_PCT
    ) * config_fib.LEVERAGE
    net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi
    return round(gross_roi, 3), round(net_roi, 3)


def backtest_symbol(symbol: str, days: int, df_btc_full: pd.DataFrame) -> list[Trade]:
    """Runs in its own worker process -- returns this symbol's trades rather
    than mutating shared state, since process pool workers don't share
    memory."""
    trades: list[Trade] = []

    df_4h_full = get_klines_extended(symbol, config_fib.TREND_TF, days)
    df_1h_full = get_klines_extended(symbol, config_fib.ENTRY_TF, days)

    if df_4h_full.empty or df_1h_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_4h_full)} x {config_fib.TREND_TF} bars, "
        f"{len(df_1h_full)} x {config_fib.ENTRY_TF} bars",
        flush=True,
    )

    min_start = max(
        config_fib.TREND_EMA_PERIOD + 5,
        config_fib.SWING_LOOKBACK_BARS + config_fib.SWING_FRACTAL_BARS + config_fib.VOLUME_MA_PERIOD + 15,
    )
    in_trade_until_idx = -1

    original_get_market_klines = fib_strategy.get_market_klines
    candle_minutes = _TF_MINUTES[config_fib.ENTRY_TF]
    trend_tf_minutes = _TF_MINUTES[config_fib.TREND_TF]
    btc_tf_minutes = _TF_MINUTES[config_fib.BTC_FILTER_TF]

    try:
        for i in range(min_start, len(df_1h_full) - 1):
            if i <= in_trade_until_idx:
                continue

            ts = df_1h_full.index[i]
            eval_time = ts + pd.Timedelta(minutes=candle_minutes)
            trend_idx = _find_as_of_index(df_4h_full, eval_time - pd.Timedelta(minutes=trend_tf_minutes))
            btc_idx = (
                _find_as_of_index(df_btc_full, eval_time - pd.Timedelta(minutes=btc_tf_minutes))
                if not df_btc_full.empty else None
            )
            if trend_idx is None or trend_idx < config_fib.TREND_EMA_PERIOD + 5:
                continue

            as_of_1h = _with_forming_row(df_1h_full, i, config_fib.ENTRY_KLINE_COUNT)
            as_of_4h = _with_forming_row(df_4h_full, trend_idx, config_fib.TREND_KLINE_COUNT)
            as_of_btc = _with_forming_row(df_btc_full, btc_idx, config_fib.TREND_KLINE_COUNT) if btc_idx is not None else None

            def _fake(sym: str, interval: str, count: int = 100, _1h=as_of_1h, _4h=as_of_4h, _btc=as_of_btc):
                if sym == config_fib.BTC_FILTER_SYMBOL and interval == config_fib.BTC_FILTER_TF:
                    return _btc if _btc is not None else pd.DataFrame()
                if interval == config_fib.ENTRY_TF:
                    return _1h
                if interval == config_fib.TREND_TF:
                    return _4h
                return pd.DataFrame()

            fib_strategy.get_market_klines = _fake

            sig = fib_strategy.evaluate_symbol_fib(symbol)

            if sig is None:
                continue

            outcome, bars_held = _simulate_outcome(
                sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, df_1h_full, i,
            )
            exit_price = sig.tp_price if outcome == "win" else (
                sig.sl_price if outcome == "loss" else float(df_1h_full["close"].iloc[min(i + bars_held, len(df_1h_full) - 1)])
            )
            gross_roi, net_roi = _roi_with_costs(sig.direction, sig.entry_price, exit_price, outcome)

            trades.append(Trade(
                symbol=symbol, direction=sig.direction, entry_price=sig.entry_price,
                tp_price=sig.tp_price, sl_price=sig.sl_price, rr=sig.rr,
                outcome=outcome, gross_roi_pct=gross_roi, net_roi_pct=net_roi,
            ))

            in_trade_until_idx = i + bars_held
    finally:
        fib_strategy.get_market_klines = original_get_market_klines

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Fibonacci MTF Strategy")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=60, help="requested lookback in days (best-effort, paginated via start/end)")
    parser.add_argument("--workers", type=int, default=6, help="parallel worker processes, one symbol each")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- paginated via MEXC start/end)")

    print(f"[{config_fib.BTC_FILTER_SYMBOL}] fetching shared BTC context ({config_fib.BTC_FILTER_TF})...")
    df_btc_full = get_klines_extended(config_fib.BTC_FILTER_SYMBOL, config_fib.BTC_FILTER_TF, args.days)
    print(f"[{config_fib.BTC_FILTER_SYMBOL}] achieved history: {len(df_btc_full)} x {config_fib.BTC_FILTER_TF} bars")

    stats = BacktestStats()
    total_days_covered = args.days
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(backtest_symbol, symbol, args.days, df_btc_full): symbol
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
    if stats.trades:
        trades_per_day = len(stats.trades) / total_days_covered
        print(f"Average trades/day (all symbols combined): {trades_per_day:.2f}")


if __name__ == "__main__":
    sys.exit(main() or 0)
