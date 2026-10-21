"""Canonical standalone EMA signal availability and expiry timestamps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


EMA_SIGNAL_EXPIRY = timedelta(hours=4)
_SETTLEMENT_DELAY = timedelta(seconds=90)


def calculate_expiry_at(confirmation_close_at: datetime) -> datetime:
    """Return the exact UTC expiry from confirmation close.

    Signal availability is 90 seconds after the confirmation candle closes;
    the fixed four-hour expiry starts at that availability timestamp.  This
    function intentionally has no clock, environment, or configuration input.
    """
    if not isinstance(confirmation_close_at, datetime):
        raise ValueError("confirmation_close_at must be a datetime")
    if confirmation_close_at.tzinfo is None or confirmation_close_at.utcoffset() is None:
        raise ValueError("confirmation_close_at must be timezone-aware")
    confirmation_close_utc = confirmation_close_at.astimezone(timezone.utc)
    availability_at = confirmation_close_utc + _SETTLEMENT_DELAY
    return availability_at + EMA_SIGNAL_EXPIRY
