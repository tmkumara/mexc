from datetime import datetime, timedelta, timezone

import pytest

import config
import database as db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_signals.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init_db()
    return db_path


def test_breakeven_status_round_trips(temp_db):
    now = datetime.now(timezone.utc)
    signal_id = db.save_signal(
        symbol="TEST_USDT", direction="LONG", entry_price=100.0,
        tp_price=100.35, sl_price=99.5, leverage=20, generated_at=now,
        strategy_name="Precision Pullback Scalper v1",
    )

    db.update_signal_outcome(signal_id, "breakeven", 0.0)

    rows = db.get_signals_in_range(now - timedelta(minutes=1), now + timedelta(minutes=1))
    assert len(rows) == 1
    assert rows[0]["status"] == "breakeven"
    assert rows[0]["pnl_roi"] == 0.0
