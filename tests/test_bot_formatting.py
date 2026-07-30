from datetime import datetime, timezone

import bot
from bot import format_signal
from strategy import Signal


def _sample_signal() -> Signal:
    return Signal(
        symbol="XRP_USDT",
        direction="LONG",
        entry_price=1.100000,
        tp_price=1.108250,
        sl_price=1.095200,
        leverage=20,
        tp_roi_pct=15.0,
        sl_roi_pct=8.7,
        timeframe_summary="Binocular confirmed trigger",
        generated_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        rr=1.72,
        score=82.5,
        entry_low=1.100000,
        entry_high=1.100000,
        tp2_price=1.116500,
        tp3_price=1.124750,
        position_size=450.25,
    )


def test_format_signal_contains_key_fields(monkeypatch):
    monkeypatch.setattr(bot, "STRATEGY_NAME", "Binocular Pending-Breakout v1")
    msg = format_signal(_sample_signal(), signal_id=12)

    assert "XRP/USDT" in msg
    assert "LONG" in msg
    assert "1.1" in msg
    assert "1:1.72" in msg
    assert "20x" in msg
    assert "Binocular Pending-Breakout v1" in msg
    assert "12" in msg


def test_format_signal_shows_all_three_targets():
    msg = format_signal(_sample_signal(), signal_id=13)
    assert "1.1082" in msg or "1.10825" in msg
    assert "1.1165" in msg
    assert "1.1247" in msg or "1.12475" in msg


def test_format_signal_shows_position_size():
    msg = format_signal(_sample_signal(), signal_id=14)
    assert "450.25" in msg or "450.2" in msg


def test_format_signal_short_uses_red_arrow():
    sig = _sample_signal()
    sig.direction = "SHORT"
    msg = format_signal(sig, signal_id=15)
    assert "SHORT" in msg
