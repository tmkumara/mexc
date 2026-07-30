import pandas as pd
import pytest

import strategy
from strategy import _build_pending_setup, _score_pending_setup


def test_build_pending_setup_long():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.8, 100.8, 100.85],
        "high":  [100.9, 100.9, 101.0],
        "low":   [100.7, 100.8, 100.85],
        "close": [100.85, 100.85, 100.95],
        "volume": [1000.0] * 3,
    }, index=idx)

    setup = _build_pending_setup("XRP_USDT", "LONG", df)

    assert setup is not None
    high, prev_low = 101.0, 100.8
    expected_entry = high * (1 + 0.0002)
    assert setup["trigger_price"] == pytest.approx(expected_entry)
    assert setup["sl_price"] == pytest.approx(100.8)
    diff = (high - prev_low) * 2
    assert setup["tp_price"] == pytest.approx(high + diff, abs=1e-6)
    assert setup["tp2_price"] == pytest.approx(high + 2 * diff, abs=1e-6)
    assert setup["tp3_price"] == pytest.approx(high + 3 * diff, abs=1e-6)
    assert setup["rr"] >= 1.5


def test_build_pending_setup_short():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.85, 100.85, 100.75],
        "high":  [100.95, 100.9, 100.85],
        "low":   [100.75, 100.7, 100.6],
        "close": [100.8, 100.75, 100.65],
        "volume": [1000.0] * 3,
    }, index=idx)

    setup = _build_pending_setup("XRP_USDT", "SHORT", df)

    assert setup is not None
    prev_high, low = 100.9, 100.6
    expected_entry = low * (1 - 0.0002)
    assert setup["trigger_price"] == pytest.approx(expected_entry)
    assert setup["sl_price"] == pytest.approx(100.9)
    diff = (prev_high - low) * 2
    assert setup["tp_price"] == pytest.approx(low - diff, abs=1e-6)
    assert setup["tp2_price"] == pytest.approx(low - 2 * diff, abs=1e-6)
    assert setup["tp3_price"] == pytest.approx(low - 3 * diff, abs=1e-6)
    assert setup["rr"] >= 1.5


def test_pending_setup_rejected_when_stop_too_wide():
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.0, 100.0, 100.4],
        "high":  [100.1, 100.1, 101.2],
        "low":   [99.9, 100.0, 100.5],
        "close": [100.0, 100.05, 101.1],
        "volume": [1000.0] * 3,
    }, index=idx)
    # sl = min(prev_low=100.0, low=100.5) = 100.0; entry ~= 101.22 ->
    # sl distance ~1.2% of entry, well above MAX_SL_PRICE_PCT (0.5% default).
    setup = _build_pending_setup("XRP_USDT", "LONG", df, reject_sink={})
    assert setup is None


def test_pending_setup_rejected_when_rr_below_min():
    # Numeric constants here are reasoned, not hand-executed against
    # pandas -- same convention as tests/strategy_fixtures.py. If this
    # fails because RR lands >= MIN_RR instead of below it, widen the gap
    # between the swing distance and the (high-prev_low) diff further and
    # re-run -- expected TDD iteration, not a defect in the test itself.
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.7, 100.7, 100.8],
        "high":  [100.75, 100.75, 100.85],
        "low":   [100.65, 100.7, 100.57],
        "close": [100.72, 100.72, 100.7],
        "volume": [1000.0] * 3,
    }, index=idx)
    setup = _build_pending_setup("XRP_USDT", "LONG", df, reject_sink={})
    assert setup is None


def test_pending_setup_carries_position_size(monkeypatch):
    monkeypatch.setattr(strategy, "ACCOUNT_BALANCE", 10000.0)
    monkeypatch.setattr(strategy, "RISK_PERCENT_PER_TRADE", 1.0)
    idx = pd.date_range("2026-01-01", periods=3, freq="15min")
    df = pd.DataFrame({
        "open":  [100.8, 100.8, 100.85],
        "high":  [100.9, 100.9, 101.0],
        "low":   [100.7, 100.8, 100.85],
        "close": [100.85, 100.85, 100.95],
        "volume": [1000.0] * 3,
    }, index=idx)
    setup = _build_pending_setup("XRP_USDT", "LONG", df)
    assert setup is not None
    assert setup["position_size"] > 0.0


def test_score_pending_setup_within_bounds(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    monkeypatch.setattr(strategy, "SIGNAL_MODE", "confirmed")
    df = make_15m_trend_df("LONG", bars=220)
    score = _score_pending_setup("LONG", df, rr=2.0, mtf_confirmations=None)
    assert 0.0 <= score <= 100.0


def test_score_pending_setup_higher_rr_scores_higher(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    monkeypatch.setattr(strategy, "SIGNAL_MODE", "confirmed")
    df = make_15m_trend_df("LONG", bars=220)
    low_rr_score = _score_pending_setup("LONG", df, rr=strategy.MIN_RR, mtf_confirmations=None)
    high_rr_score = _score_pending_setup("LONG", df, rr=strategy.MIN_RR + 1.0, mtf_confirmations=None)
    assert high_rr_score > low_rr_score


from tests.strategy_fixtures import patch_klines
from strategy import detect_pending_setup


def test_detect_pending_setup_returns_none_on_missing_data(monkeypatch):
    monkeypatch.setattr(strategy, "get_market_klines", lambda *a, **k: pd.DataFrame())
    sink = {}
    assert detect_pending_setup("XRP_USDT", reject_sink=sink) is None
    assert sink.get("missing_data") == 1


def test_detect_pending_setup_returns_none_on_flat_series(monkeypatch):
    from tests.strategy_fixtures import make_15m_trend_df
    df = make_15m_trend_df("LONG", bars=260, start_price=100.0)
    # Force a perfectly flat series (no trend) by overwriting close with a
    # constant -- flat data never flips Chandelier direction cleanly on a
    # fresh transition, so no pending setup should ever be created.
    flat = df.copy()
    flat["close"] = 100.0
    flat["open"] = 100.0
    flat["high"] = 100.05
    flat["low"] = 99.95
    patch_klines(monkeypatch, strategy, flat)
    sink = {}
    assert detect_pending_setup("XRP_USDT", reject_sink=sink) is None
