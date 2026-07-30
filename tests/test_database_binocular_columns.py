import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DB_PATH"] = path
    import importlib
    import config
    importlib.reload(config)
    import database
    importlib.reload(database)
    database.init_db()
    yield database
    os.remove(path)


def test_save_armed_setup_stores_three_targets(temp_db):
    setup_id = temp_db.save_armed_setup({
        "symbol": "XRP_USDT", "direction": "LONG",
        "trigger_price": 1.0, "entry_low": 1.0, "entry_high": 1.0,
        "sl_price": 0.98, "tp_price": 1.02, "tp2_price": 1.04, "tp3_price": 1.06,
        "position_size": 500.0, "rr": 1.8, "score": 70.0,
        "expires_at": datetime.now(timezone.utc).isoformat(),
    })
    setups = temp_db.get_armed_setups()
    assert len(setups) == 1
    row = setups[0]
    assert row["tp2_price"] == pytest.approx(1.04)
    assert row["tp3_price"] == pytest.approx(1.06)
    assert row["position_size"] == pytest.approx(500.0)


def test_save_signal_stores_targets_and_position_size(temp_db):
    signal_id = temp_db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0,
        tp_price=1.02, sl_price=0.98, leverage=20,
        generated_at=datetime.now(timezone.utc),
        tp2_price=1.04, tp3_price=1.06, position_size=500.0,
    )
    rows = temp_db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["tp2_price"] == pytest.approx(1.04)
    assert row["tp3_price"] == pytest.approx(1.06)
    assert row["position_size"] == pytest.approx(500.0)


def test_mark_signal_tp2_hit(temp_db):
    signal_id = temp_db.save_signal(
        symbol="XRP_USDT", direction="LONG", entry_price=1.0,
        tp_price=1.02, sl_price=0.98, leverage=20,
        generated_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    temp_db.mark_signal_tp2_hit(signal_id, now)
    rows = temp_db.get_pending_signals()
    row = next(r for r in rows if r["id"] == signal_id)
    assert row["tp2_hit_at"] is not None
