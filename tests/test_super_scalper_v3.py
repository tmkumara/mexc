"""Offline unit tests for SuperScalper.confluence_ok() / pullback_entry_ok()
in super_scalper_v3.py -- no network calls.

Covers the flip-entry slope fix: confluence_ok() used to require
kc_slope > 0.05 (BUY) / < -0.05 (SELL) at the same bar as kc_pos being at
the band edge, which is near-mutually-exclusive at a fresh SuperTrend flip
(kc_slope lags price). It now only requires the channel not be sloping
against the trade (> -0.02 / < 0.02).
"""

from super_scalper_v3 import SuperScalper


def _sig(**overrides):
    sig = dict(
        side="BUY", trend="BULLISH", strength=5, ao=1.0, ao_rising=True,
        kc_pos=0.2, kc_slope=0.2, kc_mid=101.0, kc_upper=104.0, kc_lower=98.0,
        adx=30.0, chop=30.0, expansion=1.5, regime="TRENDING", regime_votes=3,
        stop_loss=98.5, price=100.0,
    )
    sig.update(overrides)
    return sig


def _engine():
    return SuperScalper()


# ── confluence_ok (flip path) ──────────────────────────────────────────

def test_buy_flip_fires_with_mildly_negative_slope():
    # kc_pos low (still at the band edge) with kc_slope not yet turned up --
    # previously impossible to pass (needed > 0.05), now allowed.
    sig = _sig(side="BUY", kc_pos=0.2, kc_slope=-0.01)
    assert _engine().confluence_ok(sig) is True


def test_buy_flip_still_rejected_with_strongly_negative_slope():
    # Channel clearly still falling -- gate must still reject this.
    sig = _sig(side="BUY", kc_pos=0.2, kc_slope=-0.05)
    assert _engine().confluence_ok(sig) is False


def test_sell_flip_fires_with_mildly_positive_slope():
    sig = _sig(side="SELL", trend="BEARISH", kc_pos=0.8, kc_slope=0.01,
               ao=-1.0, ao_rising=False)
    assert _engine().confluence_ok(sig) is True


def test_sell_flip_still_rejected_with_strongly_positive_slope():
    sig = _sig(side="SELL", trend="BEARISH", kc_pos=0.8, kc_slope=0.05,
               ao=-1.0, ao_rising=False)
    assert _engine().confluence_ok(sig) is False


def test_confluence_ok_still_gates_on_regime_ranging():
    sig = _sig(side="BUY", kc_pos=0.2, kc_slope=-0.01, regime="RANGING")
    assert _engine().confluence_ok(sig) is False


# ── pullback_entry_ok (continuation path) — thresholds unchanged ───────

def test_pullback_buy_fires_with_confirmed_positive_slope():
    sig = _sig(side="BUY", kc_pos=0.2, kc_slope=0.06, ao_rising=True)
    assert _engine().pullback_entry_ok(sig) is True


def test_pullback_buy_rejected_with_mildly_negative_slope():
    # This is exactly the slope value the flip-path fix now allows through
    # confluence_ok() -- pullback_entry_ok() must NOT have been loosened.
    sig = _sig(side="BUY", kc_pos=0.2, kc_slope=-0.01, ao_rising=True)
    assert _engine().pullback_entry_ok(sig) is False


def test_pullback_sell_fires_with_confirmed_negative_slope():
    sig = _sig(side="SELL", trend="BEARISH", kc_pos=0.8, kc_slope=-0.06,
               ao_rising=False)
    assert _engine().pullback_entry_ok(sig) is True


def test_pullback_sell_rejected_with_mildly_positive_slope():
    sig = _sig(side="SELL", trend="BEARISH", kc_pos=0.8, kc_slope=0.01,
               ao_rising=False)
    assert _engine().pullback_entry_ok(sig) is False
