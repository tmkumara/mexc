from strategy import direction_slot_available


def test_second_active_long_is_blocked():
    assert direction_slot_available("LONG", active_long=0, active_short=0) is True
    assert direction_slot_available("LONG", active_long=1, active_short=0) is False


def test_second_active_short_is_blocked():
    assert direction_slot_available("SHORT", active_long=0, active_short=0) is True
    assert direction_slot_available("SHORT", active_long=0, active_short=1) is False


def test_long_and_short_can_coexist():
    assert direction_slot_available("LONG", active_long=0, active_short=1) is True
    assert direction_slot_available("SHORT", active_long=1, active_short=0) is True
