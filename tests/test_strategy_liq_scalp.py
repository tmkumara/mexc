import numpy as np
import pandas as pd
import pytest

from liq_estimator import LiqEstimator
from strategy import _base_signal, _evaluate_liquidity, _valid_trade_geometry


def _build_df(pattern: list[float], n_bars: int = 60, base_vol: float = 100.0, spike_vol: float = 500.0) -> pd.DataFrame:
    deltas = (pattern * (n_bars // 3 + 1))[: n_bars - 1]
    closes = [100.0]
    for d in deltas:
        closes.append(closes[-1] + d)
    closes = np.array(closes)
    opens = np.concatenate(([closes[0]], closes[:-1]))
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    volumes = np.full(n_bars, base_vol)
    volumes[-1] = spike_vol
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})


def test_base_signal_detects_long_on_uptrend_with_volume_confirmation():
    df = _build_df([4, 4, -5])
    assert _base_signal(df) == "LONG"


def test_base_signal_detects_short_on_downtrend_with_volume_confirmation():
    df = _build_df([-4, -4, 5])
    assert _base_signal(df) == "SHORT"


def test_base_signal_none_on_flat_market():
    df = pd.DataFrame({
        "open": [100.0] * 60, "high": [100.5] * 60, "low": [99.5] * 60,
        "close": [100.0] * 60, "volume": [100.0] * 60,
    })
    assert _base_signal(df) is None


def test_base_signal_none_on_insufficient_history():
    df = _build_df([4, 4, -5], n_bars=30)
    assert _base_signal(df) is None


def _estimator_with_magnet_both_sides() -> LiqEstimator:
    est = LiqEstimator(
        leverage_tiers={20: 1.0},
        mmr_buffer=0.0,
        bucket_pct=0.0005,
        decay=1.0,
        lookaround_pct=0.06,
        min_percentile=0,
        account_leverage=20,
    )
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=3000.0, price=100.0)   # d_oi=2000 -> clusters at ~95 (long) and ~105 (short)
    return est


def test_evaluate_liquidity_long_passes_with_magnet_above():
    est = _estimator_with_magnet_both_sides()
    ok, tp, sl, reason = _evaluate_liquidity("LONG", 100.0, funding=0.0, estimator=est)
    assert ok is True
    assert tp == pytest.approx(100.6)
    assert sl == pytest.approx(99.68)
    assert "RR" in reason


def test_evaluate_liquidity_short_passes_with_magnet_below():
    est = _estimator_with_magnet_both_sides()
    ok, tp, sl, reason = _evaluate_liquidity("SHORT", 100.0, funding=0.0, estimator=est)
    assert ok is True
    assert tp == pytest.approx(99.4)
    assert sl == pytest.approx(100.32)


def test_evaluate_liquidity_vetoes_when_no_magnet():
    est = LiqEstimator(
        leverage_tiers={20: 1.0}, mmr_buffer=0.0, bucket_pct=0.0005,
        decay=1.0, lookaround_pct=0.06, min_percentile=0, account_leverage=20,
    )
    ok, tp, sl, reason = _evaluate_liquidity("LONG", 100.0, funding=0.0, estimator=est)
    assert ok is False
    assert tp is None and sl is None
    assert "no magnet" in reason


def test_evaluate_liquidity_vetoes_on_extreme_funding():
    est = _estimator_with_magnet_both_sides()
    ok, tp, sl, reason = _evaluate_liquidity("LONG", 100.0, funding=0.0005, estimator=est)
    assert ok is False
    assert "funding" in reason


def test_valid_trade_geometry():
    assert _valid_trade_geometry("LONG", entry=100.0, tp=101.0, sl=99.0) is True
    assert _valid_trade_geometry("LONG", entry=100.0, tp=99.0, sl=101.0) is False
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=99.0, sl=101.0) is True
    assert _valid_trade_geometry("SHORT", entry=100.0, tp=101.0, sl=99.0) is False
