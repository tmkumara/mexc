from tests.strategy_fixtures import (
    make_zero_lag_trend_df, make_zero_lag_pullback_df, patch_klines_multi,
)
from strategy import detect_pending_setup
from config import MACRO_TF, TREND_TF, PULLBACK_TF, ENTRY_TF


def _pipeline_dfs(direction: str) -> dict:
    return {
        MACRO_TF: make_zero_lag_trend_df(direction, bars=320, freq="4h"),
        TREND_TF: make_zero_lag_trend_df(direction, bars=320, freq="1h"),
        PULLBACK_TF: make_zero_lag_pullback_df(direction, bars=100),
        ENTRY_TF: make_zero_lag_trend_df(direction, bars=100, freq="5min"),
    }


def test_pending_pullback_armed_on_long_pipeline_pass(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("LONG"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["direction"] == "LONG"
    assert setup["macro_trend"] == 1
    assert setup["trend_state"] == 1
    assert setup["score"] >= 30.0


def test_pending_pullback_armed_on_short_pipeline_pass(monkeypatch):
    import strategy
    patch_klines_multi(monkeypatch, strategy, _pipeline_dfs("SHORT"))

    setup = detect_pending_setup("XRP_USDT")

    assert setup is not None
    assert setup["direction"] == "SHORT"
    assert setup["macro_trend"] == -1


def test_rejected_when_macro_and_trend_disagree(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    dfs[TREND_TF] = make_zero_lag_trend_df("SHORT", bars=320, freq="1h")
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_trend_agreement") == 1


def test_rejected_when_outside_pullback_distance(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    far_pullback = make_zero_lag_pullback_df("LONG", bars=100)
    far_pullback.iloc[-1, far_pullback.columns.get_loc("close")] *= 1.02  # 2% away, well outside 0.10% band
    far_pullback.iloc[-2, far_pullback.columns.get_loc("close")] *= 1.02
    dfs[PULLBACK_TF] = far_pullback
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("no_pullback") == 1


def test_rejected_when_insufficient_history(monkeypatch):
    import strategy
    dfs = _pipeline_dfs("LONG")
    dfs[MACRO_TF] = make_zero_lag_trend_df("LONG", bars=50, freq="4h")  # too short
    patch_klines_multi(monkeypatch, strategy, dfs)

    reject_sink = {}
    setup = detect_pending_setup("XRP_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("insufficient_history") == 1
