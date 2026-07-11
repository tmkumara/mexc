from datetime import datetime, timedelta

import pandas as pd

from outcome_replay import replay_outcome

START = datetime(2026, 1, 1, 0, 0, 0)
CUTOFF = START - timedelta(minutes=1)
TRIGGER_PCT = 0.5


def _build_df(rows: list[tuple[float, float, float, float]]) -> tuple[pd.DataFrame, list[datetime]]:
    """rows: (open, high, low, close) for the real candles under test.
    Appends one extra trailing row that replay_outcome never evaluates
    (mirrors the in-progress bar the real caller also excludes via len(df)-1)."""
    timestamps = [START + timedelta(minutes=i) for i in range(len(rows) + 1)]
    all_rows = rows + [rows[-1]]
    df = pd.DataFrame(all_rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(timestamps))
    return df, timestamps[:-1]


def test_normal_tp_hit_no_breakeven():
    rows = [
        (100.0, 102.0, 99.0, 101.0),
        (104.0, 111.0, 104.0, 110.0),
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "win"
    assert triggered is None
    assert closed_at_be is False


def test_normal_sl_hit_no_breakeven():
    rows = [
        (100.0, 101.0, 98.0, 99.0),
        (99.0, 100.0, 94.0, 95.0),
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered is None
    assert closed_at_be is False


def test_breakeven_triggers_then_closes_at_breakeven():
    rows = [
        (104.0, 106.0, 103.0, 105.0),   # reaches trigger (105) -- no TP(110) or original SL(95) hit
        (100.0, 101.0, 99.0, 99.5),     # active SL now breakeven (100) -- low 99 <= 100 -> stopped at breakeven
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered == ts[0]
    assert closed_at_be is True


def test_breakeven_triggers_then_hits_real_tp():
    rows = [
        (104.0, 106.0, 103.0, 105.0),   # reaches trigger
        (102.0, 112.0, 101.0, 111.0),   # goes on to hit real TP (110)
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "win"
    assert triggered == ts[0]
    assert closed_at_be is False


def test_same_candle_tiebreak_original_sl_wins():
    rows = [
        (98.0, 106.0, 94.0, 96.0),   # single candle crosses BOTH original SL (95) and trigger (105)
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered is None
    assert closed_at_be is False


def test_existing_trigger_from_prior_tick_applies_only_after_its_candle():
    rows = [
        (101.0, 106.0, 99.0, 104.0),   # the historical trigger candle -- must still resolve
                                        # against the ORIGINAL sl (95), not breakeven, even
                                        # though its own timestamp equals existing_trigger_ts
        (100.5, 101.0, 99.5, 99.8),    # after the trigger candle -- active SL is breakeven (100)
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, ts[0], TRIGGER_PCT,
    )
    assert outcome == "loss"
    assert triggered is None
    assert closed_at_be is True


def test_still_pending_returns_all_none():
    rows = [
        (100.0, 101.0, 99.0, 100.5),
    ]
    df, ts = _build_df(rows)
    outcome, triggered, closed_at_be = replay_outcome(
        "LONG", 100.0, 110.0, 95.0, df, CUTOFF, None, TRIGGER_PCT,
    )
    assert outcome is None
    assert triggered is None
    assert closed_at_be is False
