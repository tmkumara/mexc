"""
backtest/optimize.py — walk-forward parameter optimization for Super
Scalper v3 (Phase 4).

Methodology (deliberately NOT a naive grid search over all 6 months):
  - The 6-month parquet history for a symbol is split into up to 4
    rolling windows: 6 weeks TRAIN followed by 2 weeks TEST, each window
    rolled forward by the 2-week test length.
  - The parameter grid (atr_mult x entry_zone x adx_min x chop_max x
    min_strength -- 3^5 = 243 combinations) is searched ONLY on each
    window's TRAIN slice. A candidate is only eligible if it produces
    >= MIN_TRAIN_TRADES trades on train (rejects lucky-but-thin
    parameter sets); among eligible candidates the one with the best
    train profit factor is selected.
  - The selected params are then run ONCE, untouched, on that window's
    TEST slice, and ONLY the test-window performance is reported --
    train performance never appears in the final numbers.
  - The final "recommended" param set per symbol is the one selected in
    the most recent (last) window, since it best reflects the current
    market regime; all 4 windows' out-of-sample results are reported for
    transparency so the recommendation isn't taken on faith.
  - For every OOS test window we also report the SAME metrics for (a)
    the skipped-signal population (flips confluence_ok() rejected) and
    (b) the SuperTrend-only baseline (every flip, no confluence gate) --
    Phase 3's requirement that the regime/channel filter's rejects
    provably have worse expectancy than what it accepts.

Usage (run on a host with the real backtest/data/<SYMBOL>_<interval>.parquet
files from fetch_data.py -- see backtest/README.md):
    python backtest/optimize.py --symbols BTC_USDT,ETH_USDT,SOL_USDT --data-interval 5m
    python backtest/optimize.py --symbols BTC_USDT --data-interval 5m --quick   # tiny grid, for a fast sanity check
    python backtest/optimize.py --symbols BTC_USDT --data-interval 5m --write-config   # after reviewing the report, persist recommended params into config.py (LIVE_ENABLED stays false)

--data-interval must match whatever --interval you passed to fetch_data.py
(default 5m on both sides -- MEXC's Min1 REST history only retains ~30
days, not enough for a 6-month walk-forward backtest).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import re
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from backtest.engine import BacktestParams, run_backtest, compute_metrics
import scalper_v3_strategy as v3s

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("optimize")

DATA_DIR = Path(__file__).resolve().parent / "data"
REPORTS_DIR = Path(__file__).resolve().parent / "reports"

FULL_GRID = {
    "atr_mult": [2.0, 2.5, 3.0],
    "entry_zone": [0.35, 0.45, 0.55],
    "adx_min": [18, 22, 26],
    "chop_max": [45, 50, 55],
    "min_strength": [2, 3, 4],
}
QUICK_GRID = {
    "atr_mult": [2.5],
    "entry_zone": [0.45],
    "adx_min": [22],
    "chop_max": [50],
    "min_strength": [3],
}

MIN_TRAIN_TRADES = 30
TRAIN_WEEKS = 6
TEST_WEEKS = 2
MAX_WINDOWS = 4


def load_symbol_data(symbol: str, interval: str = "5m", data_dir: Path = DATA_DIR) -> pd.DataFrame:
    path = data_dir / f"{symbol}_{interval}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- run backtest/fetch_data.py --interval {interval} first "
            f"(see backtest/README.md). If you fetched a different interval, pass --data-interval to match."
        )
    return pd.read_parquet(path)


def build_windows(df_1m: pd.DataFrame) -> list[dict]:
    start = df_1m.index.min()
    end = df_1m.index.max()
    windows = []
    cursor = start
    for _ in range(MAX_WINDOWS):
        train_start = cursor
        train_end = train_start + pd.Timedelta(weeks=TRAIN_WEEKS)
        test_start = train_end
        test_end = test_start + pd.Timedelta(weeks=TEST_WEEKS)
        if test_end > end:
            break
        windows.append({
            "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
        })
        cursor = test_start  # roll forward by the test length
    return windows


def _params_for(combo: dict, min_strength: int, entry_timeframe: str) -> BacktestParams:
    kwargs = v3s.scalper_kwargs()
    kwargs.update({k: v for k, v in combo.items() if k != "min_strength"})
    return BacktestParams(
        scalper_kwargs=kwargs, min_strength=min_strength, entry_timeframe=entry_timeframe,
        warmup_bars=250,
    )


def grid_search_window(df_1m: pd.DataFrame, symbol: str, window: dict, grid: dict, entry_timeframe: str) -> dict | None:
    train_df = df_1m[(df_1m.index >= window["train_start"]) & (df_1m.index < window["train_end"])]
    if train_df.empty:
        return None

    keys = list(grid.keys())
    best = None
    for combo_values in itertools.product(*(grid[k] for k in keys)):
        combo = dict(zip(keys, combo_values))
        params = _params_for(combo, combo["min_strength"], entry_timeframe)
        result = run_backtest(train_df, symbol, params)
        metrics = compute_metrics(result.trades, params.initial_equity)

        if metrics["total_trades"] < MIN_TRAIN_TRADES:
            continue
        pf = metrics["profit_factor"]
        if pf is None:
            continue
        pf_score = pf if pf != float("inf") else 1e9

        if best is None or pf_score > best["train_pf_score"]:
            best = {"combo": combo, "train_metrics": metrics, "train_pf_score": pf_score}

    return best


def run_walk_forward(symbol: str, grid: dict, entry_timeframe: str, data_interval: str = "5m") -> dict:
    from config import _TF_MINUTES
    if _TF_MINUTES.get(data_interval, 5) > _TF_MINUTES.get(entry_timeframe, 5):
        raise ValueError(
            f"--data-interval {data_interval} is coarser than --entry-timeframe {entry_timeframe} -- "
            f"can't upsample. Fetch data at {entry_timeframe} or coarser-but-still-divides-evenly, or "
            f"pass a coarser --entry-timeframe."
        )

    df_1m = load_symbol_data(symbol, data_interval)
    windows = build_windows(df_1m)
    if not windows:
        raise ValueError(
            f"{symbol}: not enough history for even one {TRAIN_WEEKS + TEST_WEEKS}-week "
            f"train+test window ({len(df_1m)} {data_interval} bars spanning "
            f"{(df_1m.index.max() - df_1m.index.min()).days if not df_1m.empty else 0} days)"
        )

    window_results = []
    for i, window in enumerate(windows):
        best = grid_search_window(df_1m, symbol, window, grid, entry_timeframe)
        if best is None:
            logger.warning("[%s] window %d: no param set met the >=%d train-trade minimum -- window rejected",
                           symbol, i + 1, MIN_TRAIN_TRADES)
            window_results.append({"window": window, "rejected": True})
            continue

        test_df = df_1m[(df_1m.index >= window["test_start"]) & (df_1m.index < window["test_end"])]
        params = _params_for(best["combo"], best["combo"]["min_strength"], entry_timeframe)
        test_result = run_backtest(test_df, symbol, params)

        test_metrics = compute_metrics(test_result.trades, params.initial_equity)
        baseline_metrics = compute_metrics(test_result.all_flip_trades, params.initial_equity)
        skipped_metrics = compute_metrics(test_result.skipped_trades, params.initial_equity)

        window_results.append({
            "window": window,
            "rejected": False,
            "combo": best["combo"],
            "train_metrics": best["train_metrics"],
            "test_metrics": test_metrics,
            "baseline_metrics": baseline_metrics,
            "skipped_metrics": skipped_metrics,
            "n_flip_trades": len(test_result.flip_trades),
            "n_pullback_trades": len(test_result.pullback_trades),
            "test_low_confidence": test_metrics["total_trades"] < MIN_TRAIN_TRADES,
            "equity_curve": test_result.equity_curve,
            "baseline_equity_curve": test_result.baseline_equity_curve,
        })

    accepted = [w for w in window_results if not w["rejected"]]
    recommended = accepted[-1]["combo"] if accepted else None

    return {
        "symbol": symbol,
        "windows": window_results,
        "recommended_params": recommended,
        "n_windows": len(windows),
        "n_accepted": len(accepted),
    }


# ── reporting ───────────────────────────────────────────────────────

def _stitch_equity_curves(window_results: list[dict], key: str, initial_equity: float) -> pd.Series:
    pieces = []
    for w in window_results:
        if w.get("rejected"):
            continue
        curve = w[key]
        if not curve.empty:
            pieces.append(curve)
    if not pieces:
        return pd.Series(dtype=float)
    return pd.concat(pieces).sort_index()


def plot_equity_curve(symbol: str, wf_result: dict, initial_equity: float, out_dir: Path) -> Path | None:
    filtered = _stitch_equity_curves(wf_result["windows"], "equity_curve", initial_equity)
    baseline = _stitch_equity_curves(wf_result["windows"], "baseline_equity_curve", initial_equity)
    if filtered.empty and baseline.empty:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 5))
    if not filtered.empty:
        ax.plot(filtered.index, filtered.values, label="v3 filtered (regime+channel gate)", linewidth=1.5)
    if not baseline.empty:
        ax.plot(baseline.index, baseline.values, label="SuperTrend-only baseline", linewidth=1.0, alpha=0.7)
    ax.axhline(initial_equity, color="gray", linestyle="--", linewidth=0.8)
    ax.set_title(f"{symbol} -- out-of-sample equity curve (stitched walk-forward test windows)")
    ax.set_ylabel("Equity")
    ax.legend()
    fig.autofmt_xdate()
    path = out_dir / f"{symbol}_equity_curve.png"
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def build_report(all_results: list[dict], initial_equity: float, out_dir: Path) -> str:
    lines = ["# Super Scalper v3 -- Phase 4 Walk-Forward Optimization Report", ""]
    lines.append(
        f"Methodology: {MAX_WINDOWS} rolling windows ({TRAIN_WEEKS}w train / {TEST_WEEKS}w test), "
        f"grid searched on train only (min {MIN_TRAIN_TRADES} train trades to qualify, ranked by train "
        f"profit factor), reported numbers are TEST-window (out-of-sample) only."
    )
    lines.append("")

    for wf in all_results:
        symbol = wf["symbol"]
        lines.append(f"## {symbol}")
        lines.append("")
        lines.append(f"Windows: {wf['n_accepted']}/{wf['n_windows']} accepted (rest rejected for <{MIN_TRAIN_TRADES} train trades)")
        lines.append("")
        lines.append("| Window | Train | Test | Params | Test trades (flip/pullback) | Test WR% | Test PF | Test maxDD% | Baseline PF | Skipped PF | Confidence |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")

        for i, w in enumerate(wf["windows"]):
            win = w["window"]
            train_span = f"{win['train_start'].date()}→{win['train_end'].date()}"
            test_span = f"{win['test_start'].date()}→{win['test_end'].date()}"
            if w["rejected"]:
                lines.append(f"| {i+1} | {train_span} | {test_span} | REJECTED | - | - | - | - | - | - | - |")
                continue
            c = w["combo"]
            tm = w["test_metrics"]
            bm = w["baseline_metrics"]
            sm = w["skipped_metrics"]
            combo_str = f"atr_mult={c['atr_mult']} ez={c['entry_zone']} adx≥{c['adx_min']} chop≤{c['chop_max']} str≥{c['min_strength']}"
            conf = "LOW (<30 trades)" if w["test_low_confidence"] else "ok"
            trades_str = f"{tm['total_trades']} ({w['n_flip_trades']}/{w['n_pullback_trades']})"
            lines.append(
                f"| {i+1} | {train_span} | {test_span} | {combo_str} | {trades_str} | "
                f"{tm['win_rate']} | {tm['profit_factor']} | {tm['max_drawdown_pct']} | "
                f"{bm['profit_factor']} | {sm['profit_factor']} | {conf} |"
            )

        lines.append("")
        rec = wf["recommended_params"]
        if rec:
            lines.append(f"**Recommended params for {symbol}** (from most recent accepted window): `{rec}`")
        else:
            lines.append(f"**No window produced a qualifying param set for {symbol} -- do not enable live.**")
        lines.append("")

        # filter-value check: flag if skipped trades outperform accepted trades anywhere
        for i, w in enumerate(wf["windows"]):
            if w.get("rejected"):
                continue
            sm, tm = w["skipped_metrics"], w["test_metrics"]
            if sm["total_trades"] >= 10 and sm["profit_factor"] is not None and tm["profit_factor"] is not None:
                if sm["profit_factor"] > tm["profit_factor"]:
                    lines.append(
                        f"⚠️ **Window {i+1}: skipped signals (PF={sm['profit_factor']}) outperformed "
                        f"accepted signals (PF={tm['profit_factor']}) -- the regime filter may be too tight here.**"
                    )
        lines.append("")

        img_path = plot_equity_curve(symbol, wf, initial_equity, out_dir)
        if img_path:
            lines.append(f"![{symbol} equity curve]({img_path.name})")
            lines.append("")

    report_text = "\n".join(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phase4_report.md").write_text(report_text)
    return report_text


def write_recommended_config(all_results: list[dict], config_path: Path) -> None:
    """Persist each symbol's most-recent-window recommended params into
    config.py's SCALPER_V3_* default fallbacks. Only touches the numeric
    default literal in `os.getenv("KEY", "<default>")` -- LIVE_ENABLED is
    deliberately left untouched (stays false) so this never flips live
    trading on by itself."""
    text = config_path.read_text()

    # Use the LAST symbol's recommendation that has one, as a single
    # shared default (config.py has no per-symbol param support today --
    # per-symbol overrides would need a config schema change out of scope here).
    rec = next((wf["recommended_params"] for wf in reversed(all_results) if wf["recommended_params"]), None)
    if rec is None:
        logger.warning("No symbol produced a qualifying recommendation -- config.py left unchanged.")
        return

    key_map = {
        "atr_mult": "SCALPER_V3_ATR_MULT",
        "entry_zone": "SCALPER_V3_ENTRY_ZONE",
        "adx_min": "SCALPER_V3_ADX_MIN",
        "chop_max": "SCALPER_V3_CHOP_MAX",
        "min_strength": "SCALPER_V3_MIN_STRENGTH",
    }
    for combo_key, config_key in key_map.items():
        value = rec[combo_key]
        pattern = rf'({config_key}\s*:\s*(?:int|float)\s*=\s*int\(os\.getenv\("{config_key}",\s*")([^"]+)("\)\))'
        pattern_float = rf'({config_key}\s*:\s*(?:int|float)\s*=\s*float\(os\.getenv\("{config_key}",\s*")([^"]+)("\)\))'
        new_text, n = re.subn(pattern, lambda m: m.group(1) + str(value) + m.group(3), text)
        if n == 0:
            new_text, n = re.subn(pattern_float, lambda m: m.group(1) + str(value) + m.group(3), text)
        if n == 0:
            logger.warning("Could not locate %s in config.py -- left unchanged", config_key)
            continue
        text = new_text

    config_path.write_text(text)
    logger.info("Wrote recommended params into %s (LIVE_ENABLED untouched -- still false)", config_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", type=str, required=True, help="Comma-separated symbols, must match fetched parquet files")
    parser.add_argument("--quick", action="store_true", help="Use a 1-combo grid for a fast sanity check instead of the full 243-combo grid")
    parser.add_argument("--entry-timeframe", type=str, default="5m",
                        help="Timeframe SuperScalper trades on (must be >= --data-interval granularity)")
    parser.add_argument("--data-interval", type=str, default="5m",
                        help="Interval of the fetched parquet files to load, e.g. backtest/fetch_data.py --interval 5m "
                             "writes <SYMBOL>_5m.parquet -- must match here.")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--write-config", action="store_true",
                        help="After reporting, persist the recommended params into config.py's SCALPER_V3_* defaults. LIVE_ENABLED is never touched.")
    parser.add_argument("--out", type=str, default=str(REPORTS_DIR))
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    grid = QUICK_GRID if args.quick else FULL_GRID
    out_dir = Path(args.out)

    all_results = []
    for symbol in symbols:
        logger.info("[%s] walk-forward optimization starting (grid size=%d)", symbol,
                    len(list(itertools.product(*grid.values()))))
        wf = run_walk_forward(symbol, grid, args.entry_timeframe, args.data_interval)
        all_results.append(wf)

    report_text = build_report(all_results, args.initial_equity, out_dir)
    print(report_text)

    summary_path = out_dir / "phase4_summary.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = [{
        "symbol": wf["symbol"],
        "recommended_params": wf["recommended_params"],
        "n_accepted": wf["n_accepted"],
        "n_windows": wf["n_windows"],
    } for wf in all_results]
    summary_path.write_text(json.dumps(summary, indent=2))
    logger.info("Report written to %s", out_dir / "phase4_report.md")

    if args.write_config:
        write_recommended_config(all_results, Path(__file__).resolve().parent.parent / "config.py")


if __name__ == "__main__":
    main()
