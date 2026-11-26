import sqlite3
from datetime import datetime, timezone

import pytest

import webui


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_signals.db"
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, direction TEXT, entry_price REAL, tp_price REAL, sl_price REAL,
            leverage INTEGER, status TEXT, placed INTEGER, generated_at TEXT,
            placed_at TEXT, closed_at TEXT, pnl_roi REAL
        )
    """)
    now = datetime.now(timezone.utc).isoformat()
    con.executemany(
        "INSERT INTO signals (symbol, direction, entry_price, tp_price, sl_price, leverage, "
        "status, placed, generated_at, pnl_roi) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        [
            ("A_USDT", "LONG", 100.0, 100.35, 99.5, 20, "win", now, 7.0),
            ("B_USDT", "LONG", 100.0, 100.35, 99.5, 20, "loss", now, -10.0),
            ("C_USDT", "LONG", 100.0, 100.35, 99.5, 20, "breakeven", now, 0.0),
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(webui, "DB_PATH", str(db_path))
    return db_path


def test_get_stats_reports_breakevens_and_excludes_from_win_rate(temp_db):
    stats = webui.get_stats()
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["breakevens"] == 1
    assert stats["win_rate"] == 50.0
    assert stats["net_roi"] == -3.0


def test_get_strategy_config_reports_live_scanner():
    cfg = webui.get_strategy_config()
    assert cfg["strategy"] == "1H / 4H Nadaraya-Watson Envelope + RSI"
    assert cfg["timeframe"] == "1H / 4H"
    assert cfg["coins"] == 40
    assert cfg["tp_roi_pct"] == 30
    assert cfg["leverage"] == 20
    assert cfg["next_scan_lkt"].endswith(" LKT")


def test_dashboard_login_api_and_websocket(monkeypatch, temp_db, tmp_path):
    from fastapi.testclient import TestClient
    from hashlib import pbkdf2_hmac
    import json

    monkeypatch.setattr(webui, "WEBUI_TOKEN", "test-password")
    client = TestClient(webui.app, base_url="https://example.test")
    assert "Dashboard password" in client.get("/").text
    assert client.get("/api/data").status_code == 401
    assert client.post("/", data={"token": "wrong"}).status_code == 401
    credentials = tmp_path / "credentials.json"
    salt = b"test-salt"
    credentials.write_text(json.dumps(dict(username="admin", salt=salt.hex(),
        hash=pbkdf2_hmac("sha256", b"test-password", salt, 310000).hex(), session_key="aa"*32)))
    monkeypatch.setattr(webui, "CREDENTIAL_FILE", credentials)
    proxy = {"X-Forwarded-Prefix": "/mexc/dashboard"}
    assert "Username" in client.get("/", headers=proxy).text
    assert client.get("/api/data?token=test-password", headers=proxy).status_code == 401
    proxied = client.post("/", data={"username": "admin", "password": "test-password"},
                          headers=proxy, follow_redirects=False)
    assert proxied.status_code == 303 and proxied.headers["location"] == "./"
    assert "Path=/mexc/dashboard/" in proxied.headers["set-cookie"]
    for flag in ("HttpOnly", "Secure", "SameSite=strict"):
        assert flag in proxied.headers["set-cookie"]

    proxy_session = dict(proxy, Cookie=webui.AUTH_COOKIE+"="+proxied.cookies[webui.AUTH_COOKIE])
    assert client.get("/api/data", headers=proxy_session).status_code == 200
    with client.websocket_connect("wss://example.test/ws", headers=proxy_session) as ws:
        assert ws.receive_json()["config"]["coins"] == 40

    signed_in = client.post("/", data={"token": "test-password"})
    assert signed_in.status_code == 200
    assert "Order Watch" in signed_in.text
    assert "Zero-Lag" not in signed_in.text
    data = client.get("/api/data").json()
    assert data["order_watches"] == [] and data["runtime"]["watching"] == 0
    assert data["alltime"]["wins"] == 1
    with client.websocket_connect("wss://example.test/ws") as ws:
        assert ws.receive_json()["config"]["coins"] == 40
