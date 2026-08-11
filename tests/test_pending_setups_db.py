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


def _setup_dict(symbol="XRP_USDT", direction="LONG"):
    now = datetime.now(timezone.utc)
    return {
        "symbol": symbol, "direction": direction,
        "macro_tf": "4h", "trend_tf": "1h", "pullback_tf": "15m", "entry_tf": "5m",
        "macro_trend": 1, "trend_state": 1,
        "zlema_1h": 100.0, "zlema_15m": 100.5,
        "pullback_price": 100.4, "pullback_time": now.isoformat(),
        "score": 65.0, "setup_time": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
        "created_at": now.isoformat(),
    }


def test_save_and_fetch_pending_pullback(temp_db):
    setup_id = db.save_pending_setup(_setup_dict())
    assert setup_id is not None
    assert db.pending_setup_exists("XRP_USDT") is True

    rows = db.get_pending_setups("pending_pullback")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending_pullback"


def test_breakout_transition_then_fire(temp_db):
    setup_id = db.save_pending_setup(_setup_dict())
    db.update_pending_setup_breakout(
        setup_id, confirmation_high=101.0, confirmation_low=100.0,
        confirmation_close=100.9, confirmation_time=datetime.now(timezone.utc).isoformat(),
        trigger_price=101.02,
    )
    rows = db.get_pending_setups("pending_breakout")
    assert len(rows) == 1
    assert rows[0]["trigger_price"] == pytest.approx(101.02)

    db.mark_pending_setup_fired(setup_id, signal_id=42, final_score=88.0)
    assert db.get_pending_setups("pending_breakout") == []
    assert db.pending_setup_exists("XRP_USDT") is False


def test_expire_old_pending_setups(temp_db):
    setup = _setup_dict()
    setup["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.save_pending_setup(setup)
    db.expire_old_pending_setups(datetime.now(timezone.utc))
    assert db.get_pending_setups("pending_pullback") == []
