import mexc_client


def test_get_ticker_parses_list_response(monkeypatch):
    def fake_get(path, params=None, retries=5):
        assert path == "/contract/ticker"
        assert params == {"symbol": "BTC_USDT"}
        return {"data": [
            {"symbol": "ETH_USDT", "fairPrice": "1.0", "holdVol": "1.0", "fundingRate": "0.0"},
            {"symbol": "BTC_USDT", "fairPrice": "65000.5", "holdVol": "12345.0", "fundingRate": "0.0001"},
        ]}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    result = mexc_client.get_ticker("BTC_USDT")

    assert result == {"fair_price": 65000.5, "hold_vol": 12345.0, "funding_rate": 0.0001}


def test_get_ticker_parses_dict_response(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": {"symbol": "BTC_USDT", "fairPrice": "65000.5",
                          "holdVol": "12345.0", "fundingRate": "0.0001"}}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    result = mexc_client.get_ticker("BTC_USDT")

    assert result == {"fair_price": 65000.5, "hold_vol": 12345.0, "funding_rate": 0.0001}


def test_get_ticker_returns_none_when_symbol_missing(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": [{"symbol": "ETH_USDT", "fairPrice": "1.0", "holdVol": "1.0", "fundingRate": "0.0"}]}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    assert mexc_client.get_ticker("BTC_USDT") is None


def test_get_ticker_returns_none_on_missing_required_field(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": {"symbol": "BTC_USDT", "fairPrice": "65000.5"}}   # holdVol missing
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    assert mexc_client.get_ticker("BTC_USDT") is None


def test_get_ticker_defaults_funding_rate_to_zero_when_absent(monkeypatch):
    def fake_get(path, params=None, retries=5):
        return {"data": {"symbol": "BTC_USDT", "fairPrice": "65000.5", "holdVol": "12345.0"}}
    monkeypatch.setattr(mexc_client, "_get", fake_get)

    result = mexc_client.get_ticker("BTC_USDT")

    assert result == {"fair_price": 65000.5, "hold_vol": 12345.0, "funding_rate": 0.0}
