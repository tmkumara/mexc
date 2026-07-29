import strategy
from strategy import evaluate_symbol, valid_trade_geometry
from tests.strategy_fixtures import make_ribbon_trendbar_df, patch_klines


def test_long_signal_valid(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    # Fixture's flip and Trend Bar confirmation land ~7 bars apart (by
    # design -- see strategy_fixtures docstring); production default
    # RIBBON_LOOKBACK_BARS is now 1 (exact-bar flip), so widen it here to
    # exercise the general lookback-window mechanism this test targets.
    monkeypatch.setattr(strategy, "RIBBON_LOOKBACK_BARS", 12)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "LONG"
    assert sig.tp_price > sig.entry_price > sig.sl_price
    assert sig.rr >= 1.5
    assert sig.score > 0.0


def test_long_trade_geometry(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "RIBBON_LOOKBACK_BARS", 12)

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


def test_long_rejected_without_ribbon_flip(monkeypatch):
    # A flat series never flips the ribbon.
    from tests.strategy_fixtures import make_15m_trend_df
    df = make_15m_trend_df("LONG", bars=200)
    patch_klines(monkeypatch, strategy, df)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_without_trend_bar_confirmation(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    # Force every bar gray -- ribbon may flip, Trend Bar never confirms.
    import pandas as pd
    monkeypatch.setattr(
        strategy, "calculate_trend_bar",
        lambda df, pac_length: pd.Series("gray", index=df.index, dtype=object),
    )

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_ribbon_reverts_before_confirmation(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    # Force _detect_ribbon_flip to report no current alignment (as if the
    # ribbon reverted before this scan) -- isolates the "reverted" path
    # without needing to hand-construct a revert-and-confirm fixture.
    monkeypatch.setattr(strategy, "_detect_ribbon_flip", lambda *a, **k: (None, None))

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_stop_too_wide(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "MAX_SL_PRICE_PCT", 1e-9)

    assert evaluate_symbol("TEST_USDT") is None


def test_long_rejected_when_rr_too_low(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None


def test_active_last_candle_is_ignored(monkeypatch):
    df = make_ribbon_trendbar_df("LONG")
    # Corrupt only the forming (last, duplicated) candle -- evaluate_symbol
    # must still fire using the last COMPLETED candle underneath it.
    df.iloc[-1, df.columns.get_loc("close")] = 1.0
    df.iloc[-1, df.columns.get_loc("high")] = 1.0
    df.iloc[-1, df.columns.get_loc("low")] = 0.5
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "RIBBON_LOOKBACK_BARS", 12)

    sig = evaluate_symbol("TEST_USDT")
    assert sig is not None
    assert sig.direction == "LONG"


def test_short_signal_valid(monkeypatch):
    df = make_ribbon_trendbar_df("SHORT")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "RIBBON_LOOKBACK_BARS", 12)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert sig.direction == "SHORT"
    assert sig.tp_price < sig.entry_price < sig.sl_price
    assert sig.rr >= 1.5


def test_short_trade_geometry(monkeypatch):
    df = make_ribbon_trendbar_df("SHORT")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "RIBBON_LOOKBACK_BARS", 12)

    sig = evaluate_symbol("TEST_USDT")

    assert sig is not None
    assert valid_trade_geometry("SHORT", sig.entry_price, sig.tp_price, sig.sl_price)


def test_short_rejected_when_rr_too_low(monkeypatch):
    df = make_ribbon_trendbar_df("SHORT")
    patch_klines(monkeypatch, strategy, df)
    monkeypatch.setattr(strategy, "MIN_RR", 50.0)

    assert evaluate_symbol("TEST_USDT") is None
