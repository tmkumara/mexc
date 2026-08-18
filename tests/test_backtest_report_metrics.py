import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backtest_simple_strategy import Trade, BacktestStats


def _trade(outcome, net_roi, score=85.0, direction="LONG", entry_time="2026-03-01T10:00:00", hold_minutes=20.0):
    return Trade(
        symbol="XRP_USDT", direction=direction, entry_price=1.0, tp_price=1.01, sl_price=0.99,
        rr=0.7, outcome=outcome, gross_roi_pct=net_roi, net_roi_pct=net_roi,
        closed_at="2026-03-01T10:20:00", score=score, entry_time=entry_time, hold_minutes=hold_minutes,
    )


def test_profit_factor_is_gross_wins_over_gross_losses():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0))
    stats.add(_trade("win", 7.0))
    stats.add(_trade("loss", -10.0))
    metrics = stats.compute_metrics()
    assert metrics["profit_factor"] == 14.0 / 10.0


def test_expectancy_per_trade_matches_average_net_roi():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0))
    stats.add(_trade("loss", -10.0))
    metrics = stats.compute_metrics()
    assert metrics["expectancy_pct"] == (7.0 + -10.0) / 2


def test_score_bucket_breakdown_groups_correctly():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, score=82.0))
    stats.add(_trade("loss", -10.0, score=96.0))
    metrics = stats.compute_metrics()
    buckets = metrics["score_buckets"]
    assert buckets["80-84.9"]["trades"] == 1
    assert buckets["90-100"]["trades"] == 1


def test_long_short_expectancy_split_independently():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, direction="LONG"))
    stats.add(_trade("loss", -10.0, direction="SHORT"))
    metrics = stats.compute_metrics()
    assert metrics["long_expectancy_pct"] == 7.0
    assert metrics["short_expectancy_pct"] == -10.0


def test_holding_duration_avg_and_median():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, hold_minutes=10.0))
    stats.add(_trade("loss", -10.0, hold_minutes=30.0))
    metrics = stats.compute_metrics()
    assert metrics["avg_hold_minutes"] == 20.0
    assert metrics["median_hold_minutes"] == 20.0


def test_profit_factor_is_none_when_no_losses():
    stats = BacktestStats()
    stats.add(_trade("win", 7.0))
    metrics = stats.compute_metrics()
    assert metrics["profit_factor"] is None


def test_print_report_runs_without_error(capsys):
    stats = BacktestStats()
    stats.add(_trade("win", 7.0, direction="LONG", score=82.0))
    stats.add(_trade("loss", -10.0, direction="SHORT", score=96.0))
    stats.print_report()
    out = capsys.readouterr().out
    assert "Total trades:" in out
    assert "Profit factor:" in out
