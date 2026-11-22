"""Freeze Phase 2.4 metrics and evaluate a separate, local holdout."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from hashlib import sha256
from html import escape
import json
from pathlib import Path
from random import Random
from statistics import mean, median
from types import SimpleNamespace

from strategy.market_structure.models import FOUR_HOURS

from .data import load_candles, utc, write_candles
from .evaluate import HORIZONS, METRICS, read_rows
from .group import FORWARD_COLUMNS, GROUP_COLUMNS, analyze_run, evaluate_groups
from .replay import run as run_replay, write_json, write_rows


SYMBOLS = ("ADA_USDT", "BNB_USDT", "ETH_USDT", "SOL_USDT")
WARMUP_BARS = 60
SEED = 25025
BOOTSTRAP_REPLICATES = 500
CONTINUOUS = ("max_displacement_atr", "max_swing_atr", "prior_level_tests",
              "oldest_level_age_bars", "swing_level_count")
BINARY = ("external_level_count", "range_break")
FINGERPRINT_FILES = (
    *(f"strategy/market_structure/{name}.py" for name in
      ("choch", "discovery", "internal", "models", "structure", "swings")),
    *(f"research/strong_choch/{name}.py" for name in ("data", "evaluate", "group", "replay")),
)
DEFINITIONS = {
    "max_displacement_atr": "Maximum close beyond a broken level, divided by break-candle ATR",
    "max_swing_atr": "Maximum swing movement divided by its confirmation ATR",
    "prior_level_tests": "Sum of prior touch episodes across distinct level identities",
    "oldest_level_age_bars": "Maximum confirmed level age in completed 4H bars",
    "swing_level_count": "Number of distinct broken swing identities",
    "external_level_count": "Any broken level assigned an external role by the event close",
    "range_break": "Presence of attached confirmed range-break evidence",
}


def checksum(path):
    return sha256(path.read_bytes()).hexdigest()


def fingerprint():
    root = Path(__file__).resolve().parents[2]
    files = {name: checksum(root / name) for name in FINGERPRINT_FILES}
    digest = sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"sha256": digest, "files": files, "validation_sha256": checksum(Path(__file__))}


def quantile(sorted_values, probability):
    position = (len(sorted_values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def development_bands(groups):
    return {
        key: [quantile(values, p) for p in (1 / 3, 2 / 3)]
        for key in CONTINUOUS
        if (values := sorted(float(group[key]) for group in groups if group[key] != ""))
    }


def freeze(development, output):
    source_metadata = {
        symbol: json.loads((development / symbol / "run_metadata.json").read_text())
        for symbol in SYMBOLS
    }
    fields = ("evaluation_start_inclusive", "evaluation_end_exclusive", "warmup_start_inclusive",
              "parameters", "internal_parameters", "discovery_range_edge_fraction", "algorithm_version")
    common = {key: source_metadata[SYMBOLS[0]][key] for key in fields}
    if any(any(meta[key] != common[key] for key in fields) for meta in source_metadata.values()):
        raise ValueError("Development metadata must agree across symbols")
    grouped = development / "grouped_events.csv"
    raw_groups = read_rows(grouped)
    dev_start, dev_end = utc(common["evaluation_start_inclusive"]), utc(common["evaluation_end_exclusive"])
    if not raw_groups or any(not dev_start < utc(row["event_timestamp"]) <= dev_end
                             or row["symbol"] not in SYMBOLS for row in raw_groups):
        raise ValueError("Development groups must belong to the declared development period")
    manifest = {}
    for symbol, meta in source_metadata.items():
        source = Path(meta["source_path"])
        if checksum(source) != meta["source_sha256"]:
            raise ValueError(f"Development source checksum mismatch for {symbol}")
        bars, _ = load_candles(source, symbol, meta["source_timeframe"], utc(meta["as_of"]))
        manifest[symbol] = dict(path=str(source), sha256=meta["source_sha256"],
                                source_timeframe=meta["source_timeframe"],
                                first_open=bars[0].timestamp.isoformat(),
                                last_close=bars[-1].closed_at.isoformat(), bars_4h=len(bars))
    consulted_start = min(row["first_open"] for row in manifest.values())
    consulted_end = max(row["last_close"] for row in manifest.values())
    bands = development_bands(raw_groups)
    if set(bands) != set(CONTINUOUS):
        raise ValueError("The development cohort is missing a continuous metric")
    lock = dict(common, fingerprint=fingerprint(), development_group_sha256=checksum(grouped),
                development_groups=len(raw_groups), bands=bands, metric_definitions=DEFINITIONS,
                consulted_start=consulted_start, consulted_end=consulted_end,
                warmup_bars=WARMUP_BARS, horizons=HORIZONS, baseline_seed=SEED,
                bootstrap_replicates=BOOTSTRAP_REPLICATES)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "algorithm_fingerprint.json", lock)
    write_json(output / "dataset_manifest.json", manifest)
    return lock, manifest


def validate_window(start, end, lock):
    if start >= end or any(stamp.hour % 4 or stamp.minute or stamp.second or stamp.microsecond
                           for stamp in (start, end)):
        raise ValueError("Use ordered UTC-aligned 4H evaluation boundaries")
    if start < utc(lock["consulted_end"]) and end > utc(lock["consulted_start"]):
        raise ValueError("Evaluation overlaps the development data or its consulted warm-up")


def validation_bars(bars, start, end):
    warmup_start = start - WARMUP_BARS * FOUR_HOURS
    selected = [bar for bar in bars if bar.timestamp >= warmup_start and bar.closed_at <= end]
    if (not selected or selected[0].timestamp != warmup_start or selected[-1].closed_at != end
            or len(selected) != (end - warmup_start) // FOUR_HOURS):
        raise ValueError(f"Required contiguous coverage is {warmup_start} through {end}")
    return selected


def band_for(group, metric, bands):
    value = group[metric]
    if metric in BINARY:
        return "with" if value else "without"
    if value is None:
        return "missing"
    lower, upper = bands[metric]
    return "low" if value <= lower else "middle" if value <= upper else "high"


def sample_baseline(candles, groups, start):
    """Sample independently within each symbol/direction, without replacement."""
    by_direction = Counter(group["direction"] for group in groups)
    eligible = [bar for bar in candles if bar.timestamp >= start]
    for direction, count in sorted(by_direction.items()):
        rng = Random(f"{SEED}:{candles[0].symbol}:{direction}")
        for candle in sorted(rng.sample(eligible, count), key=lambda bar: bar.closed_at):
            yield dict(event_id=f"baseline:{candle.symbol}:{candle.closed_at.isoformat()}:{direction}",
                       symbol=candle.symbol, direction=direction, event_timestamp=candle.closed_at,
                       break_close=candle.close, range_break=False)


def mean_block_interval(rows, key):
    blocks = defaultdict(list)
    for row in rows:
        day = utc(row["event_timestamp"]).date()
        blocks[day - timedelta(days=day.weekday())].append(row[key])
    if len(blocks) < 6:
        return None, None, len(blocks)
    weeks = [(sum(values), len(values)) for _, values in sorted(blocks.items())]
    rng = Random(SEED)
    draws = []
    for _ in range(BOOTSTRAP_REPLICATES):
        picked = rng.choices(weeks, k=len(weeks))
        draws.append(sum(total for total, _ in picked) / sum(count for _, count in picked))
    draws.sort()
    return quantile(draws, .025), quantile(draws, .975), len(blocks)


def summarize(rows):
    complete = [row for row in rows if row["complete"]]
    close_key = METRICS[2]
    low, high, blocks = mean_block_interval(complete, close_key)
    result = dict(
        sample_size=len(rows), complete=len(complete), incomplete=len(rows) - len(complete),
        later_external_choch=sum(row["external_choch_follows"] is True for row in rows),
        later_same_direction=sum(row["same_direction_external_choch_follows"] is True for row in rows),
        no_later_external_choch=sum(row["external_choch_follows"] is False for row in rows),
        unknown_later_external_choch=sum(row["external_choch_follows"] is None for row in rows),
        positive_return_pct=100 * mean(row[close_key] > 0 for row in complete) if complete else None,
        mean_return_ci_low=low, mean_return_ci_high=high, nonempty_utc_weeks=blocks,
    )
    for key in METRICS:
        values = sorted(row[key] for row in complete)
        for prefix, calculate in (("mean", mean), ("median", median),
                                  ("p25", lambda data: quantile(data, .25)),
                                  ("p75", lambda data: quantile(data, .75))):
            result[f"{prefix}_{key}"] = calculate(values) if values else None
    return result


def stratify(groups, forward, baseline_forward, bands):
    metrics, baselines = defaultdict(list), defaultdict(list)
    by_id = {group["event_id"]: group for group in groups}
    for row in forward:
        group = by_id[row["event_id"]]
        for symbol, direction in (("all", "all"), (row["symbol"], "all"),
                                  ("all", row["direction"]), (row["symbol"], row["direction"])):
            cohort = symbol, direction, row["horizon"]
            for metric in (*CONTINUOUS, *BINARY):
                band = band_for(group, metric, bands)
                metrics[(metric, band, *cohort)].append(row)
            baselines[("all_groups", *cohort)].append(row)
            displacement = band_for(group, "max_displacement_atr", bands)
            if displacement in ("low", "high"):
                baselines[(f"{displacement}_displacement", *cohort)].append(row)
    for row in baseline_forward:
        for symbol, direction in (("all", "all"), (row["symbol"], "all"),
                                  ("all", row["direction"]), (row["symbol"], row["direction"])):
            baselines[("sampled_candles", symbol, direction, row["horizon"])].append(row)
    metric_rows = [dict(metric=metric, band=band, symbol=symbol, direction=direction,
                        horizon=horizon, **summarize(rows))
                   for (metric, band, symbol, direction, horizon), rows in sorted(metrics.items())]
    baseline_rows = [dict(baseline=label, symbol=symbol, direction=direction,
                          horizon=horizon, **summarize(rows))
                     for (label, symbol, direction, horizon), rows in sorted(baselines.items())]
    return metric_rows, baseline_rows


def chart(path, title, entries):
    """Compact descriptive dot chart with zero reference and optional intervals."""
    height = 102 + max(1, len(entries)) * 25
    values = [v for _, _, value, low, high in entries for v in (value, low, high) if v is not None]
    extent = max((abs(value) for value in values), default=1) or 1
    x = lambda value: 520 + 260 * value / extent
    marks = [f'<svg xmlns="http://www.w3.org/2000/svg" width="920" height="{height}" '
             f'font-family="Arial" viewBox="0 0 920 {height}">',
             f'<rect width="920" height="{height}" fill="#101923"/>',
             f'<text x="24" y="32" fill="#e6eef7" font-size="21">{escape(title)}</text>',
             '<text x="24" y="56" fill="#bdc9d4" font-size="12">Values in % | n = complete windows</text>',
             f'<line x1="520" x2="520" y1="71" y2="{height - 25}" stroke="#7d8996"/>']
    for index, (label, n, value, low, high) in enumerate(entries):
        y = 87 + index * 25
        marks.append(f'<text x="24" y="{y + 4}" fill="#e6eef7" font-size="13">{escape(label)} (n={n})</text>')
        if low is not None and high is not None:
            marks.append(f'<line x1="{x(low):.2f}" x2="{x(high):.2f}" y1="{y}" y2="{y}" stroke="#9bc1fc"/>')
        if value is not None:
            marks.append(f'<circle cx="{x(value):.2f}" cy="{y}" r="4" fill="#9bc1fc"/>')
            marks.append(f'<text x="800" y="{y + 4}" fill="#e6eef7" font-size="13">{value:+.2f}%</text>')
    if not entries:
        marks.append('<text x="24" y="91" fill="#bdc9d4" font-size="13">No observations</text>')
    marks.append("</svg>")
    with path.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(marks))


def charts(directory, metrics, baselines):
    directory.mkdir()
    close = METRICS[2]
    for metric in ("max_displacement_atr", "max_swing_atr", "range_break"):
        rows = [row for row in metrics if row["metric"] == metric
                and row["symbol"] == "all" and row["direction"] == "all"]
        chart(directory / f"{metric}.svg", f"Mean directional return by {metric.replace('_', ' ')}", [
            (f"{row['horizon']} bars / {row['band']}", row["complete"], row[f"mean_{close}"],
             row["mean_return_ci_low"], row["mean_return_ci_high"])
            for row in sorted(rows, key=lambda row: (row["horizon"], row["band"]))
        ])
    pooled = [row for row in baselines if row["baseline"] == "all_groups"
              and row["symbol"] == "all" and row["direction"] == "all"]
    chart(directory / "excursions.svg", "Median favorable and adverse excursions", [
        (f"{row['horizon']} bars / {label}", row["complete"],
         sign * row[f"median_{metric}"] if row[f"median_{metric}"] is not None else None,
         None, None)
        for row in pooled for label, sign, metric in (("favorable", 1, METRICS[0]),
                                                     ("adverse", -1, METRICS[1]))
    ])
    chart(directory / "symbol_direction.svg", "Mean 24-bar directional return by symbol and direction", [
        (f"{row['symbol']} / {row['direction']}", row["complete"], row[f"mean_{close}"],
         row["mean_return_ci_low"], row["mean_return_ci_high"])
        for row in baselines if row["baseline"] == "all_groups" and row["horizon"] == 24
        and row["symbol"] != "all" and row["direction"] != "all"
    ])


def evaluate(args):
    lock = json.loads(args.freeze.read_text())
    current = fingerprint()
    if current != lock["fingerprint"]:
        raise ValueError("Implementation differs from the frozen fingerprint")
    validate_window(args.start, args.end, lock)
    entries = [item.split("=", 1) for item in args.source]
    sources = dict(entries)
    if len(entries) != len(SYMBOLS) or set(sources) != set(SYMBOLS):
        raise ValueError("Supply exactly one prepared 4H CSV for each of ADA, BNB, ETH and SOL")
    selected, manifest = {}, {}
    for symbol, source in sorted(sources.items()):
        path = Path(source)
        bars, _ = load_candles(path, symbol, "4h", args.end)
        selected[symbol] = validation_bars(bars, args.start, args.end)
        manifest[symbol] = dict(path=str(path.resolve()), sha256=checksum(path),
                                prepared_bars=len(bars), first_open=bars[0].timestamp.isoformat(),
                                last_close=bars[-1].closed_at.isoformat(), provenance=args.provenance)
    args.output.mkdir(parents=True, exist_ok=False)
    prepared = args.output / "prepared"
    prepared.mkdir()
    groups, details, forward, baseline, baseline_events = [], [], [], [], []
    for symbol, bars in selected.items():
        source = prepared / f"{symbol}_4h.csv"
        write_candles(source, bars)
        directory = args.output / symbol
        manifest[symbol]["replay_sha256"] = checksum(source)
        run_replay(SimpleNamespace(
            input=source, symbol=symbol, source_timeframe="4h", as_of=args.end,
            drop_incomplete_final=False, start=args.start, end=args.end, output=directory,
            **lock["parameters"],
        ))
        pairs, outcomes, _ = analyze_run(directory, symbol)
        cohort = [event for event, _ in pairs]
        groups.extend(cohort)
        details.extend(evidence for _, evidence in pairs)
        forward.extend(outcomes)
        draws = list(sample_baseline(bars, cohort, args.start))
        baseline_events.extend(draws)
        baseline.extend(evaluate_groups(bars, draws, read_rows(directory / "choch_candidates.csv")))
    metrics, comparisons = stratify(groups, forward, baseline, lock["bands"])
    write_json(args.output / "dataset_manifest.json", manifest)
    write_json(args.output / "algorithm_fingerprint.json", lock)
    write_rows(args.output / "grouped_events.csv", GROUP_COLUMNS, groups)
    write_json(args.output / "grouped_event_details.json", details)
    write_rows(args.output / "forward_outcomes.csv", FORWARD_COLUMNS, forward)
    write_rows(args.output / "baseline_forward_outcomes.csv", FORWARD_COLUMNS, baseline)
    write_rows(args.output / "baseline_candles.csv",
               ("event_id", "symbol", "direction", "event_timestamp", "break_close", "range_break"),
               baseline_events)
    stat_columns = tuple(summarize([]))
    write_rows(args.output / "individual_metrics.csv",
               ("metric", "band", "symbol", "direction", "horizon", *stat_columns), metrics)
    write_rows(args.output / "baseline_comparison.csv",
               ("baseline", "symbol", "direction", "horizon", *stat_columns), comparisons)
    write_json(args.output / "summary.json", dict(
        status="evaluated", evaluation_start=args.start.isoformat(), evaluation_end=args.end.isoformat(),
        warmup_start=(args.start - WARMUP_BARS * FOUR_HOURS).isoformat(),
        grouped_events=len(groups), groups_by_symbol=dict(Counter(row["symbol"] for row in groups)),
        baseline_candles=len(baseline_events), bands=lock["bands"],
        interval_method="Pooled UTC calendar-week block bootstrap; intervals require six nonempty weeks",
        outcomes=[row for row in comparisons if row["symbol"] == "all" and row["direction"] == "all"],
    ))
    charts(args.output / "charts", metrics, comparisons)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--development", type=Path, required=True)
    freeze_parser.add_argument("--output", type=Path, required=True)
    run_parser = sub.add_parser("evaluate")
    run_parser.add_argument("--freeze", type=Path, required=True)
    run_parser.add_argument("--source", action="append", required=True, metavar="SYMBOL=CSV")
    run_parser.add_argument("--start", type=utc, required=True)
    run_parser.add_argument("--end", type=utc, required=True)
    run_parser.add_argument("--provenance", required=True, help="How the input period was kept untouched")
    run_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        lock, _ = freeze(args.development, args.output)
        print(f"Frozen SHA-256: {lock['fingerprint']['sha256']}")
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
