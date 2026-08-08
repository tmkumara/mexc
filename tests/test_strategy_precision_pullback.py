from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import config
import strategy
from tests.strategy_fixtures import (
    make_trend_df,
    make_pullback_confirmation_df,
    patch_klines,
    patch_klines_multi,
)


def _patch_pipeline(monkeypatch, entry_df, trend_df):
    patch_klines_multi(monkeypatch, strategy, {
        config.ENTRY_TF: entry_df,
        config.TREND_TF: trend_df,
    })
    monkeypatch.setattr(strategy, "MIN_CANDLE_SETTLE_SECONDS", 0)


def test_pending_setup_created_on_full_pipeline_pass_long(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("LONG"),
        make_trend_df("LONG", bars=260, freq="15min"),
    )

    setup = strategy.detect_pending_setup("TEST_USDT")

    assert setup is not None
    assert setup["direction"] == "LONG"
    assert setup["tp_price"] > setup["trigger_price"] > setup["sl_price"]
    assert setup["score"] >= config.MIN_SIGNAL_SCORE


def test_pending_setup_created_on_full_pipeline_pass_short(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("SHORT"),
        make_trend_df("SHORT", bars=260, freq="15min"),
    )

    setup = strategy.detect_pending_setup("TEST_USDT")

    assert setup is not None
    assert setup["direction"] == "SHORT"
    assert setup["tp_price"] < setup["trigger_price"] < setup["sl_price"]


def test_rejected_when_trend_disagrees_across_timeframes(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("LONG"),
        make_trend_df("SHORT", bars=260, freq="15min"),
    )

    reject_sink: dict = {}
    setup = strategy.detect_pending_setup("TEST_USDT", reject_sink=reject_sink)

    assert setup is None
    # TREND_TF's own close/EMA200/slope are self-consistent for "SHORT" here
    # (a clean, noiseless downtrend), so the TREND_TF-only trend-filter gate
    # (step 1, "no_trend_alignment") passes on its own terms -- the
    # cross-timeframe mismatch against the LONG-shaped ENTRY_TF data is
    # instead caught by the very next gate that reads ENTRY_TF (step 2,
    # "no_ema_alignment", since ENTRY_TF's EMA20>EMA50 there disagrees with
    # the SHORT direction TREND_TF established). Either gate correctly
    # rejects the setup; this asserts the one the pipeline's actual gate
    # ordering guarantees fires first for this exact fixture combination.
    assert reject_sink.get("no_ema_alignment") == 1


def test_rejected_when_chasing_price(monkeypatch):
    entry_df = make_pullback_confirmation_df("LONG")
    entry_df.loc[entry_df.index[-2], "close"] += 5.0
    entry_df.loc[entry_df.index[-2], "high"] += 5.0
    _patch_pipeline(monkeypatch, entry_df, make_trend_df("LONG", bars=260, freq="15min"))

    reject_sink: dict = {}
    setup = strategy.detect_pending_setup("TEST_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("chasing_price") == 1


def test_rejected_when_score_below_minimum(monkeypatch):
    _patch_pipeline(
        monkeypatch,
        make_pullback_confirmation_df("LONG"),
        make_trend_df("LONG", bars=260, freq="15min"),
    )
    monkeypatch.setattr(strategy, "MIN_SIGNAL_SCORE", 101.0)

    reject_sink: dict = {}
    setup = strategy.detect_pending_setup("TEST_USDT", reject_sink=reject_sink)

    assert setup is None
    assert reject_sink.get("score_below_min") == 1


def test_setup_confirms_on_entry_breakout(monkeypatch):
    setup = {
        "symbol": "TEST_USDT", "direction": "LONG",
        "trigger_price": 101.0, "sl_price": 99.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    df = pd.DataFrame({
        "open": [100.5], "high": [101.5], "low": [100.2], "close": [101.3], "volume": [1200.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="5min"))
    df = pd.concat([df, df.iloc[[-1]]])
    patch_klines(monkeypatch, strategy, df)

    status, fill_price = strategy.check_setup_confirmation(setup)

    assert status == "confirmed"
    assert fill_price == 101.0


def test_setup_expires_after_n_candles(monkeypatch):
    setup = {
        "symbol": "TEST_USDT", "direction": "LONG",
        "trigger_price": 200.0, "sl_price": 99.0,
        "created_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    df = pd.DataFrame({
        "open": [100.5], "high": [101.5], "low": [100.2], "close": [101.3], "volume": [1200.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="5min"))
    df = pd.concat([df, df.iloc[[-1]]])
    patch_klines(monkeypatch, strategy, df)

    status, fill_price = strategy.check_setup_confirmation(setup)

    assert status == "expired"
    assert fill_price is None


def test_same_candle_sl_blocks_confirmation(monkeypatch):
    setup = {
        "symbol": "TEST_USDT", "direction": "LONG",
        "trigger_price": 101.0, "sl_price": 99.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    df = pd.DataFrame({
        "open": [100.5], "high": [101.5], "low": [98.5], "close": [101.3], "volume": [1200.0],
    }, index=pd.date_range("2026-01-01", periods=1, freq="5min"))
    df = pd.concat([df, df.iloc[[-1]]])
    patch_klines(monkeypatch, strategy, df)

    status, fill_price = strategy.check_setup_confirmation(setup)

    assert status == "invalidated"
    assert fill_price is None
