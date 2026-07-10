import pytest

from liq_estimator import LiqEstimator


def _single_tier_estimator(**overrides):
    params = dict(
        leverage_tiers={10: 1.0},
        mmr_buffer=0.0,
        bucket_pct=0.01,
        decay=0.9,
        lookaround_pct=0.5,
        min_percentile=0,
        account_leverage=10,
    )
    params.update(overrides)
    return LiqEstimator(**params)


def test_rising_oi_accumulates_clusters_at_projected_liquidation_prices():
    est = _single_tier_estimator()
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)   # first sample: baseline only
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)   # d_oi=200 -> distribute

    long_mag = est.magnitude_between(85.0, 95.0, side="long")
    short_mag = est.magnitude_between(105.0, 115.0, side="short")
    assert long_mag == pytest.approx(100.0)
    assert short_mag == pytest.approx(100.0)
    # no cross-contamination
    assert est.magnitude_between(85.0, 95.0, side="short") == pytest.approx(0.0)


def test_falling_oi_does_not_distribute_new_positions():
    est = _single_tier_estimator()
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=800.0, price=100.0)   # d_oi negative -> no new clusters
    assert est.magnitude_between(0.0, 1000.0) == pytest.approx(0.0)


def test_price_sweep_clears_the_bucket_it_crosses():
    est = _single_tier_estimator()
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)   # clusters at ~90 (long) and ~110 (short)
    assert est.magnitude_between(85.0, 95.0, side="long") == pytest.approx(100.0)

    est.on_oi_sample(oi_usdt=1200.0, price=85.0)    # price sweeps down through 90
    assert est.magnitude_between(85.0, 95.0, side="long") == pytest.approx(0.0)
    # the far cluster at ~110 was never crossed
    assert est.magnitude_between(105.0, 115.0, side="short") == pytest.approx(100.0)


def test_decay_shrinks_and_eventually_removes_clusters():
    est = _single_tier_estimator(decay=0.5)
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)
    assert est.magnitude_between(105.0, 115.0, side="short") == pytest.approx(100.0)

    for _ in range(100):
        est.decay_clusters()

    assert est.magnitude_between(0.0, 10_000_000.0) < 1e-6


def test_significant_clusters_respects_lookaround_window():
    est = _single_tier_estimator(lookaround_pct=0.05)   # window = +/-5 at price 100
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=1200.0, price=100.0)        # clusters at ~90 and ~110, both 10 away
    assert est.significant_clusters(100.0) == []

    est_wide = _single_tier_estimator(lookaround_pct=0.2)   # window = +/-20
    est_wide.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est_wide.on_oi_sample(oi_usdt=1200.0, price=100.0)
    clusters = est_wide.significant_clusters(100.0)
    assert len(clusters) == 2
    sides = {c[1] for c in clusters}
    assert sides == {"long", "short"}


def test_significant_clusters_filters_by_percentile():
    est = LiqEstimator(
        leverage_tiers={10: 0.1, 50: 0.9},
        mmr_buffer=0.0,
        bucket_pct=0.01,
        decay=1.0,
        lookaround_pct=0.5,
        min_percentile=90,
        account_leverage=10,
    )
    est.on_oi_sample(oi_usdt=1000.0, price=100.0)
    est.on_oi_sample(oi_usdt=2000.0, price=100.0)   # d_oi=1000 -> two magnitude tiers per side

    clusters = est.significant_clusters(100.0)
    # only the lev=50 tier (magnitude 450) clears the 90th percentile of [50,50,450,450]
    assert len(clusters) == 2
    for _, _, magnitude in clusters:
        assert magnitude == pytest.approx(450.0)
