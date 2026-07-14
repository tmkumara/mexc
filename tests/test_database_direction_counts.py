from datetime import datetime, timezone

import database as db


def test_count_active_signals_by_direction(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_signals.db"))
    db.init_db()

    now = datetime.now(timezone.utc)
    db.save_signal("XRP_USDT", "LONG", 1.0, 1.0075, 0.995, 20, now)
    db.save_signal("DOGE_USDT", "LONG", 0.1, 0.10075, 0.0995, 20, now)
    db.save_signal("ADA_USDT", "SHORT", 1.0, 0.9925, 1.005, 20, now)

    assert db.count_active_signals_by_direction("LONG") == 2
    assert db.count_active_signals_by_direction("SHORT") == 1
    assert db.count_active_signals_by_direction("SHORT") != 2


def test_save_signal_persists_new_metadata_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_signals2.db"))
    db.init_db()

    now = datetime.now(timezone.utc)
    db.save_signal(
        "XRP_USDT", "LONG", 1.0, 1.0075, 0.995, 20, now,
        strategy_name="Simple Supertrend Pullback v1",
        score=82.5, rr=1.72, entry_timeframe="5m", trend_timeframe="15m",
        setup_reason="15m bullish trend + 5m EMA20 pullback reclaim",
    )

    row = db.get_pending_signals()[0]
    assert row["strategy_name"] == "Simple Supertrend Pullback v1"
    assert row["score"] == 82.5
    assert row["rr"] == 1.72
    assert row["entry_timeframe"] == "5m"
    assert row["trend_timeframe"] == "15m"
    assert row["setup_reason"] == "15m bullish trend + 5m EMA20 pullback reclaim"
