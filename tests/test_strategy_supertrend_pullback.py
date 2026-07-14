import numpy as np

import strategy
from strategy import evaluate_symbol, valid_trade_geometry
from tests.strategy_fixtures import make_15m_trend_df, make_5m_pullback_df, patch_klines


def test_long_signal_valid(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= 1.5
    assert sig.score > 0.0


def test_long_trade_geometry(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("LONG", sig.entry_price, sig.tp_price, sig.sl_price)


def test_invalid_geometry_rejected():
    assert valid_trade_geometry("LONG", 100.0, 99.0, 101.0) is False
    assert valid_trade_geometry("SHORT", 100.0, 101.0, 99.0) is False
    assert valid_trade_geometry("LONG", 0.0, 101.0, 99.0) is False


def test_risk_formula_matches_roi_targets():
    from config import TP_PRICE_PCT, MAX_SL_PRICE_PCT
    import pytest

    assert TP_PRICE_PCT == pytest.approx(0.0075, abs=1e-9)
    assert MAX_SL_PRICE_PCT == pytest.approx(0.005, abs=1e-9)


def test_long_rejected_without_15m_trend(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")   # wrong-direction 15m trend
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_without_pullback(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    # A pure uptrend 5m series never dips through EMA20 -- no pullback.
    df_5m = make_15m_trend_df("LONG", bars=60).rename_axis(None)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_rsi_too_high(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    # NOTE: this fixture is intended to exercise the RSI-too-high rejection,
    # but as constructed it actually rejects earlier via the "no prior
    # uptrend above EMA20 before pullback" gate (confirmed by direct execution
    # during review). The assertion below still correctly verifies
    # evaluate_symbol rejects this candidate; it does not isolate the RSI gate
    # specifically. A fixture redesign (recomputing the confirmation candle after
    # mutating history) would be needed to isolate the RSI gate, which is
    # out of scope for numeric-constant tuning.
    df_5m = make_5m_pullback_df("LONG", dip_depth=0.2)
    df_5m.loc[df_5m.index[:-6], "close"] = (
        100.0 + 0.4 * np.arange(len(df_5m) - 6)
    )
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_volume_too_low(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG", confirm_volume_mult=1.05)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_candle_too_large(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG", confirm_body=8.0)
    df_5m.iloc[-2, df_5m.columns.get_loc("high")] += 6.0
    df_5m.iloc[-2, df_5m.columns.get_loc("low")] -= 6.0
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_stop_too_wide(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG", dip_depth=6.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_rr_too_low(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    # TP_PRICE_PCT is fixed and MAX_SL_PRICE_PCT caps SL, so the naturally
    # achievable RR sits at or above MIN_RR by construction; raise the bar
    # above whatever this fixture achieves to exercise the RR gate itself.
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None


def test_active_last_candle_is_ignored(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    # Corrupt only the forming (last, duplicated) candle so it alone would
    # break the setup if it were read -- evaluate_symbol must still fire
    # using the last COMPLETED candle underneath it.
    df_5m.iloc[-1, df_5m.columns.get_loc("close")] = 1.0
    df_5m.iloc[-1, df_5m.columns.get_loc("high")] = 1.0
    df_5m.iloc[-1, df_5m.columns.get_loc("low")] = 0.5
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")
    assert sig is not None
    assert sig.direction == "LONG"


def test_short_signal_valid(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price
    assert sig.rr >= 1.5


def test_short_trade_geometry(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("SHORT", sig.entry_price, sig.tp_price, sig.sl_price)


def test_short_rejected_without_15m_trend(monkeypatch):
    df_15m = make_15m_trend_df("LONG")   # wrong-direction 15m trend
    df_5m = make_5m_pullback_df("SHORT")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_without_pullback(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_15m_trend_df("SHORT", bars=60).rename_axis(None)  # pure downtrend, no pullback
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_when_rsi_too_low(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    # NOTE: mirrors test_long_rejected_when_rsi_too_high -- as constructed this
    # actually rejects earlier via the "no prior downtrend below EMA20 before
    # pullback" gate (confirmed by direct execution during review), not the
    # RSI gate specifically. The assertion still correctly verifies
    # evaluate_symbol rejects this candidate.
    df_5m = make_5m_pullback_df("SHORT", dip_depth=0.2)
    df_5m.loc[df_5m.index[:-6], "close"] = (
        100.0 - 0.4 * np.arange(len(df_5m) - 6)
    )
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    result = evaluate_symbol("TEST_USDT")
    assert result is None


def test_short_rejected_when_volume_too_low(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT", confirm_volume_mult=1.05)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_when_candle_too_large(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT", confirm_body=8.0)
    df_5m.iloc[-2, df_5m.columns.get_loc("high")] += 6.0
    df_5m.iloc[-2, df_5m.columns.get_loc("low")] -= 6.0
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_when_stop_too_wide(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT", dip_depth=6.0)
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    assert evaluate_symbol("TEST_USDT") is None


def test_short_rejected_when_rr_too_low(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None

