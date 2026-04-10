"""
Scalping signal engine.

Detects EMA9/EMA21 cross + RSI(7) + VWAP + volume signals on 5-minute candles.
All 3 conditions must be true simultaneously for a signal to fire.

Signal deduplication: same symbol+direction blocked for SCALPING_SIGNAL_COOLDOWN_MINUTES.
Dedup resets when EMA cross reverses direction.

Public API:
  engine = ScalpingEngine()
  signals = await engine.scan_all()          # returns list[ScalpingSignal]
  engine.mark_signal_sent(symbol, direction) # call after broadcasting
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone, date

import aiohttp
import pandas as pd

from config import (
    SCALPING_PAIRS,
    SCALPING_LEVERAGE,
    SCALPING_TP_PCT,
    SCALPING_SL_PCT,
    SCALPING_SIGNAL_COOLDOWN_MINUTES,
    EMA_FAST,
    EMA_SLOW,
    RSI_PERIOD,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    VOLUME_MA_PERIOD,
    VOLUME_MULTIPLIER,
)
from strategy.indicators import ema, rsi, vwap, volume_ma
from strategy.mexc_data import fetch_ohlcv
from strategy.filters import is_trading_session, is_funding_window, current_session_name

logger = logging.getLogger(__name__)

# Minimum candle count needed before indicators are reliable
_MIN_CANDLES = max(EMA_SLOW + 10, VOLUME_MA_PERIOD + 5)


@dataclass
class ScalpingSignal:
    symbol:        str
    direction:     str       # "LONG" | "SHORT"
    entry_price:   float
    tp_price:      float
    sl_price:      float
    leverage:      int
    tp_roi_pct:    float
    sl_roi_pct:    float
    strength:      str       # always "STRONG" (all 3 conditions met)
    fresh_cross:   bool      # EMA cross happened within last 2 candles
    rsi_divergence: bool     # price/RSI divergence detected (warning tag)
    ema9:          float
    ema21:         float
    rsi_val:       float
    vwap_val:      float
    session:       str
    generated_at:  datetime


class ScalpingEngine:
    """
    Stateful scanner for the 5-minute EMA/RSI/VWAP scalping strategy.

    Mutable state (modified by bot commands):
      active_pairs            — list of symbols being scanned
      paused                  — if True, scan_all() still runs but returns []
      session_filter_enabled  — if True, suppress signals outside trading hours
    """

    def __init__(self):
        self.active_pairs: list[str] = list(SCALPING_PAIRS)
        self.paused: bool = False
        self.session_filter_enabled: bool = True
        self.start_time: datetime = datetime.now(timezone.utc)

        # Deduplication: symbol -> {"direction": str, "timestamp": datetime}
        self._last_signals: dict[str, dict] = {}

        # Daily signal counter
        self._signal_count_today: int = 0
        self._count_date: date | None = None

        # Latest indicator snapshot per symbol (for /pairs command)
        self._indicator_snapshot: dict[str, dict] = {}

    # ── public accessors ───────────────────────────────────────────

    def get_signal_count(self) -> int:
        self._maybe_reset_count()
        return self._signal_count_today

    def get_last_signals(self) -> dict[str, dict]:
        return dict(self._last_signals)

    def get_indicator_snapshot(self) -> dict[str, dict]:
        return dict(self._indicator_snapshot)

    def mark_signal_sent(self, symbol: str, direction: str) -> None:
        """
        Call after a signal is successfully broadcast.
        Updates dedup state and increments the daily counter.
        """
        self._last_signals[symbol] = {
            "direction": direction,
            "timestamp": datetime.now(timezone.utc),
        }
        self._maybe_reset_count()
        self._signal_count_today += 1

    # ── scan entry point ───────────────────────────────────────────

    async def scan_all(self) -> list[ScalpingSignal]:
        """
        Scan all active pairs concurrently.

        Returns an empty list when:
          - engine is paused
          - currently in a funding window
          - session filter is on and outside trading hours

        Indicator snapshots are always updated (powers the /pairs command).
        """
        if self.paused:
            return []

        now = datetime.now(timezone.utc)

        if is_funding_window(now):
            logger.debug("Suppressing scan: funding window active")
            return []

        if self.session_filter_enabled and not is_trading_session(now):
            logger.debug("Suppressing scan: outside trading session")
            return []

        async with aiohttp.ClientSession() as http:
            tasks = [
                self._scan_pair_with_jitter(http, sym, random.uniform(0, 5))
                for sym in self.active_pairs
            ]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        signals: list[ScalpingSignal] = []
        for sym, result in zip(self.active_pairs, raw_results):
            if isinstance(result, Exception):
                logger.error(f"[ScalpingEngine] Error scanning {sym}: {result}")
            elif result is not None:
                signals.append(result)

        return signals

    # ── internal helpers ───────────────────────────────────────────

    async def _scan_pair_with_jitter(
        self,
        http: aiohttp.ClientSession,
        symbol: str,
        delay: float,
    ) -> ScalpingSignal | None:
        await asyncio.sleep(delay)
        return await self._analyze_pair(http, symbol)

    async def _analyze_pair(
        self,
        http: aiohttp.ClientSession,
        symbol: str,
    ) -> ScalpingSignal | None:
        try:
            # Fetch 200 5-minute candles (~16 h) for reliable VWAP + indicators
            df = await fetch_ohlcv(http, symbol, interval="5m", limit=200)
            if df.empty or len(df) < _MIN_CANDLES:
                logger.debug(f"{symbol}: insufficient candle data ({len(df)} bars)")
                return None

            # Drop the in-progress (current) candle; use only closed bars
            df = df.iloc[:-1]

            close   = df["close"]
            high    = df["high"]
            low     = df["low"]
            vol     = df["volume"]

            e9      = ema(close, EMA_FAST)
            e21     = ema(close, EMA_SLOW)
            r       = rsi(close, RSI_PERIOD)
            v       = vwap(df)
            vol_avg = volume_ma(vol, VOLUME_MA_PERIOD)

            # Last closed bar values
            e9_cur      = float(e9.iloc[-1])
            e9_prev     = float(e9.iloc[-2])
            e21_cur     = float(e21.iloc[-1])
            e21_prev    = float(e21.iloc[-2])
            rsi_cur     = float(r.iloc[-1])
            rsi_prev    = float(r.iloc[-2])
            vwap_cur    = float(v.iloc[-1])
            price       = float(close.iloc[-1])
            vol_cur     = float(vol.iloc[-1])
            vol_avg_cur = float(vol_avg.iloc[-1]) if not pd.isna(vol_avg.iloc[-1]) else 0.0

            # Update snapshot regardless of signal outcome
            self._indicator_snapshot[symbol] = {
                "price":     price,
                "ema9":      round(e9_cur, 6),
                "ema21":     round(e21_cur, 6),
                "rsi":       round(rsi_cur, 2),
                "vwap":      round(vwap_cur, 6),
                "vol_ratio": round(vol_cur / vol_avg_cur, 2) if vol_avg_cur else 0.0,
            }

            # ── EMA cross / bounce detection ───────────────────────
            cross_up   = (e9_prev <= e21_prev) and (e9_cur > e21_cur)
            cross_down = (e9_prev >= e21_prev) and (e9_cur < e21_cur)

            # EMA21 bounce/rejection: price within 0.15% of EMA21
            near_ema21  = abs(price - e21_cur) / e21_cur < 0.0015
            bounce_up   = near_ema21 and e9_cur > e21_cur and price > e21_cur
            reject_down = near_ema21 and e9_cur < e21_cur and price < e21_cur

            long_ema  = cross_up  or bounce_up
            short_ema = cross_down or reject_down

            # Fresh cross: current bar OR within the last 2 prior bars
            fresh_cross = cross_up or cross_down
            if not fresh_cross and len(e9) >= 5:
                for back in range(2, 4):
                    ep   = float(e9.iloc[-(back + 1)])
                    ec   = float(e9.iloc[-back])
                    ep21 = float(e21.iloc[-(back + 1)])
                    ec21 = float(e21.iloc[-back])
                    if (ep <= ep21 and ec > ec21) or (ep >= ep21 and ec < ec21):
                        fresh_cross = True
                        break

            # ── volume check ───────────────────────────────────────
            vol_ok = vol_avg_cur > 0 and vol_cur >= VOLUME_MULTIPLIER * vol_avg_cur

            # ── RSI divergence (warning tag only, not a filter) ────
            rsi_divergence = False
            if len(close) >= 12:
                window_prices = close.iloc[-12:-1]
                window_rsi    = r.iloc[-12:-1]
                if price > float(window_prices.max()) and rsi_cur < float(window_rsi.max()):
                    rsi_divergence = True   # bearish divergence
                elif price < float(window_prices.min()) and rsi_cur > float(window_rsi.min()):
                    rsi_divergence = True   # bullish divergence

            # ── signal determination ───────────────────────────────
            if (
                long_ema
                and rsi_cur > 50
                and rsi_cur > rsi_prev       # rising RSI
                and rsi_cur < RSI_OVERBOUGHT
                and price > vwap_cur
                and vol_ok
            ):
                direction = "LONG"
            elif (
                short_ema
                and rsi_cur < 50
                and rsi_cur < rsi_prev       # falling RSI
                and rsi_cur > RSI_OVERSOLD
                and price < vwap_cur
                and vol_ok
            ):
                direction = "SHORT"
            else:
                logger.debug(
                    f"{symbol}: no signal — "
                    f"long_ema={long_ema} short_ema={short_ema} "
                    f"rsi={rsi_cur:.1f} price_vs_vwap={'above' if price > vwap_cur else 'below'} "
                    f"vol_ok={vol_ok}"
                )
                return None

            # ── deduplication ──────────────────────────────────────
            if self._is_dedup_blocked(symbol, direction):
                logger.debug(f"Dedup blocked: {direction} {symbol}")
                return None

            # ── build signal ───────────────────────────────────────
            if direction == "LONG":
                tp_price = round(price * (1 + SCALPING_TP_PCT), 8)
                sl_price = round(price * (1 - SCALPING_SL_PCT), 8)
            else:
                tp_price = round(price * (1 - SCALPING_TP_PCT), 8)
                sl_price = round(price * (1 + SCALPING_SL_PCT), 8)

            tp_roi = SCALPING_TP_PCT * SCALPING_LEVERAGE * 100
            sl_roi = SCALPING_SL_PCT * SCALPING_LEVERAGE * 100

            logger.info(
                f"[SCALP] {direction} {symbol} @ {price} | "
                f"RSI={rsi_cur:.1f} fresh={fresh_cross} div={rsi_divergence}"
            )

            return ScalpingSignal(
                symbol        = symbol,
                direction     = direction,
                entry_price   = price,
                tp_price      = tp_price,
                sl_price      = sl_price,
                leverage      = SCALPING_LEVERAGE,
                tp_roi_pct    = tp_roi,
                sl_roi_pct    = sl_roi,
                strength      = "STRONG",
                fresh_cross   = fresh_cross,
                rsi_divergence = rsi_divergence,
                ema9          = round(e9_cur, 6),
                ema21         = round(e21_cur, 6),
                rsi_val       = round(rsi_cur, 2),
                vwap_val      = round(vwap_cur, 6),
                session       = current_session_name(),
                generated_at  = datetime.now(timezone.utc),
            )

        except Exception as e:
            logger.error(f"[ScalpingEngine] Error analyzing {symbol}: {e}", exc_info=True)
            return None

    # ── deduplication helpers ──────────────────────────────────────

    def _is_dedup_blocked(self, symbol: str, direction: str) -> bool:
        entry = self._last_signals.get(symbol)
        if entry is None:
            return False
        last_dir = entry["direction"]
        last_ts  = entry["timestamp"]
        now = datetime.now(timezone.utc)
        # Direction reversed → reset dedup, allow signal
        if last_dir != direction:
            del self._last_signals[symbol]
            return False
        # Same direction within cooldown window → block
        elapsed = (now - last_ts).total_seconds()
        return elapsed < SCALPING_SIGNAL_COOLDOWN_MINUTES * 60

    def _maybe_reset_count(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._count_date != today:
            self._signal_count_today = 0
            self._count_date = today
