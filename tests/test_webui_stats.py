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


def test_get_strategy_config_reports_precision_pullback_keys():
    cfg = webui.get_strategy_config()
    assert "min_signal_score" in cfg
    assert "tp_roi_pct" in cfg
    assert "breakeven_trigger_roi_pct" in cfg
    assert "no_chase_max_distance_pct" in cfg
    assert "signal_mode" not in cfg
