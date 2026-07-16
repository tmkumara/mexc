import config_nw


def test_defaults_loaded():
    assert config_nw.NW_TREND_TF == "4h"
    assert config_nw.NW_ENTRY_TF == "1h"
    assert config_nw.NW_TREND_EMA_PERIOD == 200
    assert config_nw.NW_LOOKBACK_WINDOW == 8.0
    assert config_nw.NW_RELATIVE_WEIGHTING == 8.0
    assert config_nw.NW_KERNEL_SUM_WINDOW == 300
    assert config_nw.NW_CONFIRMATION_LOOKBACK_BARS == 5
    assert config_nw.NW_SWING_FRACTAL_BARS == 5
    assert config_nw.NW_SWING_LOOKBACK_BARS == 20
    assert config_nw.NW_RSI_PERIOD == 14
    assert config_nw.NW_RSI_LONG_MIN == 45
    assert config_nw.NW_RSI_LONG_MAX == 75
    assert config_nw.NW_RSI_SHORT_MIN == 25
    assert config_nw.NW_RSI_SHORT_MAX == 55
    assert config_nw.NW_VOLUME_MA_PERIOD == 20
    assert config_nw.NW_MIN_VOLUME_MULTIPLIER == 1.0
    assert config_nw.NW_ATR_PERIOD == 14
    assert config_nw.NW_SL_ATR_BUFFER_MULTIPLIER == 0.5
    assert config_nw.LEVERAGE == 20
    assert config_nw.TARGET_ROI_PCT == 15.0
    assert config_nw.MAX_SL_ROI_PCT == 20.0
    assert config_nw.MIN_RR == 1.5
    assert config_nw.NW_ENABLE_BTC_FILTER is True
    assert config_nw.NW_BTC_FILTER_TF == "4h"
    assert config_nw.NW_SIGNAL_EXPIRE_HOURS == 48


def test_derived_tp_price_pct():
    expected = 0.0075
    assert abs(config_nw.TP_PRICE_PCT - expected) < 1e-12


def test_derived_max_sl_price_pct():
    expected = 0.01
    assert abs(config_nw.MAX_SL_PRICE_PCT - expected) < 1e-12
