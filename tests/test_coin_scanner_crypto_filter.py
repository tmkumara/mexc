import coin_scanner


def test_known_index_keyword_still_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("NAS100_USDT") is False


def test_tesla_is_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("TESLA_USDT") is False


def test_soxl_is_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("SOXL_USDT") is False


def test_tsla_ticker_variant_is_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("TSLA_USDT") is False


def test_legitimate_coin_with_similar_prefix_not_blocked(monkeypatch):
    # exact base-coin match must not false-positive on a real coin whose
    # symbol merely starts with a blocked ticker's letters
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("METAVERSE_USDT") is True


def test_real_crypto_coin_not_blocked(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", True)
    assert coin_scanner._is_crypto_symbol("DOGE_USDT") is True
    assert coin_scanner._is_crypto_symbol("BTC_USDT") is True


def test_returns_true_unconditionally_when_filter_disabled(monkeypatch):
    monkeypatch.setattr(coin_scanner, "CRYPTO_FUTURES_ONLY", False)
    assert coin_scanner._is_crypto_symbol("TESLA_USDT") is True
