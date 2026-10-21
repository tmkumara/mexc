"""Deterministic offline replay orchestration for the standalone EMA strategy."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import inspect
from typing import Mapping

import pandas as pd

from ema_dataset_loader import EmaHistoricalDataset
from ema_indicators import calculate_atr, calculate_ema, calculate_rsi, find_confirmed_pivots
from ema_signal import EmaSignal, to_ema_signal
from strategy_ema_pullback import evaluate_ema_pullback


@dataclass(frozen=True)
class EmaReplaySignal:
    signal: EmaSignal
    evaluation_timestamp: datetime


@dataclass(frozen=True)
class EmaReplayResult:
    symbol: str
    evaluation_start_at: datetime
    evaluation_end_at: datetime
    total_evaluations: int
    accepted_signals: tuple[EmaReplaySignal, ...]
    rejected_evaluation_count: int
    rejection_reason_counts: Mapping[str, int]
    duplicate_count: int


@dataclass(frozen=True)
class _ReplayAccess:
    evaluation_positions: tuple[int, ...]
    evaluation_timestamps: tuple[pd.Timestamp, ...]
    fifteen_prefix_ends: tuple[int, ...]
    hourly_prefix_ends: tuple[int, ...]


@dataclass(frozen=True)
class _ReplayIndicatorCache:
    ema20_15m: pd.Series
    ema50_15m: pd.Series
    rsi14_15m: pd.Series
    atr14_15m: pd.Series
    ema50_1h: pd.Series
    ema200_1h: pd.Series
    pivots_15m: pd.DataFrame

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "ema20_15m": self.ema20_15m,
            "ema50_15m": self.ema50_15m,
            "rsi14_15m": self.rsi14_15m,
            "atr14_15m": self.atr14_15m,
            "ema50_1h": self.ema50_1h,
            "ema200_1h": self.ema200_1h,
            "pivots_15m": self.pivots_15m,
            "pivot_confirmation_timestamps": pd.DatetimeIndex(
                self.pivots_15m["confirmation_timestamp"]
            ),
        }


def _utc_timestamp(value: object, field_name: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"dataset {field_name} must be a valid timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError(f"dataset {field_name} must be UTC-aware")
    return timestamp.tz_convert("UTC")


def _build_replay_access(dataset: EmaHistoricalDataset) -> _ReplayAccess:
    """Precompute causal candle boundaries for every evaluation candle."""
    if not isinstance(dataset, EmaHistoricalDataset):
        raise ValueError("dataset must be an EmaHistoricalDataset")
    evaluation_start = _utc_timestamp(dataset.evaluation_start_at, "evaluation_start_at")
    evaluation_end = _utc_timestamp(dataset.evaluation_end_at, "evaluation_end_at")
    if evaluation_start >= evaluation_end:
        raise ValueError("dataset evaluation_start_at must be before evaluation_end_at")

    fifteen_index = dataset.candles_15m.index
    first_position = int(fifteen_index.searchsorted(evaluation_start, side="left"))
    end_position = int(fifteen_index.searchsorted(evaluation_end, side="right"))
    positions = tuple(range(first_position, end_position))
    evaluation_timestamps = tuple(
        fifteen_index[position] + pd.Timedelta(minutes=15, seconds=90)
        for position in positions
    )
    hourly_prefix_ends = tuple(
        int(
            dataset.candles_1h.index.searchsorted(
                evaluation_timestamp - pd.Timedelta(hours=1),
                side="right",
            )
        )
        for evaluation_timestamp in evaluation_timestamps
    )
    return _ReplayAccess(
        evaluation_positions=positions,
        evaluation_timestamps=evaluation_timestamps,
        fifteen_prefix_ends=tuple(position + 1 for position in positions),
        hourly_prefix_ends=hourly_prefix_ends,
    )


def _build_indicator_cache(dataset: EmaHistoricalDataset) -> Mapping[str, object]:
    """Build causal indicator series once without mutating the dataset."""
    fifteen = dataset.candles_15m
    hourly = dataset.candles_1h
    cache = _ReplayIndicatorCache(
        ema20_15m=calculate_ema(fifteen["close"], 20),
        ema50_15m=calculate_ema(fifteen["close"], 50),
        rsi14_15m=calculate_rsi(fifteen["close"], 14),
        atr14_15m=calculate_atr(fifteen, 14),
        ema50_1h=calculate_ema(hourly["close"], 50),
        ema200_1h=calculate_ema(hourly["close"], 200),
        pivots_15m=find_confirmed_pivots(fifteen, left=2, right=2),
    )
    return cache.as_mapping()


def _replay_dataset(
    dataset: EmaHistoricalDataset,
    indicator_cache: Mapping[str, object] | None,
    replay_access: _ReplayAccess | None = None,
) -> EmaReplayResult:
    """Replay each evaluation-period 15m candle without future data."""
    if not isinstance(dataset, EmaHistoricalDataset):
        raise ValueError("dataset must be an EmaHistoricalDataset")
    evaluation_start = _utc_timestamp(dataset.evaluation_start_at, "evaluation_start_at")
    evaluation_end = _utc_timestamp(dataset.evaluation_end_at, "evaluation_end_at")
    if evaluation_start >= evaluation_end:
        raise ValueError("dataset evaluation_start_at must be before evaluation_end_at")

    fifteen = dataset.candles_15m
    hourly = dataset.candles_1h
    access = replay_access or _build_replay_access(dataset)
    accepted: list[EmaReplaySignal] = []
    seen_setup_keys: set[str] = set()
    rejection_counts: Counter[str] = Counter()
    duplicate_count = 0
    evaluator_parameters = inspect.signature(evaluate_ema_pullback).parameters
    accepts_cache = (
        "indicator_cache" in evaluator_parameters
        or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in evaluator_parameters.values())
    )
    accepts_trusted_prefix = (
        "trusted_completed_prefix" in evaluator_parameters
        or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in evaluator_parameters.values())
    )

    for position, evaluation_timestamp, fifteen_end, hourly_end in zip(
        access.evaluation_positions,
        access.evaluation_timestamps,
        access.fifteen_prefix_ends,
        access.hourly_prefix_ends,
    ):
        fifteen_available = fifteen.iloc[:fifteen_end]
        hourly_available = hourly.iloc[:hourly_end]
        evaluator_kwargs: dict[str, object] = {}
        if indicator_cache is not None and accepts_cache:
            evaluator_kwargs["indicator_cache"] = indicator_cache
        if indicator_cache is not None and accepts_trusted_prefix:
            evaluator_kwargs["trusted_completed_prefix"] = True
        if evaluator_kwargs:
            result = evaluate_ema_pullback(
                dataset.symbol,
                fifteen_available,
                hourly_available,
                evaluation_timestamp,
                **evaluator_kwargs,
            )
        else:
            result = evaluate_ema_pullback(
                dataset.symbol,
                fifteen_available,
                hourly_available,
                evaluation_timestamp,
            )
        if result.get("accepted") is not True:
            reason = result.get("reason", "unknown_rejection")
            rejection_counts[str(reason)] += 1
            continue

        signal = to_ema_signal(result, evaluation_timestamp.to_pydatetime())
        if signal.setup_key in seen_setup_keys:
            duplicate_count += 1
            continue
        seen_setup_keys.add(signal.setup_key)
        accepted.append(
            EmaReplaySignal(
                signal=signal,
                evaluation_timestamp=evaluation_timestamp.to_pydatetime(),
            )
        )

    return EmaReplayResult(
        symbol=dataset.symbol,
        evaluation_start_at=evaluation_start.to_pydatetime(),
        evaluation_end_at=evaluation_end.to_pydatetime(),
        total_evaluations=len(access.evaluation_positions),
        accepted_signals=tuple(accepted),
        rejected_evaluation_count=sum(rejection_counts.values()),
        rejection_reason_counts=dict(rejection_counts),
        duplicate_count=duplicate_count,
    )


def replay_dataset(dataset: EmaHistoricalDataset) -> EmaReplayResult:
    """Replay each evaluation-period 15m candle using one causal indicator cache."""
    if not isinstance(dataset, EmaHistoricalDataset):
        raise ValueError("dataset must be an EmaHistoricalDataset")
    access = _build_replay_access(dataset)
    return _replay_dataset(
        dataset,
        indicator_cache=_build_indicator_cache(dataset),
        replay_access=access,
    )
