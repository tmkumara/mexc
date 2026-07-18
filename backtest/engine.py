"""
backtest/engine.py — event-driven, bar-by-bar backtester for Super
Scalper v3, built on the same SuperScalper + confluence_ok + walk_trade
code the live bot uses (scalper_v3_strategy.py), so backtested behavior
can't silently drift from live behavior.

No lookahead: a signal computed from bar N's CLOSE executes at bar N+1's
OPEN. All indicators (SuperTrend, Keltner, AO, ADX, Choppiness, band
expansion) are causal (ewm/rolling/diff/shift only look backward), so
vectorizing SuperScalper.compute() once over the full series is safe --
row N's indicator values never depend on rows > N.

Runs TWO parallel virtual books over one pass through the data so the
Phase 3 "skipped vs accepted" comparison is apples-to-apples:
  - "filtered" book: only signals where confluence_ok() passes (what the
    live bot actually takes).
  - "all_flips" book: every SuperTrend flip, confluence gate ignored --
    the "SuperTrend-only" baseline. Trades in this book that failed
    confluence_ok() are also tagged as the skipped-signal population.
Each book enforces its own one-position-at-a-time exclusivity per
symbol, so the two books can diverge in *timing* (a filtered-out flip
doesn't block the filtered book's next real entry).

The funding-rate and liq_estimator safety filters from the live scanner
are NOT applied here (Phase 2 only fetches OHLCV, not funding-rate
history), so this measures the regime/channel confluence layer in
isolation -- see backtest/README.md.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from super_scalper_v3 import SuperScalper
import scalper_v3_strategy as v3s


# ── config / result types ──────────────────────────────────────────────

@dataclass
class BacktestParams:
    scalper_kwargs: dict
    min_strength: int = 3
    initial_equity: float = 10_000.0
    risk_pct: float = 0.01          # 1% of current equity risked per trade
    taker_fee_pct: float = 0.0      # per side; MEXC's v1 coin pool is zero-fee USDT perps (coin_scanner.py) -- override for fee-bearing symbols
    slippage_ticks: float = 1.0
    tick_pct: float = 0.0002        # price move per "tick" as a fraction of price (2bps default -- no live tick-size feed in this sandbox)
    entry_timeframe: str = "5m"
    warmup_bars: int = 250          # bars needed before indicators (esp. EMA200-scale ones) are trustworthy
    apply_breakeven: bool = True


@dataclass
class Trade:
    symbol: str
    direction: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp | None
    exit_price: float | None
    sl_price: float
    tp1_price: float
    tp2_price: float
    exit_reason: str | None
    tp1_hit: bool
    bars_held: int
    position_size: float
    pnl: float
    pnl_pct: float
    r_multiple: float
    equity_after: float
    taken: bool                 # True = passed confluence_ok (the "filtered"/real book)
    skip_reason: str | None = None


@dataclass
class BacktestResult:
    symbol: str
    trades: list[Trade] = field(default_factory=list)          # filtered/real book
    all_flip_trades: list[Trade] = field(default_factory=list)  # SuperTrend-only baseline (every flip)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    baseline_equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    params: BacktestParams | None = None

    @property
    def skipped_trades(self) -> list["Trade"]:
        return [t for t in self.all_flip_trades if not t.taken]


# ── resampling ───────────────────────────────────────────────────────

def resample_ohlcv(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = {"1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[timeframe]
    out = df_1m.resample(rule, label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    })
    out.dropna(inplace=True)
    return out


# ── per-bar signal extraction (mirrors SuperScalper.latest_signal) ────

def _row_signal(computed: pd.DataFrame, i: int) -> dict:
    row = computed.iloc[i]
    f = lambda k, d=0.0: float(row[k]) if pd.notna(row[k]) else d
    return {
        "side": row["signal"],
        "trend": "BULLISH" if row["trend"] == 1 else "BEARISH",
        "strength": int(row["strength"]) if pd.notna(row["strength"]) else 0,
        "ao": f("ao"), "ao_rising": bool(row["ao_rising"]) if pd.notna(row["ao_rising"]) else False,
        "kc_pos": f("kc_pos", 0.5), "kc_slope": f("kc_slope"),
        "kc_mid": f("kc_mid"), "kc_upper": f("kc_upper"), "kc_lower": f("kc_lower"),
        "adx": f("adx"), "chop": f("chop", 50.0),
        "expansion": f("expansion", 1.0),
        "regime": row["regime"],
        "regime_votes": int(row["regime_votes"]) if pd.notna(row["regime_votes"]) else 0,
        "stop_loss": f("supertrend"),
        "price": f("close"),
    }


# ── trade simulation ────────────────────────────────────────────────

def _simulate_trade(
    symbol: str, direction: str, signal_time: pd.Timestamp,
    computed: pd.DataFrame, entry_idx: int,
    sl: float, tp1: float, tp2: float,
    equity: float, params: BacktestParams, taken: bool, skip_reason: str | None,
) -> Trade | None:
    """entry_idx is bar N+1 -- the bar whose OPEN is the fill price."""
    if entry_idx >= len(computed):
        return None

    raw_entry = float(computed["open"].iloc[entry_idx])
    tick = raw_entry * params.tick_pct * params.slippage_ticks
    entry_price = raw_entry + tick if direction == "LONG" else raw_entry - tick  # slippage always adverse

    risk_amount = equity * params.risk_pct
    distance = abs(entry_price - sl)
    if distance <= 0:
        return None
    position_size = risk_amount / distance

    bars_after = computed.iloc[entry_idx + 1:]
    if bars_after.empty:
        result = {"status": "pending", "exit_price": None, "exit_reason": None,
                   "tp1_hit": False, "bars_held": 0, "final_sl": sl}
    else:
        result = v3s.walk_trade(direction, entry_price, sl, tp1, tp2, bars_after)

    entry_fee = position_size * entry_price * params.taker_fee_pct

    if result["status"] == "pending":
        return Trade(
            symbol=symbol, direction=direction, signal_time=signal_time,
            entry_time=computed.index[entry_idx], entry_price=entry_price,
            exit_time=None, exit_price=None, sl_price=sl, tp1_price=tp1, tp2_price=tp2,
            exit_reason=None, tp1_hit=result["tp1_hit"], bars_held=result["bars_held"],
            position_size=position_size, pnl=-entry_fee, pnl_pct=0.0, r_multiple=0.0,
            equity_after=equity - entry_fee, taken=taken, skip_reason=skip_reason,
        )

    raw_exit = result["exit_price"]
    exit_tick = raw_exit * params.tick_pct * params.slippage_ticks
    exit_price = raw_exit - exit_tick if direction == "LONG" else raw_exit + exit_tick

    gross_pnl = (
        (exit_price - entry_price) * position_size
        if direction == "LONG"
        else (entry_price - exit_price) * position_size
    )
    exit_fee = position_size * exit_price * params.taker_fee_pct
    pnl = gross_pnl - entry_fee - exit_fee
    pnl_pct = pnl / equity if equity > 0 else 0.0
    r_multiple = pnl / risk_amount if risk_amount > 0 else 0.0

    exit_idx = entry_idx + 1 + (result["bars_held"] - 1)
    exit_time = computed.index[min(exit_idx, len(computed) - 1)]

    return Trade(
        symbol=symbol, direction=direction, signal_time=signal_time,
        entry_time=computed.index[entry_idx], entry_price=entry_price,
        exit_time=exit_time, exit_price=exit_price,
        sl_price=sl, tp1_price=tp1, tp2_price=tp2,
        exit_reason=result["exit_reason"], tp1_hit=result["tp1_hit"], bars_held=result["bars_held"],
        position_size=position_size, pnl=pnl, pnl_pct=pnl_pct, r_multiple=r_multiple,
        equity_after=equity + pnl, taken=taken, skip_reason=skip_reason,
    )


def run_backtest(df_1m: pd.DataFrame, symbol: str, params: BacktestParams) -> BacktestResult:
    df = resample_ohlcv(df_1m, params.entry_timeframe)
    if len(df) < params.warmup_bars + 5:
        return BacktestResult(symbol=symbol, params=params)

    engine = SuperScalper(**params.scalper_kwargs)
    computed = engine.compute(df)

    filtered_trades: list[Trade] = []
    all_flip_trades: list[Trade] = []

    filtered_equity = params.initial_equity
    baseline_equity = params.initial_equity
    filtered_open_until: int = -1   # bar index; filtered book is flat once current index >= this
    baseline_open_until: int = -1

    filtered_curve: dict[pd.Timestamp, float] = {}
    baseline_curve: dict[pd.Timestamp, float] = {}

    n = len(computed)
    for i in range(params.warmup_bars, n - 1):  # need i+1 (entry bar) to exist
        sig = _row_signal(computed, i)
        if sig["side"] is None:
            continue

        direction = "LONG" if sig["side"] == "BUY" else "SHORT"
        sl, tp1, tp2 = v3s._calc_tp_sl(direction, sig)
        if not v3s.valid_v3_geometry(direction, sig["price"], sl, tp1, tp2):
            continue

        signal_time = computed.index[i]
        entry_idx = i + 1
        confluence = engine.confluence_ok(sig, min_strength=params.min_strength)

        # -- baseline book: every valid flip, no confluence gate --
        if entry_idx > baseline_open_until:
            trade = _simulate_trade(
                symbol, direction, signal_time, computed, entry_idx, sl, tp1, tp2,
                baseline_equity, params, taken=confluence,
                skip_reason=None if confluence else v3s._confluence_reject_reason(sig, direction),
            )
            if trade is not None:
                all_flip_trades.append(trade)
                if trade.exit_time is not None:
                    baseline_equity = trade.equity_after
                    baseline_open_until = computed.index.get_loc(trade.exit_time)
                    if isinstance(baseline_open_until, slice):
                        baseline_open_until = baseline_open_until.stop - 1
                else:
                    baseline_open_until = n  # still open at data end -- book stays flat
                baseline_curve[trade.entry_time] = baseline_equity

        # -- filtered book: only confluence_ok() passes (what live actually trades) --
        if confluence and entry_idx > filtered_open_until:
            trade = _simulate_trade(
                symbol, direction, signal_time, computed, entry_idx, sl, tp1, tp2,
                filtered_equity, params, taken=True, skip_reason=None,
            )
            if trade is not None:
                filtered_trades.append(trade)
                if trade.exit_time is not None:
                    filtered_equity = trade.equity_after
                    filtered_open_until = computed.index.get_loc(trade.exit_time)
                    if isinstance(filtered_open_until, slice):
                        filtered_open_until = filtered_open_until.stop - 1
                else:
                    filtered_open_until = n
                filtered_curve[trade.entry_time] = filtered_equity

    return BacktestResult(
        symbol=symbol,
        trades=filtered_trades,
        all_flip_trades=all_flip_trades,
        equity_curve=pd.Series(filtered_curve, dtype=float).sort_index(),
        baseline_equity_curve=pd.Series(baseline_curve, dtype=float).sort_index(),
        params=params,
    )


# ── metrics ─────────────────────────────────────────────────────────

def compute_metrics(trades: list[Trade], initial_equity: float) -> dict:
    closed = [t for t in trades if t.exit_time is not None]
    if not closed:
        return {
            "total_trades": 0, "win_rate": 0.0, "profit_factor": None,
            "max_drawdown_pct": 0.0, "avg_r_multiple": 0.0, "avg_duration_bars": 0.0,
            "total_pnl": 0.0, "total_return_pct": 0.0, "monthly": {},
        }

    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    equity_series = pd.Series([initial_equity] + [t.equity_after for t in closed])
    running_max = equity_series.cummax()
    drawdown = (equity_series - running_max) / running_max
    max_dd_pct = float(drawdown.min() * 100.0)

    final_equity = closed[-1].equity_after
    monthly: dict[str, dict] = {}
    for t in closed:
        key = t.exit_time.strftime("%Y-%m")
        m = monthly.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
        m["trades"] += 1
        m["wins"] += 1 if t.pnl > 0 else 0
        m["pnl"] += t.pnl
    for m in monthly.values():
        m["win_rate"] = round(m["wins"] / m["trades"] * 100.0, 1) if m["trades"] else 0.0

    return {
        "total_trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100.0, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "avg_r_multiple": round(sum(t.r_multiple for t in closed) / len(closed), 3),
        "avg_duration_bars": round(sum(t.bars_held for t in closed) / len(closed), 1),
        "total_pnl": round(sum(t.pnl for t in closed), 2),
        "total_return_pct": round((final_equity - initial_equity) / initial_equity * 100.0, 2),
        "monthly": monthly,
    }
