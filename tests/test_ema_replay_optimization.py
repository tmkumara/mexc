from dataclasses import replace

import numpy as np
import pandas as pd

import strategy_ema_pullback
from ema_dataset_loader import load_dataset
from ema_replay import _build_indicator_cache, _build_replay_access, _replay_dataset, replay_dataset


def _reference_rsi(values: pd.Series, period: int = 14) -> pd.Series:
    prices = values.astype(float)
    result = pd.Series(np.nan, index=prices.index, dtype=float)
    if len(prices) <= period:
        return result
    changes = prices.diff().to_numpy(dtype=float)
    gains = np.maximum(changes, 0.0)
    losses = np.maximum(-changes, 0.0)
    average_gain = gains[1 : period + 1].mean()
    average_loss = losses[1 : period + 1].mean()

    def rsi_value(gain: float, loss: float) -> float:
        if loss == 0.0:
            return 50.0 if gain == 0.0 else 100.0
        if gain == 0.0:
            return 0.0
        return 100.0 - (100.0 / (1.0 + gain / loss))

    result.iloc[period] = rsi_value(average_gain, average_loss)
    for position in range(period + 1, len(prices)):
        average_gain = ((average_gain * (period - 1)) + gains[position]) / period
        average_loss = ((average_loss * (period - 1)) + losses[position]) / period
        result.iloc[position] = rsi_value(average_gain, average_loss)
    return result


def _reference_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    high = candles["high"].astype(float).to_numpy()
    low = candles["low"].astype(float).to_numpy()
    close = candles["close"].astype(float).to_numpy()
    result = pd.Series(np.nan, index=candles.index, dtype=float)
    if len(candles) < period:
        return result
    true_range = np.empty(len(candles), dtype=float)
    true_range[0] = high[0] - low[0]
    true_range[1:] = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])]
    )
    atr = true_range[:period].mean()
    result.iloc[period - 1] = atr
    for position in range(period, len(candles)):
        atr = ((atr * (period - 1)) + true_range[position]) / period
        result.iloc[position] = atr
    return result


def _bounded_dataset(end_evaluations: int):
    dataset = load_dataset("analysis_ema_2026q3/sol", "SOL_USDT")
    eligible = dataset.candles_15m.loc[dataset.candles_15m.index >= pd.Timestamp(dataset.evaluation_start_at)]
    end = eligible.index[end_evaluations - 1] + pd.Timedelta(minutes=15)
    return replace(dataset, evaluation_end_at=end.to_pydatetime())


def _replay_signature(result):
    return {
        "accepted": [
            (
                item.signal.setup_key,
                item.signal.direction,
                item.signal.entry_price,
                item.signal.tp_price,
                item.signal.sl_price,
                item.signal.confirmation_close_at,
                item.signal.pivot_timestamp,
                item.signal.rsi_reset_timestamp,
                item.evaluation_timestamp,
            )
            for item in result.accepted_signals
        ],
        "rejected": result.rejected_evaluation_count,
        "reasons": dict(result.rejection_reason_counts),
        "duplicates": result.duplicate_count,
        "evaluations": result.total_evaluations,
    }


def test_optimized_replay_matches_preoptimization_rsi_atr_on_bounded_real_data(monkeypatch):
    dataset = _bounded_dataset(100)
    optimized = replay_dataset(dataset)

    monkeypatch.setattr(strategy_ema_pullback, "calculate_rsi", _reference_rsi)
    monkeypatch.setattr(strategy_ema_pullback, "calculate_atr", _reference_atr)
    reference = replay_dataset(dataset)

    assert _replay_signature(optimized) == _replay_signature(reference)


def test_cached_replay_matches_uncached_early_middle_and_late_windows():
    dataset = load_dataset("analysis_ema_2026q3/sol", "SOL_USDT")
    eligible = dataset.candles_15m.loc[dataset.candles_15m.index >= pd.Timestamp(dataset.evaluation_start_at)]

    for start_position in (0, 3000, 6000):
        start = eligible.index[start_position]
        end = eligible.index[start_position + 99] + pd.Timedelta(minutes=15)
        window = replace(dataset, evaluation_start_at=start.to_pydatetime(), evaluation_end_at=end.to_pydatetime())
        uncached = _replay_dataset(window, indicator_cache=None)
        cached = replay_dataset(window)
        assert _replay_signature(cached) == _replay_signature(uncached)


def test_cache_does_not_use_future_candles_for_a_bounded_window():
    dataset = _bounded_dataset(100)
    future_15m = dataset.candles_15m.copy(deep=True)
    future_1h = dataset.candles_1h.copy(deep=True)
    cutoff = pd.Timestamp(dataset.evaluation_end_at)
    future_15m.loc[future_15m.index > cutoff, ["high", "low", "close"]] = [1000.0, 1.0, 500.0]
    future_1h.loc[future_1h.index + pd.Timedelta(hours=1) > cutoff, ["high", "low", "close"]] = [1000.0, 1.0, 500.0]
    future_dataset = replace(dataset, candles_15m=future_15m, candles_1h=future_1h)

    original = replay_dataset(dataset)
    modified_future = replay_dataset(future_dataset)

    assert _replay_signature(original) == _replay_signature(modified_future)


def test_cache_is_built_once_and_does_not_mutate_dataset():
    dataset = _bounded_dataset(100)
    before_15m = dataset.candles_15m.copy(deep=True)
    before_1h = dataset.candles_1h.copy(deep=True)

    _build_indicator_cache(dataset)

    pd.testing.assert_frame_equal(dataset.candles_15m, before_15m)
    pd.testing.assert_frame_equal(dataset.candles_1h, before_1h)


def test_replay_access_precomputes_causal_prefix_boundaries():
    dataset = _bounded_dataset(100)

    access = _build_replay_access(dataset)

    assert len(access.evaluation_positions) == 101
    assert access.fifteen_prefix_ends[0] == int(access.evaluation_positions[0]) + 1
    assert access.fifteen_prefix_ends[-1] == int(access.evaluation_positions[-1]) + 1
    assert all(
        earlier <= later
        for earlier, later in zip(access.hourly_prefix_ends, access.hourly_prefix_ends[1:])
    )
    assert all(end <= len(dataset.candles_1h) for end in access.hourly_prefix_ends)


def test_replay_access_preserves_90_second_settlement_boundary():
    dataset = _bounded_dataset(100)

    access = _build_replay_access(dataset)
    first_position = access.evaluation_positions[0]
    first_evaluation = access.evaluation_timestamps[0]

    assert first_evaluation == dataset.candles_15m.index[first_position] + pd.Timedelta(minutes=15, seconds=90)
    assert access.fifteen_prefix_ends[0] == first_position + 1
    assert (
        dataset.candles_1h.index[access.hourly_prefix_ends[0] - 1] + pd.Timedelta(hours=1)
        <= first_evaluation
    )


def test_replay_uses_trusted_completed_prefixes(monkeypatch):
    dataset = _bounded_dataset(10)

    def unexpected_completion_check(*args, **kwargs):
        raise AssertionError("replay should not recalculate completion for trusted prefixes")

    monkeypatch.setattr(strategy_ema_pullback, "is_completed_candle", unexpected_completion_check)

    result = replay_dataset(dataset)

    assert result.total_evaluations == 11
