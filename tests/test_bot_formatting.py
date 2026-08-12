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
        sl_price=1.088500,
        leverage=20,
        tp_roi_pct=7.0,
        sl_roi_pct=10.0,
        timeframe_summary="4H:Bullish 1H:Agree 15m:Pullback 5m:Recovery",
        generated_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        rr=0.70,
        score=82.5,
        entry_low=1.100000,
        entry_high=1.100000,
    )


def test_format_signal_contains_key_fields(monkeypatch):
    monkeypatch.setattr(bot, "STRATEGY_NAME", "Zero-Lag MTF Pullback v1")
    msg = format_signal(_sample_signal(), signal_id=12)

    assert "XRP/USDT" in msg
    assert "LONG" in msg
    assert "1.1" in msg
    assert "1:0.7" in msg
    assert "20x" in msg
    assert "Zero-Lag MTF Pullback v1" in msg
    assert "12" in msg


def test_format_signal_does_not_show_ladder_targets():
    msg = format_signal(_sample_signal(), signal_id=13)
    assert "T2" not in msg
    assert "T3" not in msg
    assert "(to T1)" not in msg


def test_format_signal_short_uses_red_arrow():
    sig = _sample_signal()
    sig.direction = "SHORT"
    msg = format_signal(sig, signal_id=15)
    assert "SHORT" in msg
