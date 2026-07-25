"""Unit tests for backtest/relative_strength.py -- pure functions, no network, no real data."""

import math

import pandas as pd
import pytest

from backtest.relative_strength import compute_returns, rs_signal


def test_compute_returns_basic():
    close = pd.Series([100.0, 101.0, 102.0, 105.0])
    result = compute_returns(close, lookback_bars=2)
    # bar 2: (102-100)/100*100 = 2.0%   bar 3: (105-101)/101*100 ~= 3.9604%
    assert math.isnan(result.iloc[0])
    assert math.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[3] == pytest.approx((105.0 - 101.0) / 101.0 * 100.0)


def test_rs_signal_long_when_btc_up_and_alt_outperforms():
    # BTC +2% (above 1% move threshold), alt +5% (rs_score=3%, above 1% rs threshold)
    assert rs_signal(btc_return=2.0, alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) == "LONG"


def test_rs_signal_short_when_btc_down_and_alt_underperforms():
    # BTC -2%, alt -5% (rs_score=-3%, below -1% rs threshold)
    assert rs_signal(btc_return=-2.0, alt_return=-5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) == "SHORT"


def test_rs_signal_none_when_btc_move_too_small():
    # BTC only +0.2%, below the 1% move threshold -- no signal regardless of alt
    assert rs_signal(btc_return=0.2, alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_none_when_alt_not_outperforming_enough():
    # BTC +2% (passes move threshold), alt +2.5% -- rs_score=0.5%, below 1% rs threshold
    assert rs_signal(btc_return=2.0, alt_return=2.5, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_none_when_alt_underperforms_during_btc_uptrend():
    # BTC +2%, alt only +1% -- rs_score=-1%, negative, but BTC is UP so LONG needs positive rs_score
    assert rs_signal(btc_return=2.0, alt_return=1.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_none_on_nan_inputs():
    assert rs_signal(btc_return=float("nan"), alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None
    assert rs_signal(btc_return=2.0, alt_return=float("nan"), move_threshold_pct=1.0, rs_threshold_pct=1.0) is None


def test_rs_signal_boundary_is_exclusive():
    # Exactly AT the move threshold should not qualify (require strictly greater)
    assert rs_signal(btc_return=1.0, alt_return=5.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None
    # Exactly AT the rs threshold should not qualify
    assert rs_signal(btc_return=2.0, alt_return=3.0, move_threshold_pct=1.0, rs_threshold_pct=1.0) is None
