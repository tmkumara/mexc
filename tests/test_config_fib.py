import config_fib


def test_defaults_loaded():
    assert config_fib.TREND_TF == "4h"
    assert config_fib.ENTRY_TF == "1h"
    assert config_fib.TREND_EMA_PERIOD == 200
    assert config_fib.SWING_FRACTAL_BARS == 5
    assert config_fib.SWING_LOOKBACK_BARS == 60
    assert config_fib.ZONE_LOOKBACK_BARS == 10
    assert config_fib.ZONE_LOWER == 0.5
    assert config_fib.ZONE_UPPER == 0.618
    assert config_fib.TP_EXTENSION_LEVEL == 1.272
    assert config_fib.LEVERAGE == 20
    assert config_fib.MIN_RR == 1.5
    assert config_fib.ENABLE_BTC_FILTER is True
    assert config_fib.BTC_FILTER_TF == "4h"


def test_derived_max_sl_price_pct():
    expected = config_fib.MAX_SL_ROI_PCT / 100.0 / config_fib.LEVERAGE
    assert abs(config_fib.MAX_SL_PRICE_PCT - expected) < 1e-12
