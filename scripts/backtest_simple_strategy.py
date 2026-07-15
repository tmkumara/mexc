"""
Backtest utility for Simple Supertrend Pullback v1.

Walks 5m candles forward in time, at each completed bar building an
"as-of" view (all 15m/5m/BTC candles up to and including that bar, plus a
duplicated last row standing in for the not-yet-formed candle) and calling
strategy.evaluate_symbol against it -- the exact same function the live
bot uses, so backtest and live share one source of truth and no signal
logic is duplicated here.

Known limitation (documented, not solved, in this first version): MEXC's
kline endpoint only accepts a candle `count`, not a start/end range, so
the achieved history length may be shorter than --days asks for. The
script reports what it actually achieved.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

import strategy
from mexc_client import get_klines
from config import (
    ENTRY_TF, TREND_TF, BTC_FILTER_SYMBOL, BTC_FILTER_TF,
    SIGNAL_EXPIRE_HOURS, CANDLE_MINUTES,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    TREND_EMA_PERIOD, ENTRY_EMA_PERIOD, PULLBACK_LOOKBACK_BARS,
)

MAX_REST_COUNT = 2000   # single-request ceiling this script asks MEXC for


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


def _with_forming_row(df: pd.DataFrame, upto_idx: int) -> pd.DataFrame:
    """Rows [0, upto_idx] plus a duplicated last row standing in for the
    still-forming candle, so evaluate_symbol's iloc[:-1] leaves exactly
    rows [0, upto_idx] as 'completed'."""
    window = df.iloc[: upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def _find_as_of_index(df: pd.DataFrame, timestamp) -> int | None:
    """Index of the last row of df with index <= timestamp, or None."""
    eligible = df.index[df.index <= timestamp]
    if len(eligible) == 0:
        return None
    return int(df.index.get_loc(eligible[-1]))


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


def backtest_symbol(symbol: str, stats: BacktestStats) -> None:
    df_15m_full = get_klines(symbol, TREND_TF, count=MAX_REST_COUNT)
    df_5m_full = get_klines(symbol, ENTRY_TF, count=MAX_REST_COUNT)
    df_btc_full = get_klines(BTC_FILTER_SYMBOL, BTC_FILTER_TF, count=MAX_REST_COUNT)

    if df_15m_full.empty or df_5m_full.empty:
        print(f"[{symbol}] no candle history returned -- skipping")
        return

    print(
        f"[{symbol}] achieved history: {len(df_15m_full)} x {TREND_TF} bars, "
        f"{len(df_5m_full)} x {ENTRY_TF} bars"
    )

    min_start = max(TREND_EMA_PERIOD + 5, ENTRY_EMA_PERIOD + PULLBACK_LOOKBACK_BARS + 15)
    in_trade_until_idx = -1

    original_get_market_klines = strategy.get_market_klines

    try:
        for i in range(min_start, len(df_5m_full) - 1):
            if i <= in_trade_until_idx:
                continue

            ts = df_5m_full.index[i]
            trend_idx = _find_as_of_index(df_15m_full, ts)
            btc_idx = _find_as_of_index(df_btc_full, ts) if not df_btc_full.empty else None
            if trend_idx is None or trend_idx < min_start:
                continue

            as_of_5m = _with_forming_row(df_5m_full, i)
            as_of_15m = _with_forming_row(df_15m_full, trend_idx)
            as_of_btc = _with_forming_row(df_btc_full, btc_idx) if btc_idx is not None else None

            def _fake(sym: str, interval: str, count: int = 100, _5m=as_of_5m, _15m=as_of_15m, _btc=as_of_btc):
                if sym == BTC_FILTER_SYMBOL and interval == BTC_FILTER_TF:
                    return _btc if _btc is not None else pd.DataFrame()
                if interval == ENTRY_TF:
                    return _5m
                if interval == TREND_TF:
                    return _15m
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            sig = strategy.evaluate_symbol(symbol)

            if sig is None:
                continue

            outcome, bars_held = _simulate_outcome(
                sig.direction, sig.entry_price, sig.tp_price, sig.sl_price, df_5m_full, i,
            )
            exit_price = sig.tp_price if outcome == "win" else (
                sig.sl_price if outcome == "loss" else float(df_5m_full["close"].iloc[min(i + bars_held, len(df_5m_full) - 1)])
            )
            gross_roi, net_roi = _roi_with_costs(sig.direction, sig.entry_price, exit_price, outcome)

            stats.add(Trade(
                symbol=symbol, direction=sig.direction, entry_price=sig.entry_price,
                tp_price=sig.tp_price, sl_price=sig.sl_price, rr=sig.rr,
                outcome=outcome, gross_roi_pct=gross_roi, net_roi_pct=net_roi,
            ))

            in_trade_until_idx = i + bars_held
    finally:
        strategy.get_market_klines = original_get_market_klines


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Simple Supertrend Pullback v1")
    parser.add_argument("--symbols", nargs="+", required=True, help="e.g. XRP_USDT DOGE_USDT")
    parser.add_argument("--days", type=int, default=30, help="requested lookback in days (best-effort, see limitation note)")
    args = parser.parse_args()

    print(f"Requested lookback: {args.days} days (best-effort -- single REST request, no pagination)")

    stats = BacktestStats()
    for symbol in args.symbols:
        backtest_symbol(symbol, stats)

    print("\n" + "=" * 60)
    stats.print_report()


if __name__ == "__main__":
    sys.exit(main() or 0)
