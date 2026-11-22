"""Mandatory strength filters for a confirmed structural transition."""

from .models import CHoCHEvent, MarketTrend, RejectionReason


def qualify(candle, atr, direction, protected, trend, continuation_count, parameters):
    swing = protected.swing
    body = abs(candle.close - candle.open)
    distance = abs(candle.close - swing.price)
    span = candle.high - candle.low
    bullish = direction == MarketTrend.BULLISH
    close_distance = candle.close - candle.low if bullish else candle.high - candle.close
    close_position = close_distance / span if span else 0.0
    body_ratio = body / span if span else 0.0
    body_atr = body / atr if atr else None
    break_atr = distance / atr if atr else None
    directional_body = candle.close - candle.open if bullish else candle.open - candle.close
    checks = (
        (not atr, RejectionReason.MISSING_ATR),
        (directional_body <= 0, RejectionReason.WRONG_BODY_DIRECTION),
        (body_atr is not None and body_atr < parameters.minimum_body_atr,
         RejectionReason.WEAK_DISPLACEMENT),
        (break_atr is not None and break_atr < parameters.minimum_break_atr,
         RejectionReason.SHALLOW_BREAK),
        (body_ratio < parameters.minimum_body_ratio, RejectionReason.POOR_BODY_QUALITY),
        (close_position < parameters.minimum_close_position, RejectionReason.WEAK_CLOSE_POSITION),
        (continuation_count < 1, RejectionReason.INSUFFICIENT_TREND_HISTORY),
    )
    reasons = tuple(reason for failed, reason in checks if failed)
    event_id = f"{candle.symbol}:4h:{direction.value}:{swing.pivot_timestamp.isoformat()}"
    return CHoCHEvent(
        event_id, candle.symbol, "4h", direction, candle.closed_at,
        swing.pivot_timestamp, swing.confirmation_timestamp, swing.price,
        protected.established_at, trend, MarketTrend.UNKNOWN, candle.close, continuation_count,
        atr, body_atr, break_atr, body_ratio, close_position, not reasons, reasons,
    )
