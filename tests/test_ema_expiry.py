from datetime import datetime, timedelta, timezone

import pytest

from ema_expiry import EMA_SIGNAL_EXPIRY, calculate_expiry_at


UTC = timezone.utc


def test_exact_four_hour_duration_and_ninety_second_availability_delay():
    confirmation_close = datetime(2026, 7, 1, 10, 15, tzinfo=UTC)

    expiry = calculate_expiry_at(confirmation_close)

    availability = confirmation_close + timedelta(seconds=90)
    assert EMA_SIGNAL_EXPIRY == timedelta(hours=4)
    assert expiry == availability + timedelta(hours=4)
    assert expiry == datetime(2026, 7, 1, 14, 16, 30, tzinfo=UTC)


def test_result_is_utc_aware():
    result = calculate_expiry_at(datetime(2026, 7, 1, 10, 15, tzinfo=UTC))

    assert result.tzinfo is UTC
    assert result.utcoffset() == timedelta(0)


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_expiry_at(datetime(2026, 7, 1, 10, 15))


def test_non_utc_aware_input_is_normalized_to_utc():
    india = timezone(timedelta(hours=5, minutes=30))

    result = calculate_expiry_at(datetime(2026, 7, 1, 15, 45, tzinfo=india))

    assert result == datetime(2026, 7, 1, 14, 16, 30, tzinfo=UTC)
    assert result.tzinfo is UTC


def test_midnight_crossing():
    result = calculate_expiry_at(datetime(2026, 7, 1, 23, 45, tzinfo=UTC))

    assert result == datetime(2026, 7, 2, 3, 46, 30, tzinfo=UTC)


def test_month_and_year_boundary_crossing():
    result = calculate_expiry_at(datetime(2026, 12, 31, 23, 45, tzinfo=UTC))

    assert result == datetime(2027, 1, 1, 3, 46, 30, tzinfo=UTC)


def test_repeated_calculation_is_deterministic_and_clock_independent():
    confirmation_close = datetime(2026, 7, 1, 10, 15, tzinfo=UTC)

    first = calculate_expiry_at(confirmation_close)
    second = calculate_expiry_at(confirmation_close)

    assert first == second


def test_input_is_not_mutated_and_old_configuration_is_not_imported():
    confirmation_close = datetime(2026, 7, 1, 10, 15, tzinfo=UTC)
    before = confirmation_close

    calculate_expiry_at(confirmation_close)

    assert confirmation_close == before
