"""
Backtest utility for Zero-Lag MTF Pullback v1.

Three-state simulation, mirroring the live pipeline exactly: a
pending_pullback setup (from strategy.detect_pending_setup, as-of each
bar) advances to pending_breakout (5m ZLEMA crossover + confirmation
candle, via strategy.check_setup_confirmation) then to a fired trade
(price breaks the trigger level) -- the exact same functions the live bot
uses, so backtest and live share one source of truth and no signal logic
is duplicated here.

Needs all four timeframes' historical data per symbol -- fetch with
backtest/fetch_data.py first (arbitrary --interval supported).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy
from outcome_check import check_tp_sl
from mexc_client import get_klines
from config import (
    MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF, _TF_MINUTES,
    MACRO_KLINE_COUNT, TREND_KLINE_COUNT, PULLBACK_KLINE_COUNT, ENTRY_KLINE_COUNT,
    ESTIMATED_ENTRY_FEE_PCT, ESTIMATED_EXIT_FEE_PCT, ESTIMATED_SLIPPAGE_PCT,
    ZERO_LAG_LENGTH, ZERO_LAG_BAND_LOOKBACK, LEVERAGE, TP_ROI_PCT, SL_ROI_PCT,
)

MAX_REST_COUNT = 2000   # single-request ceiling this script asks MEXC for


class _SimulatedDatetime(datetime):
    """Stand-in for strategy.datetime during backtesting. check_setup_confirmation
    computes age_minutes = (datetime.now(timezone.utc) - setup_time) --
    with a historical setup_time and the REAL wall clock, that's always
    enormous, so every pending setup would be marked expired after just
    one simulated bar instead of PENDING_EXPIRY_CANDLES. Overriding only
    now() to return the current as-of bar's timestamp fixes this;
    fromisoformat/utcnow/etc. are inherited unchanged (utcnow() staying on
    the real wall clock is fine -- detect_pending_setup's settle-age check
    just needs 'not suspiciously fresh', which real-now vs.
    historical-candle-time always satisfies)."""
    _sim_now: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        if cls._sim_now is None:
            return super().now(tz)
        return cls._sim_now if tz is None else cls._sim_now.astimezone(tz)


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
    outcome: str            # "win" | "loss" | "expired"
    gross_roi_pct: float
    net_roi_pct: float
    closed_at: str = ""
    score: float = 0.0
    entry_time: str = ""
    hold_minutes: float = 0.0


@dataclass
class BacktestStats:
    trades: list[Trade] = field(default_factory=list)

    def add(self, trade: Trade) -> None:
        self.trades.append(trade)

    def compute_metrics(self) -> dict:
        n = len(self.trades)
        if n == 0:
            return {"total_trades": 0}

        wins = [t for t in self.trades if t.outcome == "win"]
        losses = [t for t in self.trades if t.outcome == "loss"]
        expired = [t for t in self.trades if t.outcome == "expired"]
        closed_for_rate = len(wins) + len(losses)

        gross_wins = sum(t.net_roi_pct for t in wins)
        gross_losses = abs(sum(t.net_roi_pct for t in losses))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 1e-9 else None

        avg_winner = (gross_wins / len(wins)) if wins else 0.0
        avg_loser = (-gross_losses / len(losses)) if losses else 0.0

        net_roi = sum(t.net_roi_pct for t in self.trades)
        expectancy_pct = net_roi / n

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

        def _bucket_metrics(bucket: list[Trade]) -> dict:
            bwins = [t for t in bucket if t.outcome == "win"]
            blosses = [t for t in bucket if t.outcome == "loss"]
            bclosed = len(bwins) + len(blosses)
            return {
                "trades": len(bucket),
                "win_rate": (len(bwins) / bclosed * 100.0) if bclosed else 0.0,
                "expectancy_pct": (sum(t.net_roi_pct for t in bucket) / len(bucket)) if bucket else 0.0,
                "net_roi_pct": sum(t.net_roi_pct for t in bucket),
            }

        longs = [t for t in self.trades if t.direction == "LONG"]
        shorts = [t for t in self.trades if t.direction == "SHORT"]

        score_buckets: dict[str, dict] = {}
        for label, lo, hi in [("80-84.9", 80, 85), ("85-89.9", 85, 90), ("90-100", 90, 100.001)]:
            bucket = [t for t in self.trades if lo <= t.score < hi]
            score_buckets[label] = _bucket_metrics(bucket)

        hour_buckets: dict[int, dict] = {}
        for hour in range(24):
            bucket = [
                t for t in self.trades
                if t.entry_time and int(t.entry_time[11:13]) == hour
            ]
            if bucket:
                hour_buckets[hour] = _bucket_metrics(bucket)

        hold_minutes_sorted = sorted(t.hold_minutes for t in self.trades if t.outcome != "expired")
        if hold_minutes_sorted:
            mid = len(hold_minutes_sorted) // 2
            if len(hold_minutes_sorted) % 2:
                median_hold = hold_minutes_sorted[mid]
            else:
                median_hold = (hold_minutes_sorted[mid - 1] + hold_minutes_sorted[mid]) / 2
        else:
            median_hold = 0.0

        return {
            "total_trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "expired": len(expired),
            "win_rate": (len(wins) / closed_for_rate * 100.0) if closed_for_rate else 0.0,
            "profit_factor": profit_factor,
            "avg_winner_pct": avg_winner,
            "avg_loser_pct": avg_loser,
            "expectancy_pct": expectancy_pct,
            "net_roi_pct": net_roi,
            "max_drawdown_pct": max_drawdown,
            "max_consecutive_losses": max_consecutive,
            "long_expectancy_pct": _bucket_metrics(longs)["expectancy_pct"],
            "short_expectancy_pct": _bucket_metrics(shorts)["expectancy_pct"],
            "long": _bucket_metrics(longs),
            "short": _bucket_metrics(shorts),
            "score_buckets": score_buckets,
            "hour_buckets": hour_buckets,
            "avg_hold_minutes": (sum(hold_minutes_sorted) / len(hold_minutes_sorted)) if hold_minutes_sorted else 0.0,
            "median_hold_minutes": median_hold,
        }

    def _bucket_for_symbol(self, symbol: str) -> dict:
        bucket = [t for t in self.trades if t.symbol == symbol]
        bwins = [t for t in bucket if t.outcome == "win"]
        blosses = [t for t in bucket if t.outcome == "loss"]
        bclosed = len(bwins) + len(blosses)
        return {
            "trades": len(bucket),
            "win_rate": (len(bwins) / bclosed * 100.0) if bclosed else 0.0,
            "net_roi_pct": sum(t.net_roi_pct for t in bucket),
        }

    def print_report(self) -> None:
        m = self.compute_metrics()
        n = m["total_trades"]
        print(f"Total trades:        {n}")
        if n == 0:
            print("No trades generated -- nothing further to report.")
            return

        pf_str = f"{m['profit_factor']:.3f}" if m["profit_factor"] is not None else "inf (no losses)"
        print(f"Wins:                {m['wins']}")
        print(f"Losses:              {m['losses']}")
        print(f"Expired trades:      {m['expired']}")
        print(f"Win rate (win/loss): {m['win_rate']:.1f}%")
        print(f"Profit factor:       {pf_str}")
        print(f"Average winner:      {m['avg_winner_pct']:+.2f}%")
        print(f"Average loser:       {m['avg_loser_pct']:+.2f}%")
        print(f"Expectancy/trade:    {m['expectancy_pct']:+.2f}%")
        print(f"Net ROI:             {m['net_roi_pct']:+.1f}%")
        print(f"Max drawdown:        {m['max_drawdown_pct']:.1f}%")
        print(f"Max consecutive losses: {m['max_consecutive_losses']}")
        print(f"Average hold:        {m['avg_hold_minutes']:.1f} min (median {m['median_hold_minutes']:.1f} min)")

        print(f"\nLONG performance:  {m['long']['trades']} trades, WR {m['long']['win_rate']:.1f}%, "
              f"expectancy {m['long']['expectancy_pct']:+.2f}%, net ROI {m['long']['net_roi_pct']:+.1f}%")
        print(f"SHORT performance: {m['short']['trades']} trades, WR {m['short']['win_rate']:.1f}%, "
              f"expectancy {m['short']['expectancy_pct']:+.2f}%, net ROI {m['short']['net_roi_pct']:+.1f}%")

        print("\nBy score range:")
        for label, bucket in m["score_buckets"].items():
            print(f"  {label}: {bucket['trades']} trades, WR {bucket['win_rate']:.1f}%, "
                  f"expectancy {bucket['expectancy_pct']:+.2f}%")

        print("\nBy entry hour (UTC):")
        for hour in sorted(m["hour_buckets"]):
            bucket = m["hour_buckets"][hour]
            print(f"  {hour:02d}:00: {bucket['trades']} trades, WR {bucket['win_rate']:.1f}%, "
                  f"expectancy {bucket['expectancy_pct']:+.2f}%")

        print("\nPerformance by symbol:")
        for symbol in sorted({t.symbol for t in self.trades}):
            b = self._bucket_for_symbol(symbol)
            print(f"  {symbol}: {b['trades']} trades, WR {b['win_rate']:.1f}%, net ROI {b['net_roi_pct']:+.1f}%")

        print("\nMonthly performance:")
        by_month: dict[str, list[Trade]] = defaultdict(list)
        for t in self.trades:
            if t.closed_at:
                by_month[t.closed_at[:7]].append(t)
        for month_key in sorted(by_month):
            bucket = by_month[month_key]
            bwins = sum(1 for t in bucket if t.outcome == "win")
            print(f"  {month_key}: {len(bucket)} trades, {bwins}/{len(bucket)} wins, "
                  f"net ROI {sum(t.net_roi_pct for t in bucket):+.1f}%")


def _with_forming_row(df: pd.DataFrame, upto_idx: int, window_count: int) -> pd.DataFrame:
    """Last `window_count` rows ending at upto_idx, plus a duplicated last
    row standing in for the still-forming candle, so
    detect_pending_setup's/check_setup_confirmation's iloc[:-1] leaves
    exactly that trailing window as 'completed'."""
    start = max(0, upto_idx + 1 - window_count)
    window = df.iloc[start : upto_idx + 1]
    return pd.concat([window, window.iloc[[-1]]])


def _as_of_higher_tf(
    df_full: pd.DataFrame, ts, entry_tf_minutes: int, tf_minutes: int, window_count: int,
) -> pd.DataFrame:
    """As-of view of a higher timeframe, filtered by CLOSE time relative to
    the entry bar's own close time (ts + entry_tf_minutes) -- a candle on
    df_full is only 'closed' by then if its own open + tf_minutes <= that
    close time. Filtering by open time (`index <= ts`) would leak a
    still-forming higher-timeframe candle's real historical close/high/low
    into the strategy on most bars -- this is the exact lookahead bug
    fixed in commit 7296b5c for the two-timeframe case, generalized here
    to three higher timeframes."""
    cutoff = ts + timedelta(minutes=entry_tf_minutes) - timedelta(minutes=tf_minutes)
    as_of = df_full[df_full.index <= cutoff]
    if as_of.empty:
        return as_of
    return _with_forming_row(as_of, len(as_of) - 1, window_count)


def backtest_symbol(symbol: str, days: int) -> list[Trade]:
    """Runs in its own worker process (see main()) -- returns this symbol's
    trades rather than mutating shared state. One setup/trade at a time."""
    trades: list[Trade] = []

    df_entry_full = get_klines_extended(symbol, ENTRY_TF, days)
    df_pullback_full = get_klines_extended(symbol, PULLBACK_TF, days)
    df_trend_full = get_klines_extended(symbol, TREND_TF, days)
    df_macro_full = get_klines_extended(symbol, MACRO_TF, days)

    if any(d.empty for d in (df_entry_full, df_pullback_full, df_trend_full, df_macro_full)):
        print(f"[{symbol}] no candle history returned for one or more timeframes -- skipping", flush=True)
        return trades

    print(
        f"[{symbol}] achieved history: {len(df_entry_full)}x{ENTRY_TF}, {len(df_pullback_full)}x{PULLBACK_TF}, "
        f"{len(df_trend_full)}x{TREND_TF}, {len(df_macro_full)}x{MACRO_TF} bars", flush=True,
    )

    min_start = ZERO_LAG_LENGTH + ZERO_LAG_BAND_LOOKBACK + 10

    original_get_market_klines = strategy.get_market_klines
    original_datetime = strategy.datetime
    entry_tf_minutes = _TF_MINUTES.get(ENTRY_TF, 5)
    pullback_tf_minutes = _TF_MINUTES.get(PULLBACK_TF, 15)
    trend_tf_minutes = _TF_MINUTES.get(TREND_TF, 60)
    macro_tf_minutes = _TF_MINUTES.get(MACRO_TF, 240)

    pending_setup: dict | None = None
    in_trade_until_idx = -1

    try:
        for i in range(min_start, len(df_entry_full) - 1):
            if i <= in_trade_until_idx:
                continue

            as_of_entry = _with_forming_row(df_entry_full, i, ENTRY_KLINE_COUNT)
            ts = df_entry_full.index[i]

            as_of_pullback = _as_of_higher_tf(df_pullback_full, ts, entry_tf_minutes, pullback_tf_minutes, PULLBACK_KLINE_COUNT)
            as_of_trend = _as_of_higher_tf(df_trend_full, ts, entry_tf_minutes, trend_tf_minutes, TREND_KLINE_COUNT)
            as_of_macro = _as_of_higher_tf(df_macro_full, ts, entry_tf_minutes, macro_tf_minutes, MACRO_KLINE_COUNT)

            if as_of_pullback.empty or as_of_trend.empty or as_of_macro.empty:
                continue

            def _fake(sym, interval, count=100,
                      _entry=as_of_entry, _pullback=as_of_pullback, _trend=as_of_trend, _macro=as_of_macro):
                if interval == ENTRY_TF:
                    return _entry
                if interval == PULLBACK_TF:
                    return _pullback
                if interval == TREND_TF:
                    return _trend
                if interval == MACRO_TF:
                    return _macro
                return pd.DataFrame()

            strategy.get_market_klines = _fake

            bar_now = ts.to_pydatetime()
            if bar_now.tzinfo is None:
                bar_now = bar_now.replace(tzinfo=timezone.utc)
            _SimulatedDatetime._sim_now = bar_now
            strategy.datetime = _SimulatedDatetime

            if pending_setup is not None:
                status, fill_price, extra = strategy.check_setup_confirmation(pending_setup)

                if status in ("expired", "missed"):
                    pending_setup = None
                    continue
                if status == "waiting":
                    continue
                if status == "armed_breakout":
                    pending_setup.update(extra)
                    pending_setup["status"] = "pending_breakout"
                    continue

                # confirmed
                direction = pending_setup["direction"]
                entry_score = float(extra["score"])
                entry_time_str = df_entry_full.index[i].isoformat()
                tp_price, sl_price = strategy.build_trade_prices(direction, fill_price)
                entry_candle_cutoff = df_entry_full.index[i]

                result = check_tp_sl(direction, fill_price, sl_price, tp_price, df_entry_full, entry_candle_cutoff)
                bars_held = 1
                if result is None:
                    outcome = "expired"
                    gross_roi_pct = 0.0
                    closed_at_str = str(df_entry_full.index[i])
                else:
                    outcome = result["status"]
                    gross_roi_pct = result["pnl_roi_pct"]
                    closed_idx = df_entry_full.index.get_loc(result["closed_at"])
                    bars_held = max(1, closed_idx - i)
                    closed_at_str = str(result["closed_at"])

                gross_roi = gross_roi_pct * LEVERAGE
                cost_pct = (ESTIMATED_ENTRY_FEE_PCT + ESTIMATED_EXIT_FEE_PCT + ESTIMATED_SLIPPAGE_PCT) * LEVERAGE
                net_roi = gross_roi - cost_pct if outcome != "expired" else gross_roi

                trades.append(Trade(
                    symbol=symbol, direction=direction, entry_price=fill_price,
                    tp_price=tp_price, sl_price=sl_price,
                    rr=round(TP_ROI_PCT / SL_ROI_PCT, 2), outcome=outcome,
                    gross_roi_pct=round(gross_roi, 3), net_roi_pct=round(net_roi, 3),
                    closed_at=closed_at_str,
                    score=entry_score, entry_time=entry_time_str,
                    hold_minutes=bars_held * entry_tf_minutes,
                ))
                in_trade_until_idx = i + bars_held
                pending_setup = None
                continue

            setup = strategy.detect_pending_setup(symbol)
            if setup is not None:
                setup["setup_time"] = df_entry_full.index[i].isoformat()
                setup["status"] = "pending_pullback"
                pending_setup = setup
    finally:
        strategy.get_market_klines = original_get_market_klines
        strategy.datetime = original_datetime

    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Zero-Lag MTF Pullback v1")
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
