from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from ema_lifecycle import ProcessingSummary, process_pending_outcomes
from ema_outcome import OutcomeResult
from ema_repository import EmaRepository
from ema_signal import EmaSignal


UTC = timezone.utc
BASE = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _signal(direction="LONG", symbol="TEST_USDT", setup_key=None, minute=0):
    entry = 100.0
    tp, sl = (101.5, 99.0) if direction == "LONG" else (98.5, 101.0)
    opened = BASE
    return EmaSignal(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        tp_price=tp,
        sl_price=sl,
        leverage=20,
        tp_roi_pct=30.0,
        sl_roi_pct=20.0,
        rr=1.5,
        strategy_id="ema_trend_pullback",
        strategy_version="v1",
        setup_key=setup_key or f"{symbol}-{direction}-{minute}",
        confirmation_candle_open=opened,
        confirmation_close_at=opened + timedelta(minutes=15),
        pivot_timestamp=opened - timedelta(minutes=30),
        rsi_reset_timestamp=opened - timedelta(minutes=15),
        generated_at=opened + timedelta(minutes=16),
    )


def _candles(rows):
    index = [BASE + timedelta(minutes=15 * offset) for offset, *_ in rows]
    return pd.DataFrame(
        {
            "open": [opening for _, opening, _, _, _ in rows],
            "high": [high for _, _, high, _, _ in rows],
            "low": [low for _, _, _, low, _ in rows],
            "close": [closing for _, _, _, _, closing in rows],
        },
        index=pd.DatetimeIndex(index),
    )


def _save(repo, signal):
    repo.save_signal(signal)


def _run(repo, candles_by_symbol, evaluation=None, expiry=timedelta(hours=1)):
    return process_pending_outcomes(
        repo,
        candles_by_symbol,
        evaluation or BASE + timedelta(hours=2),
        expiry,
    )


def test_pending_long_becomes_win(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal("LONG")
    _save(repo, signal)

    summary = _run(repo, {signal.symbol: _candles([(1, 100, 101.6, 99.8, 101.2)])})

    assert summary == ProcessingSummary(1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
    assert repo.get_signal_record(signal.setup_key).status == "WIN"


def test_pending_long_becomes_loss(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal("LONG")
    _save(repo, signal)

    summary = _run(repo, {signal.symbol: _candles([(1, 100, 100.2, 98.9, 99.2)])})

    assert summary.losses == 1
    assert repo.get_signal_record(signal.setup_key).status == "LOSS"


@pytest.mark.parametrize("direction, high, low, expected", [
    ("SHORT", 100.2, 98.4, "WIN"),
    ("SHORT", 101.1, 99.8, "LOSS"),
])
def test_pending_short_outcomes(tmp_path, direction, high, low, expected):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal(direction)
    _save(repo, signal)

    _run(repo, {signal.symbol: _candles([(1, 100, high, low, 100)])})

    assert repo.get_signal_record(signal.setup_key).status == expected


def test_no_hit_before_expiry_remains_pending(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal()
    _save(repo, signal)

    summary = _run(
        repo,
        {signal.symbol: _candles([(1, 100, 100.4, 99.8, 100.1)])},
        evaluation=BASE + timedelta(minutes=45),
    )

    assert summary.still_pending == 1
    assert summary.pending_remaining == 1


def test_complete_history_produces_timeout(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal()
    _save(repo, signal)

    summary = _run(
        repo,
        {signal.symbol: _candles([
            (1, 100, 100.4, 99.8, 100.1),
            (2, 100.1, 100.4, 99.8, 100.2),
            (3, 100.2, 100.4, 99.8, 100.3),
            (4, 100.3, 100.4, 99.8, 100.4),
        ])},
    )

    assert summary.timeouts == 1
    assert repo.get_signal_record(signal.setup_key).status == "TIMEOUT"


def test_missing_symbol_data_leaves_pending(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal()
    _save(repo, signal)

    summary = _run(repo, {})

    assert summary.missing_market_data == 1
    assert summary.pending_remaining == 1
    assert repo.get_signal_record(signal.setup_key).status == "PENDING"


def test_incomplete_history_leaves_pending(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal()
    _save(repo, signal)

    summary = _run(
        repo,
        {signal.symbol: _candles([(1, 100, 100.4, 99.8, 100.1), (3, 100, 100.4, 99.8, 100.1)])},
    )

    assert summary.still_pending == 1
    assert repo.get_signal_record(signal.setup_key).status == "PENDING"


def test_repeated_execution_and_restart_are_idempotent(tmp_path):
    path = tmp_path / "ema.sqlite3"
    signal = _signal()
    candles = {signal.symbol: _candles([(1, 100, 101.6, 99.8, 101.2)])}
    first_repo = EmaRepository(path)
    _save(first_repo, signal)
    first = _run(first_repo, candles)
    second = _run(EmaRepository(path), candles)

    assert first.wins == 1
    assert second == ProcessingSummary(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert EmaRepository(path).get_signal_record(signal.setup_key).status == "WIN"


def test_multiple_signals_and_symbols_are_processed(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    long_signal = _signal("LONG", "A_USDT", minute=1)
    short_signal = _signal("SHORT", "B_USDT", minute=2)
    _save(repo, long_signal)
    _save(repo, short_signal)

    summary = _run(repo, {
        "A_USDT": _candles([(1, 100, 101.6, 99.8, 101.2)]),
        "B_USDT": _candles([(1, 100, 100.2, 98.4, 99.0)]),
    })

    assert summary.total_pending == 2
    assert summary.evaluated == 2
    assert summary.wins == 2


def test_more_than_one_hundred_pending_records_are_not_silently_ignored(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    for index in range(101):
        _save(repo, _signal(setup_key=f"bulk-{index}", minute=index % 50))

    summary = _run(repo, {})

    assert summary.total_pending == 101
    assert summary.missing_market_data == 101
    assert summary.pending_remaining == 101


def test_conflicting_terminal_update_is_not_overwritten(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    signal = _signal()
    _save(repo, signal)
    repo.save_outcome(signal.setup_key, OutcomeResult("WIN", BASE + timedelta(hours=1), 101.5, 30.0, "tp_hit"))

    summary = _run(repo, {signal.symbol: _candles([(1, 100, 100.2, 98.9, 99.2)])})

    assert summary.total_pending == 0
    assert repo.get_signal_record(signal.setup_key).status == "WIN"


def test_summary_counts_errors_without_corrupting_unrelated_signal(tmp_path):
    repo = EmaRepository(tmp_path / "ema.sqlite3")
    bad = _signal(symbol="BAD_USDT", setup_key="bad")
    good = _signal(symbol="GOOD_USDT", setup_key="good")
    _save(repo, bad)
    _save(repo, good)

    summary = _run(repo, {
        "BAD_USDT": pd.DataFrame({"open": [1]}, index=pd.DatetimeIndex([BASE])),
        "GOOD_USDT": _candles([(1, 100, 101.6, 99.8, 101.2)]),
    })

    assert summary.errors == 1
    assert summary.wins == 1
    assert repo.get_signal_record(bad.setup_key).status == "PENDING"
    assert repo.get_signal_record(good.setup_key).status == "WIN"
