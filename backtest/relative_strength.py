"""
Pure functions for the BTC relative-strength continuation backtest (Phase
1 -- see docs/superpowers/specs/2026-07-25-btc-relative-strength-backtest-design.md).

No lookahead: compute_returns is a plain causal pct_change (each bar's
value only depends on bars at or before it). rs_signal is a pure function
of two already-computed return values -- callers are responsible for
passing btc_return/alt_return as of the same bar's CLOSE, then filling
any resulting entry at the NEXT bar's OPEN (see
backtest/rs_continuation_backtest.py for the walk-forward loop that does
this).
"""

from __future__ import annotations

import pandas as pd


def compute_returns(close: pd.Series, lookback_bars: int) -> pd.Series:
    """% return over the lookback window, ending at each bar (causal -- pandas
    pct_change only looks backward, never forward)."""
    return close.pct_change(lookback_bars) * 100.0


def rs_signal(
    btc_return: float,
    alt_return: float,
    move_threshold_pct: float,
    rs_threshold_pct: float,
) -> str | None:
    """Given BTC's and one alt's cumulative % return over the same lookback
    window, return "LONG", "SHORT", or None.

    LONG:  BTC moved up more than move_threshold_pct AND the alt's excess
           return over BTC (rs_score) exceeds rs_threshold_pct.
    SHORT: BTC moved down more than move_threshold_pct AND the alt's excess
           return over BTC is below -rs_threshold_pct.
    Thresholds are exclusive (strictly greater/less than) so a bar sitting
    exactly on a threshold does not qualify.
    """
    if pd.isna(btc_return) or pd.isna(alt_return):
        return None

    rs_score = alt_return - btc_return

    if btc_return > move_threshold_pct and rs_score > rs_threshold_pct:
        return "LONG"
    if btc_return < -move_threshold_pct and rs_score < -rs_threshold_pct:
        return "SHORT"
    return None
