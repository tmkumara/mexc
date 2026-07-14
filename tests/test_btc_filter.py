import strategy
from strategy import BtcContext, evaluate_symbol
from tests.strategy_fixtures import make_15m_trend_df, make_5m_pullback_df, patch_klines


def _bullish_btc() -> BtcContext:
    return BtcContext(
        close=50100.0, ema_200=49500.0, supertrend_direction=1,
        one_candle_move_pct=0.1, three_candle_move_pct=0.3,
    )


def _bearish_btc() -> BtcContext:
    return BtcContext(
        close=49500.0, ema_200=50100.0, supertrend_direction=-1,
        one_candle_move_pct=-0.1, three_candle_move_pct=-0.3,
    )


def _extreme_single_candle_btc() -> BtcContext:
    return BtcContext(
        close=50100.0, ema_200=49500.0, supertrend_direction=1,
        one_candle_move_pct=0.9, three_candle_move_pct=0.3,
    )


def test_long_allowed_when_btc_bullish(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT", btc_context=_bullish_btc())
    assert sig is not None


def test_long_blocked_when_btc_bearish(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT", btc_context=_bearish_btc())
    assert sig is None


def test_short_allowed_when_btc_bearish(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT", btc_context=_bearish_btc())
    assert sig is not None


def test_short_blocked_when_btc_bullish(monkeypatch):
    df_15m = make_15m_trend_df("SHORT")
    df_5m = make_5m_pullback_df("SHORT")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT", btc_context=_bullish_btc())
    assert sig is None


def test_signal_blocked_during_extreme_btc_move(monkeypatch):
    df_15m = make_15m_trend_df("LONG")
    df_5m = make_5m_pullback_df("LONG")
    patch_klines(monkeypatch, strategy, df_15m, df_5m)

    sig = evaluate_symbol("TEST_USDT", btc_context=_extreme_single_candle_btc())
    assert sig is None


def test_btc_active_candle_is_ignored(monkeypatch):
    df_btc = make_15m_trend_df("LONG", bars=220)
    # Corrupt only the forming (last, duplicated) candle -- build_btc_context
    # must still produce a clean bullish context from the completed candles
    # underneath it.
    df_btc.iloc[-1, df_btc.columns.get_loc("close")] = 1.0

    def _fake(symbol, interval, count=100):
        assert symbol == strategy.BTC_FILTER_SYMBOL
        return df_btc

    monkeypatch.setattr(strategy, "get_market_klines", _fake)

    ctx = strategy.build_btc_context()
    assert ctx is not None
    assert ctx.supertrend_direction == 1
    assert ctx.close > ctx.ema_200
