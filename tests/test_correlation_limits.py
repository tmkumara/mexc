import config
from strategy import direction_slot_available


def test_second_active_long_is_blocked_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_LONG_SIGNALS", 1)
    assert direction_slot_available("LONG", active_long=0, active_short=0) is True
    assert direction_slot_available("LONG", active_long=1, active_short=0) is False


def test_second_active_short_is_blocked_at_the_cap(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_SHORT_SIGNALS", 1)
    assert direction_slot_available("SHORT", active_long=0, active_short=0) is True
    assert direction_slot_available("SHORT", active_long=0, active_short=1) is False


def test_long_and_short_can_coexist():
    assert direction_slot_available("LONG", active_long=0, active_short=1) is True
    assert direction_slot_available("SHORT", active_long=1, active_short=0) is True


def test_current_default_allows_two_active_per_direction():
    # Zero-Lag MTF Pullback v1's higher signal-frequency target raised this
    # cap from Precision Pullback's 1 -- pin the expectation to the actual
    # current default so a future config change fails loudly here instead
    # of silently changing correlation-limit behavior.
    assert config.MAX_ACTIVE_LONG_SIGNALS == 2
    assert config.MAX_ACTIVE_SHORT_SIGNALS == 2
    assert direction_slot_available("LONG", active_long=1, active_short=0) is True
    assert direction_slot_available("LONG", active_long=2, active_short=0) is False
