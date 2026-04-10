"""
Trading session and funding-rate filters.

Trading sessions (UTC):
  Asian:  01:00 – 05:00
  London: 07:00 – 11:00
  US:     13:00 – 17:00

Funding settlements: 00:00, 08:00, 16:00 UTC
Suppression window:  ±10 minutes around each settlement.
"""

from datetime import datetime, timezone

# (start_hour, end_hour) inclusive start, exclusive end
TRADING_SESSIONS: list[tuple[int, int]] = [
    (1,  5),   # Asian session
    (7,  11),  # London session
    (13, 17),  # US session
]

FUNDING_HOURS: list[int] = [0, 8, 16]
FUNDING_SUPPRESS_MINUTES: int = 10


def is_trading_session(dt: datetime | None = None) -> bool:
    """Return True if *dt* (default: now UTC) falls within a high-volume session."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    frac = dt.hour + dt.minute / 60.0
    return any(start <= frac < end for start, end in TRADING_SESSIONS)


def is_funding_window(dt: datetime | None = None) -> bool:
    """
    Return True if within FUNDING_SUPPRESS_MINUTES of any funding settlement.
    Handles day-boundary wrap (e.g. 23:55 is 5 min before 00:00).
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    total_min = dt.hour * 60 + dt.minute
    for fh in FUNDING_HOURS:
        funding_min = fh * 60
        diff = total_min - funding_min
        # Wrap around midnight (1440 minutes per day)
        if diff > 720:
            diff -= 1440
        elif diff < -720:
            diff += 1440
        if abs(diff) <= FUNDING_SUPPRESS_MINUTES:
            return True
    return False


def current_session_name(dt: datetime | None = None) -> str:
    """Return a human-readable session label, or 'off-hours'."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    frac = dt.hour + dt.minute / 60.0
    if 1 <= frac < 5:
        return "Asian"
    if 7 <= frac < 11:
        return "London"
    if 13 <= frac < 17:
        return "US"
    return "off-hours"
