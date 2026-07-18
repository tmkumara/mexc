"""Offline unit tests for backtest/engine.py -- no network, no real data."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest import engine as bt
import scalper_v3_strategy as v3s


def _flat_row(i, price=100.0, side=None, regime="RANGING", regime_votes=0, **overrides):
    row = dict(
        open=price, high=price * 1.001, low=price * 0.999, close=price, volume=100.0,
        supertrend=price * 0.97, trend=1, signal=side,
        kc_mid=price, kc_upper=price * 1.03, kc_lower=price * 0.97,
        kc_pos=0.5, kc_slope=0.0, ao=0.0, ao_rising=True,
        strength=0, adx=15.0, chop=60.0, expansion=1.0,
        regime=regime, regime_votes=regime_votes,
    )
    row.update(overrides)
    return row


def _build_computed(n=400, freq="5min"):
    idx = pd.date_range("2026-01-01", periods=n, freq=freq)
    rows = [_flat_row(i) for i in range(n)]
    return pd.DataFrame(rows, index=idx)


class _FakeEngine:
    """Wraps the real SuperScalper.confluence_ok (unmodified) but serves a
    pre-baked computed DataFrame instead of running compute() on price
    data -- lets tests pin exact flip/confluence bars deterministically."""

    def __init__(self, computed_df, **kwargs):
        self._computed = computed_df
        self._real = __import__("super_scalper_v3").SuperScalper(**kwargs)

    def compute(self, df):
        return self._computed

    def confluence_ok(self, sig, min_strength=3):
        return self._real.confluence_ok(sig, min_strength=min_strength)

    def pullback_entry_ok(self, sig):
        return self._real.pullback_entry_ok(sig)


@pytest.fixture
def params():
    return bt.BacktestParams(
        scalper_kwargs=v3s.scalper_kwargs(), min_strength=3,
        initial_equity=10_000.0, risk_pct=0.01, taker_fee_pct=0.0,
        slippage_ticks=0.0, entry_timeframe="5m", warmup_bars=50,
    )


def _dummy_1m(n_minutes=400 * 5 + 50):
    idx = pd.date_range("2026-01-01", periods=n_minutes, freq="min")
    return pd.DataFrame({"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1.0}, index=idx)


def test_confluence_pass_lands_in_filtered_book(monkeypatch, params):
    computed = _build_computed()
    # bar 100: clean BUY flip with full confluence (matches super_scalper_v3's LONG rule)
    computed.iloc[100] = pd.Series(_flat_row(
        100, price=100.0, side="BUY", trend=1, regime="TRENDING", regime_votes=3,
        kc_pos=0.2, kc_slope=0.2, ao=1.0, ao_rising=True, strength=5, supertrend=97.0,
        kc_mid=101.0, kc_upper=105.0,
    ))
    # subsequent bars: price runs to TP2 (105) so the trade resolves inside the window
    for j in range(101, 110):
        computed.iloc[j] = pd.Series(_flat_row(j, price=101.0 + j, side=None, supertrend=98.0 + j * 0.1))
    computed.loc[computed.index[105], "high"] = 106.0  # ensure TP2 (105) is crossed

    monkeypatch.setattr(bt, "SuperScalper", lambda **kw: _FakeEngine(computed, **kw))
    result = bt.run_backtest(_dummy_1m(), "FAKE_USDT", params)

    assert len(result.trades) == 1
    assert result.trades[0].direction == "LONG"
    assert result.trades[0].taken is True
    assert result.trades[0].exit_reason == "tp2"
    assert result.trades[0] in result.all_flip_trades
    assert len(result.skipped_trades) == 0


def test_confluence_fail_lands_only_in_baseline_book(monkeypatch, params):
    computed = _build_computed()
    # bar 100: BUY flip but kc_pos way outside the entry zone -> confluence_ok() False
    computed.iloc[100] = pd.Series(_flat_row(
        100, price=100.0, side="BUY", trend=1, regime="TRENDING", regime_votes=3,
        kc_pos=0.95, kc_slope=0.2, ao=1.0, ao_rising=True, strength=5, supertrend=97.0,
        kc_mid=101.0, kc_upper=105.0,
    ))
    for j in range(101, 110):
        # price=99 sits strictly between SL(97) and TP1(101) so filler bars don't
        # accidentally trigger either before the deliberate SL breach below.
        computed.iloc[j] = pd.Series(_flat_row(j, price=99.0, side=None, supertrend=98.0))
    computed.loc[computed.index[103], "low"] = 90.0  # SL(97) hit -> loss

    monkeypatch.setattr(bt, "SuperScalper", lambda **kw: _FakeEngine(computed, **kw))
    result = bt.run_backtest(_dummy_1m(), "FAKE_USDT", params)

    assert len(result.trades) == 0
    assert len(result.all_flip_trades) == 1
    assert len(result.skipped_trades) == 1
    assert result.skipped_trades[0].skip_reason == "kc_pos_outside_entry_zone"
    assert result.skipped_trades[0].exit_reason == "sl"


def test_filtered_book_stays_flat_until_position_closes(monkeypatch, params):
    computed = _build_computed()
    # Two BUY flips close together; the second should be skipped by the
    # filtered book's exclusivity (still in the first trade) even though
    # both individually pass confluence_ok().
    for bar in (100, 102):
        computed.iloc[bar] = pd.Series(_flat_row(
            bar, price=100.0, side="BUY", trend=1, regime="TRENDING", regime_votes=3,
            kc_pos=0.2, kc_slope=0.2, ao=1.0, ao_rising=True, strength=5, supertrend=97.0,
            kc_mid=101.0, kc_upper=105.0,
        ))
    # Keep price pinned between SL and TP1 for a long time so trade 1 never closes.
    for j in range(101, 200):
        if j in (100, 102):
            continue
        computed.iloc[j] = pd.Series(_flat_row(j, price=100.0, side=None, supertrend=97.0,
                                                 high=100.5, low=99.5, kc_mid=101.0, kc_upper=105.0))

    monkeypatch.setattr(bt, "SuperScalper", lambda **kw: _FakeEngine(computed, **kw))
    result = bt.run_backtest(_dummy_1m(n_minutes=250 * 5 + 50), "FAKE_USDT", params)

    # Only the first flip should have opened a filtered-book trade.
    assert len(result.trades) == 1
    assert result.trades[0].entry_time == computed.index[101]
    # both flips still register in the all-flip baseline book (its own exclusivity is independent)
    assert len(result.all_flip_trades) == 1  # second flip at bar 102 also blocked (baseline book also flat-until-close, same first trade still open)


def test_pullback_entry_fires_without_a_flip(monkeypatch, params):
    computed = _build_computed()
    # bar 100: NO flip (side=None), but an ongoing TRENDING bullish pullback setup.
    # price matches bar 101's fill (99.0) -- with the flat SL/TP bands sized off
    # the signal bar's own price (_calc_tp_sl), a bar-100-vs-bar-101 price gap
    # bigger than the band itself would make the computed SL/TP invalid relative
    # to the actual entry_idx+1 fill price, which real market data won't do at
    # this band width (0.5%) but this synthetic fixture could by accident.
    computed.iloc[100] = pd.Series(_flat_row(
        100, price=99.0, side=None, trend=1, regime="TRENDING", regime_votes=3,
        kc_pos=0.2, kc_slope=0.2, ao=1.0, ao_rising=True, supertrend=97.0,
        kc_mid=101.0, kc_upper=105.0,
    ))
    for j in range(101, 110):
        computed.iloc[j] = pd.Series(_flat_row(j, price=99.0, side=None, supertrend=98.0))
    computed.loc[computed.index[103], "high"] = 106.0  # TP2 win

    monkeypatch.setattr(bt, "SuperScalper", lambda **kw: _FakeEngine(computed, **kw))
    result = bt.run_backtest(_dummy_1m(), "FAKE_USDT", params)

    assert len(result.trades) == 1
    assert result.trades[0].entry_kind == "pullback"
    assert result.trades[0].direction == "LONG"
    assert result.trades[0].exit_reason == "tp2"
    assert len(result.pullback_trades) == 1
    assert len(result.flip_trades) == 0
    # pullback has no baseline/skipped counterpart -- shouldn't appear in the flip-only baseline book
    assert len(result.all_flip_trades) == 0


def test_compute_metrics_empty():
    m = bt.compute_metrics([], 10_000.0)
    assert m["total_trades"] == 0 and m["profit_factor"] is None


def test_compute_metrics_profit_factor_and_drawdown(monkeypatch, params):
    computed = _build_computed()
    computed.iloc[100] = pd.Series(_flat_row(
        100, price=100.0, side="BUY", trend=1, regime="TRENDING", regime_votes=3,
        kc_pos=0.2, kc_slope=0.2, ao=1.0, ao_rising=True, strength=5, supertrend=97.0,
        kc_mid=101.0, kc_upper=105.0,
    ))
    for j in range(101, 110):
        computed.iloc[j] = pd.Series(_flat_row(j, price=99.0, side=None, supertrend=98.0))
    computed.loc[computed.index[103], "high"] = 106.0  # TP2 win

    monkeypatch.setattr(bt, "SuperScalper", lambda **kw: _FakeEngine(computed, **kw))
    result = bt.run_backtest(_dummy_1m(), "FAKE_USDT", params)
    metrics = bt.compute_metrics(result.trades, params.initial_equity)
    assert metrics["total_trades"] == 1
    assert metrics["win_rate"] == 100.0
    assert metrics["profit_factor"] == float("inf")
    assert metrics["total_pnl"] > 0
    assert "2026-01" in metrics["monthly"]


def test_resample_ohlcv_no_lookahead_ordering():
    idx = pd.date_range("2026-01-01 00:01", periods=15, freq="min")  # start off a clean 5m boundary
    df = pd.DataFrame({
        "open": np.arange(15) + 100.0, "high": np.arange(15) + 100.5,
        "low": np.arange(15) + 99.5, "close": np.arange(15) + 100.2, "volume": 1.0,
    }, index=idx)
    out = bt.resample_ohlcv(df, "5m")
    assert len(out) == 3
    # first 5m bar's open must be the first 1m bar's open (not a later one)
    assert out["open"].iloc[0] == df["open"].iloc[0]
    assert out["close"].iloc[0] == df["close"].iloc[4]
    assert out["high"].iloc[0] == df["high"].iloc[0:5].max()
