"""Offline unit tests for scalper_v3_strategy.py -- no network calls."""

import pandas as pd
import pytest

import scalper_v3_strategy as v3


@pytest.fixture(autouse=True)
def _isolate_state():
    v3.reset_state()
    yield
    v3.reset_state()


# ── valid_v3_geometry ──────────────────────────────────────────────────

def test_geometry_long_valid():
    assert v3.valid_v3_geometry("LONG", entry=100, sl=97, tp1=103, tp2=106)


def test_geometry_long_invalid_sl_above_entry():
    assert not v3.valid_v3_geometry("LONG", entry=100, sl=101, tp1=103, tp2=106)


def test_geometry_short_valid():
    assert v3.valid_v3_geometry("SHORT", entry=100, sl=103, tp1=97, tp2=94)


def test_geometry_rejects_nonpositive():
    assert not v3.valid_v3_geometry("LONG", entry=0, sl=97, tp1=103, tp2=106)


# ── walk_trade ───────────────────────────────────────────────────────

def test_walk_trade_long_straight_loss():
    bars = pd.DataFrame({
        "high": [100.5, 100.2],
        "low": [96.5, 95.0],
        "close": [97.0, 95.5],
        "supertrend": [97.0, 96.0],
    })
    result = v3.walk_trade("LONG", entry_price=100, initial_sl=97, tp1_price=105, tp2_price=110, bars=bars)
    assert result["status"] == "loss"
    assert result["exit_reason"] == "sl"
    assert not result["tp1_hit"]


def test_walk_trade_long_tp1_then_breakeven_is_a_win():
    bars = pd.DataFrame({
        "high": [101, 103, 106, 100.5],
        "low": [99, 100, 104, 99.9],
        "close": [100, 102, 105, 100.0],
        "supertrend": [97, 98, 100, 100],
    })
    result = v3.walk_trade("LONG", entry_price=100, initial_sl=97, tp1_price=103, tp2_price=110, bars=bars)
    assert result["status"] == "win"
    assert result["exit_reason"] == "breakeven"
    assert result["tp1_hit"]
    assert result["exit_price"] == 100


def test_walk_trade_long_tp2_win():
    bars = pd.DataFrame({
        "high": [103, 112], "low": [100, 108], "close": [102, 111], "supertrend": [98, 101],
    })
    result = v3.walk_trade("LONG", 100, 97, 103, 110, bars)
    assert result["status"] == "win" and result["exit_reason"] == "tp2"


def test_walk_trade_short_tp2_win():
    bars = pd.DataFrame({
        "high": [100, 92], "low": [97, 88], "close": [98, 89], "supertrend": [102, 99],
    })
    result = v3.walk_trade("SHORT", 100, 103, 97, 90, bars)
    assert result["status"] == "win" and result["exit_reason"] == "tp2"


def test_walk_trade_same_bar_tie_break_favors_stop():
    # A single bar that touches both SL and TP1 -- SL must win (conservative).
    bars = pd.DataFrame({
        "high": [104], "low": [96], "close": [100], "supertrend": [97],
    })
    result = v3.walk_trade("LONG", entry_price=100, initial_sl=97, tp1_price=103, tp2_price=110, bars=bars)
    assert result["status"] == "loss"
    assert result["exit_reason"] == "sl"


def test_walk_trade_still_pending_when_no_bars_hit():
    bars = pd.DataFrame({"high": [101], "low": [99], "close": [100], "supertrend": [97]})
    result = v3.walk_trade("LONG", 100, 97, 110, 120, bars)
    assert result["status"] == "pending"


def test_walk_trade_no_lookahead_trail_uses_previous_bar():
    # Bar 0's own supertrend jumps very high, but that must NOT protect
    # bar 0 itself -- only bar 1 onward should see the tightened stop.
    bars = pd.DataFrame({
        "high": [100.5, 100.5],
        "low": [96.0, 99.5],       # bar 0 dips to 96, below the *initial* SL of 97
        "close": [97.0, 100.0],
        "supertrend": [99.5, 99.5],  # bar 0's own (post-close) supertrend value
    })
    result = v3.walk_trade("LONG", entry_price=100, initial_sl=97, tp1_price=110, tp2_price=120, bars=bars)
    # bar 0 must be judged against initial_sl=97 (not the 99.5 computed from bar 0's own close)
    assert result["status"] == "loss"
    assert result["bars_held"] == 1


# ── evaluate_symbol_v3 via a monkeypatched engine (deterministic) ──────

class _FakeEngine:
    def __init__(self, sig):
        self._sig = sig

    def compute(self, df):
        return df

    def latest_signal(self, df):
        return self._sig

    def confluence_ok(self, sig, min_strength=3):
        from super_scalper_v3 import SuperScalper
        return SuperScalper(**v3.scalper_kwargs()).confluence_ok(sig, min_strength=min_strength)


def _base_sig(**overrides):
    sig = dict(
        side="BUY", trend="BULLISH", strength=5, ao=1.0, ao_rising=True,
        kc_pos=0.2, kc_slope=0.2, kc_mid=101.0, kc_upper=104.0, kc_lower=98.0,
        adx=30.0, chop=30.0, expansion=1.5, regime="TRENDING", regime_votes=3,
        stop_loss=98.5, price=100.0,
    )
    sig.update(overrides)
    return sig


def _dummy_df(n=120):
    return pd.DataFrame({
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.0] * n, "volume": [1000.0] * n,
    })


def test_evaluate_returns_none_when_no_flip(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: None)
    v3._engines["FOO_USDT"] = _FakeEngine(_base_sig(side=None))
    result = v3.evaluate_symbol_v3("FOO_USDT", df=_dummy_df())
    assert result is None


def test_evaluate_returns_signal_when_confluence_passes(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: None)
    v3._engines["FOO_USDT"] = _FakeEngine(_base_sig())
    result = v3.evaluate_symbol_v3("FOO_USDT", df=_dummy_df())
    assert isinstance(result, v3.ScalperV3Signal)
    assert result.direction == "LONG"
    assert result.sl_price == 98.5
    assert result.tp1_price == 101.0
    assert result.tp2_price == 104.0
    assert v3.valid_v3_geometry(result.direction, result.entry_price, result.sl_price, result.tp1_price, result.tp2_price)


def test_evaluate_logs_skip_when_regime_ranging(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: None)
    v3._engines["FOO_USDT"] = _FakeEngine(_base_sig(regime="RANGING", regime_votes=0))
    result = v3.evaluate_symbol_v3("FOO_USDT", df=_dummy_df())
    assert isinstance(result, v3.SkippedSignal)
    assert result.reason == "regime_ranging"


def test_evaluate_logs_skip_when_kc_pos_outside_zone(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: None)
    v3._engines["FOO_USDT"] = _FakeEngine(_base_sig(kc_pos=0.9))  # too high for a LONG entry zone
    result = v3.evaluate_symbol_v3("FOO_USDT", df=_dummy_df())
    assert isinstance(result, v3.SkippedSignal)
    assert result.reason == "kc_pos_outside_entry_zone"


def test_evaluate_blocked_by_funding_filter(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: {"fair_price": 100.0, "hold_vol": 0.0, "funding_rate": 0.01})
    v3._engines["FOO_USDT"] = _FakeEngine(_base_sig())  # confluence passes
    result = v3.evaluate_symbol_v3("FOO_USDT", df=_dummy_df())
    assert isinstance(result, v3.SkippedSignal)
    assert result.reason.startswith("funding:")


def test_evaluate_blocked_by_liq_estimator(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: None)
    v3._engines["FOO_USDT"] = _FakeEngine(_base_sig())

    class _FakeLiq:
        def significant_clusters(self, price):
            return [(98.7, "long", 500_000.0)]  # sits between entry(100) and sl(98.5)

    v3.register_liq_estimator("FOO_USDT", _FakeLiq())
    result = v3.evaluate_symbol_v3("FOO_USDT", df=_dummy_df())
    assert isinstance(result, v3.SkippedSignal)
    assert result.reason.startswith("liq_estimator:")


def test_funding_filter_fails_open_when_ticker_missing(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: None)
    ok, funding, reason = v3._funding_filter_ok("FOO_USDT", "LONG")
    assert ok is True and reason == ""


def test_funding_filter_blocks_crowded_long(monkeypatch):
    monkeypatch.setattr(v3, "get_ticker", lambda s: {"fair_price": 1, "hold_vol": 1, "funding_rate": 0.001})
    ok, funding, reason = v3._funding_filter_ok("FOO_USDT", "LONG")
    assert ok is False
    assert "crowded long" in reason
