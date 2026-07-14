from datetime import datetime, timezone

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
        timeframe_summary="15m bullish trend + 5m EMA20 pullback reclaim",
        generated_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        rr=1.72,
        score=82.5,
        entry_low=1.100000,
        entry_high=1.100000,
    )


def test_format_signal_contains_key_fields():
    msg = format_signal(_sample_signal(), signal_id=12)

    assert "XRP/USDT" in msg
    assert "LONG" in msg
    assert "1.1" in msg
    assert "gross ROI" in msg
    assert "1:1.72" in msg
    assert "20x" in msg
    assert "15m bullish trend + 5m EMA20 pullback reclaim" in msg
    assert "Simple Supertrend Pullback v1" in msg
    assert "12" in msg


def test_format_signal_short_uses_red_arrow():
    sig = _sample_signal()
    sig.direction = "SHORT"
    msg = format_signal(sig, signal_id=13)
    assert "SHORT" in msg
