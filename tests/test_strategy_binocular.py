import numpy as np
import pandas as pd

import strategy
from strategy import evaluate_symbol, valid_trade_geometry
from tests.strategy_fixtures import make_15m_zone_df, make_5m_trigger_df, patch_klines


def test_detect_trigger_rejects_long_when_chandelier_bearish(monkeypatch):
    # Isolates the chandelier-direction gate: PVT/RSI/breakout all pass
    # (this fixture is already tuned in Step 5 to satisfy them for LONG),
    # but chandelier direction is forced bearish -- must not fire LONG.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    def _fake_chandelier(df, atr_period, multiplier):
        return pd.DataFrame(
            {
                "chandelier_long_stop": 0.0,
                "chandelier_short_stop": 0.0,
                "chandelier_direction": -1,
            },
            index=df.index,
        )

    monkeypatch.setattr(strategy, "calculate_chandelier_exit", _fake_chandelier)

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None


def test_detect_trigger_rejects_without_pvt_momentum(monkeypatch):
    # Isolates the PVT-vs-signal gate: chandelier direction comes from the
    # real (tuned-bullish) fixture, but PVT is forced below its signal.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    monkeypatch.setattr(
        strategy, "calculate_pvt",
        lambda df: pd.Series(np.linspace(10.0, 0.0, len(df)), index=df.index),
    )
    monkeypatch.setattr(
        strategy, "calculate_pvt_signal",
        lambda pvt, length, ma_type: pd.Series(5.0, index=pvt.index),
    )

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None
    assert "PVT" in reason


def test_detect_trigger_rejects_without_rsi_regime(monkeypatch):
    # Isolates the dual-RSI gate: chandelier/PVT come from the real
    # (tuned-bullish) fixture, but both RSI periods are forced equal.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    monkeypatch.setattr(
        strategy, "calculate_rsi",
        lambda series, period: pd.Series(50.0, index=series.index),
    )

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None
    assert "RSI regime" in reason


def test_detect_trigger_rejects_without_breakout_confirmation(monkeypatch):
    # Isolates the breakout-buffer gate: chandelier/PVT/RSI all pass, but
    # an impossibly large buffer means the close can never clear it.
    df_5m = make_5m_trigger_df("LONG", base_price=90.0).iloc[:-1]

    monkeypatch.setattr(strategy, "ENTRY_BUFFER_PCT", 10.0)

    direction, reason, details = strategy._detect_trigger(df_5m)
    assert direction is None
    assert "breakout confirmation" in reason


def test_long_signal_valid(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= 1.5
    assert sig.score > 0.0


def test_long_trade_geometry(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("LONG", sig.entry_price, sig.tp_price, sig.sl_price)


def test_invalid_geometry_rejected():
    assert valid_trade_geometry("LONG", 100.0, 99.0, 101.0) is False
    assert valid_trade_geometry("SHORT", 100.0, 101.0, 99.0) is False
    assert valid_trade_geometry("LONG", 0.0, 101.0, 99.0) is False


def test_long_rejected_without_zone_confluence(monkeypatch):
    # Zone sits at 50, but the 5m trigger fires around 90 -- no confluence.
    df_15m = make_15m_zone_df("LONG", zone_price=50.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_stop_too_wide(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MAX_SL_PRICE_PCT", 1e-9)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_rr_too_low(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None


def test_active_last_candle_is_ignored(monkeypatch):
    df_15m = make_15m_zone_df("LONG", zone_price=90.0)
    df_5m = make_5m_trigger_df("LONG", base_price=90.0)
    # Corrupt only the forming (last, duplicated) candle -- evaluate_symbol
    # must still fire using the last COMPLETED candle underneath it.
    df_5m.iloc[-1, df_5m.columns.get_loc("close")] = 1.0
    df_5m.iloc[-1, df_5m.columns.get_loc("high")] = 1.0
    df_5m.iloc[-1, df_5m.columns.get_loc("low")] = 0.5
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")
    assert sig is not None
    assert sig.direction == "LONG"


def test_short_signal_valid(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price
    assert sig.rr >= 1.5


def test_short_trade_geometry(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("SHORT", sig.entry_price, sig.tp_price, sig.sl_price)


def test_short_rejected_without_zone_confluence(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=150.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_when_rr_too_low(monkeypatch):
    df_15m = make_15m_zone_df("SHORT", zone_price=110.0)
    df_5m = make_5m_trigger_df("SHORT", base_price=110.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None


def test_risk_formula_matches_roi_targets():
    from config import TP_PRICE_PCT, MAX_SL_PRICE_PCT
    import pytest

    assert TP_PRICE_PCT == pytest.approx(0.0075, abs=1e-9)
    assert MAX_SL_PRICE_PCT == pytest.approx(0.005, abs=1e-9)
