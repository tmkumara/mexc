from datetime import datetime, timedelta, timezone
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ema_outcome import OutcomeResult
from ema_repository import EmaRepository
from ema_signal import EmaSignal


def _signal(direction="LONG", setup_key=None, minute=0):
    entry = 100.0
    if direction == "LONG":
        tp, sl = 101.5, 99.0
    else:
        tp, sl = 98.5, 101.0
    open_at = datetime(2026, 1, 2, 12, minute, tzinfo=timezone.utc)
    return EmaSignal(
        symbol="TEST_USDT",
        direction=direction,
        entry_price=entry,
        tp_price=tp,
        sl_price=sl,
        leverage=20,
        tp_roi_pct=30.125,
        sl_roi_pct=20.875,
        rr=1.503,
        strategy_id="ema_trend_pullback",
        strategy_version="v1",
        setup_key=setup_key or f"key-{direction}-{minute}",
        confirmation_candle_open=open_at,
        confirmation_close_at=open_at + timedelta(minutes=15),
        pivot_timestamp=open_at - timedelta(minutes=15),
        rsi_reset_timestamp=open_at - timedelta(minutes=10),
        generated_at=open_at + timedelta(minutes=1),
    )


def test_schema_initialization_creates_only_fresh_signals_table(tmp_path):
    path = tmp_path / "ema.sqlite3"
    repo = EmaRepository(path)

    repo.initialize()

    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(signals)")]
        assert columns == [
            "id", "strategy_id", "strategy_version", "setup_key", "symbol", "direction",
            "entry_price", "tp_price", "sl_price", "leverage", "tp_roi_pct", "sl_roi_pct",
            "rr", "confirmation_candle_open", "confirmation_close_at", "pivot_timestamp",
            "rsi_reset_timestamp", "generated_at", "created_at", "status", "closed_at",
            "exit_price", "gross_roi_pct", "outcome_reason", "updated_at",
        ]


def test_save_long_and_retrieve_by_setup_key(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal("LONG")

    result = repo.save_signal(signal)
    restored = repo.get_by_setup_key(signal.setup_key)

    assert result.inserted is True
    assert result.signal_id == 1
    assert result.setup_key == signal.setup_key
    assert restored == signal


def _outcome(status, signal):
    if status == "WIN":
        exit_price, roi, reason = signal.tp_price, signal.tp_roi_pct, "tp_hit"
    elif status == "LOSS":
        exit_price, roi, reason = signal.sl_price, -signal.sl_roi_pct, "sl_hit"
    elif status == "TIMEOUT":
        exit_price, roi, reason = signal.entry_price + 0.25, 5.0, "expiry_reached"
    else:
        return OutcomeResult("PENDING", None, None, None, "awaiting_expiry")
    return OutcomeResult(
        status,
        signal.confirmation_close_at + timedelta(minutes=30),
        exit_price,
        roi,
        reason,
    )


def test_new_signal_starts_pending_and_appears_in_pending_list(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()

    repo.save_signal(signal)
    record = repo.get_signal_record(signal.setup_key)

    assert record is not None
    assert record.signal == signal
    assert record.status == "PENDING"
    assert record.closed_at is None
    assert record.exit_price is None
    assert record.gross_roi_pct is None
    assert record.outcome_reason is None
    assert repo.list_pending_signals() == [record]


@pytest.mark.parametrize("direction,status", [
    ("LONG", "WIN"), ("LONG", "LOSS"),
    ("SHORT", "WIN"), ("SHORT", "LOSS"), ("LONG", "TIMEOUT"),
])
def test_pending_signal_can_save_each_terminal_outcome(tmp_path, direction, status):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal(direction, setup_key=f"{direction}-{status}")
    repo.save_signal(signal)

    result = repo.save_outcome(signal.setup_key, _outcome(status, signal))
    record = repo.get_signal_record(signal.setup_key)

    assert result.updated is True
    assert record.status == status
    assert record.closed_at is not None
    assert record.exit_price is not None
    assert record.gross_roi_pct is not None
    assert record.outcome_reason
    assert repo.list_pending_signals() == []


def test_pending_outcome_leaves_signal_pending(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()
    repo.save_signal(signal)

    result = repo.save_outcome(signal.setup_key, _outcome("PENDING", signal))
    record = repo.get_signal_record(signal.setup_key)

    assert result.updated is False
    assert result.reason == "still_pending"
    assert record.status == "PENDING"
    assert record.closed_at is None


def test_terminal_update_is_idempotent(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()
    outcome = _outcome("WIN", signal)
    repo.save_signal(signal)
    first = repo.save_outcome(signal.setup_key, outcome)
    second = repo.save_outcome(signal.setup_key, outcome)

    assert first.updated is True
    assert second.updated is False
    assert second.reason == "already_terminal_same"
    assert repo.get_signal_record(signal.setup_key).status == "WIN"


def test_conflicting_terminal_update_is_rejected_without_overwrite(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()
    repo.save_signal(signal)
    repo.save_outcome(signal.setup_key, _outcome("WIN", signal))

    result = repo.save_outcome(signal.setup_key, _outcome("LOSS", signal))

    assert result.updated is False
    assert result.reason == "terminal_conflict"
    assert repo.get_signal_record(signal.setup_key).status == "WIN"


def test_unknown_setup_key_returns_clear_result(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()

    result = repo.save_outcome("missing", OutcomeResult("PENDING", None, None, None, "waiting"))

    assert result.updated is False
    assert result.reason == "not_found"
    assert repo.get_signal_record("missing") is None


def test_invalid_terminal_outcome_is_rejected(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()
    repo.save_signal(signal)
    invalid = OutcomeResult("WIN", None, signal.tp_price, signal.tp_roi_pct, "tp_hit")

    with pytest.raises(ValueError, match="terminal"):
        repo.save_outcome(signal.setup_key, invalid)


def test_outcome_survives_repository_restart(tmp_path):
    path = tmp_path / "ema.sqlite3"
    signal = _signal(setup_key="restart-outcome")
    first_repo = EmaRepository(path)
    first_repo.initialize()
    first_repo.save_signal(signal)
    first_repo.save_outcome(signal.setup_key, _outcome("TIMEOUT", signal))

    second_repo = EmaRepository(path)
    second_repo.initialize()
    record = second_repo.get_signal_record(signal.setup_key)

    assert record.status == "TIMEOUT"
    assert record.outcome_reason == "expiry_reached"


def test_concurrent_terminal_updates_cannot_conflict_in_final_state(tmp_path):
    path = tmp_path / "ema.sqlite3"
    setup_key = "concurrent"
    signal = _signal(setup_key=setup_key)
    EmaRepository(path).save_signal(signal)

    def save(status):
        repo = EmaRepository(path)
        return repo.save_outcome(setup_key, _outcome(status, signal))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["WIN", "LOSS"]))

    assert sorted(result.updated for result in results) == [False, True]
    assert EmaRepository(path).get_signal_record(setup_key).status in {"WIN", "LOSS"}


def test_original_signal_fields_remain_unchanged_after_outcome(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()
    repo.save_signal(signal)
    repo.save_outcome(signal.setup_key, _outcome("WIN", signal))

    record = repo.get_signal_record(signal.setup_key)

    assert record.signal == signal


def test_save_short_and_list_signals_in_deterministic_order(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    first = _signal("LONG", "first", 0)
    second = _signal("SHORT", "second", 1)

    repo.save_signal(first)
    repo.save_signal(second)

    assert repo.list_signals() == [first, second]
    assert repo.list_signals(limit=1) == [first]


def test_duplicate_setup_key_is_not_overwritten(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    original = _signal("LONG", "same")
    duplicate = _signal("SHORT", "same", 1)

    first = repo.save_signal(original)
    second = repo.save_signal(duplicate)

    assert first.inserted is True
    assert second.inserted is False
    assert second.signal_id == first.signal_id
    assert repo.get_by_setup_key("same") == original
    assert len(repo.list_signals()) == 1


def test_duplicate_remains_rejected_after_repository_restart(tmp_path):
    path = tmp_path / "ema.sqlite3"
    signal = _signal(setup_key="restart-safe")
    first_repo = EmaRepository(path)
    first_repo.initialize()
    first_repo.save_signal(signal)

    second_repo = EmaRepository(path)
    second_repo.initialize()
    result = second_repo.save_signal(signal)

    assert result.inserted is False
    assert second_repo.setup_exists(signal.setup_key)


def test_timestamp_price_and_roi_round_trip(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()
    signal = _signal()
    repo.save_signal(signal)

    restored = repo.get_by_setup_key(signal.setup_key)

    assert restored is not None
    assert restored.confirmation_candle_open == signal.confirmation_candle_open
    assert restored.confirmation_close_at == signal.confirmation_close_at
    assert restored.pivot_timestamp == signal.pivot_timestamp
    assert restored.rsi_reset_timestamp == signal.rsi_reset_timestamp
    assert restored.generated_at == signal.generated_at
    assert restored.entry_price == signal.entry_price
    assert restored.tp_roi_pct == signal.tp_roi_pct
    assert restored.sl_roi_pct == signal.sl_roi_pct
    assert restored.rr == signal.rr


def test_empty_database_behavior(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()

    assert repo.get_by_setup_key("missing") is None
    assert repo.list_signals() == []
    assert repo.setup_exists("missing") is False


def test_invalid_signal_is_rejected(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    repo.initialize()

    with pytest.raises(ValueError, match="EmaSignal"):
        repo.save_signal(object())


def test_database_initialization_is_idempotent(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")

    repo.initialize()
    repo.initialize()
    repo.save_signal(_signal())

    assert len(repo.list_signals()) == 1
