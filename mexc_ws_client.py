"""
MEXC Futures WebSocket client for kline updates.

Current purpose:
    - Connect to MEXC Futures WebSocket.
    - Subscribe to kline streams.
    - Update CandleCache.
    - Detect closed candles via CandleCache.
    - Provide safe callbacks for future strategy integration.

This file does not directly send Telegram signals.
Strategy integration will be done in main.py in the next step.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from candle_cache import CandleCache, CandleUpdateResult
from config import (
    MEXC_WS_URL,
    MEXC_INTERVAL_MAP,
    WS_RECONNECT_DELAY_SECONDS,
    WS_PING_INTERVAL_SECONDS,
    WS_PING_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

CandleUpdateCallback = Callable[[CandleUpdateResult], Awaitable[None] | None]


class MexcWebSocketClient:
    """
    Small WebSocket client for MEXC Futures klines.

    Subscription format:
        {
            "method": "sub.kline",
            "param": {
                "symbol": "BTC_USDT",
                "interval": "Min5"
            }
        }

    Expected push channel:
        push.kline
    """

    def __init__(
        self,
        candle_cache: CandleCache,
        symbols: list[str],
        app_intervals: list[str],
        on_candle_update: CandleUpdateCallback | None = None,
        url: str = MEXC_WS_URL,
    ):
        self.candle_cache = candle_cache
        self.symbols = [self._normalize_symbol(s) for s in symbols if s]
        self.app_intervals = [self._normalize_app_interval(i) for i in app_intervals if i]
        self.on_candle_update = on_candle_update
        self.url = url

        self._running = False
        self._ws = None

        self.mexc_intervals = [
            self._to_mexc_interval(app_interval)
            for app_interval in self.app_intervals
        ]

        if not self.symbols:
            raise ValueError("At least one symbol is required for WebSocket subscription")

        if not self.mexc_intervals:
            raise ValueError("At least one interval is required for WebSocket subscription")

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return str(symbol).strip().upper()

    @staticmethod
    def _normalize_app_interval(interval: str) -> str:
        return str(interval).strip().lower()

    @staticmethod
    def _to_mexc_interval(app_interval: str) -> str:
        mexc_interval = MEXC_INTERVAL_MAP.get(app_interval)

        if not mexc_interval:
            raise ValueError(
                f"Unsupported WebSocket interval: {app_interval}. "
                f"Supported intervals: {', '.join(MEXC_INTERVAL_MAP.keys())}"
            )

        return mexc_interval

    async def start(self) -> None:
        """
        Start the reconnect loop.

        This runs until stop() is called or the task is cancelled.
        """
        self._running = True

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[WS] Client error: %s", e, exc_info=True)

            if self._running:
                logger.info("[WS] Reconnecting in %s seconds", WS_RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(WS_RECONNECT_DELAY_SECONDS)

    async def stop(self) -> None:
        self._running = False

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("[WS] Error while closing socket", exc_info=True)

    async def _connect_and_listen(self) -> None:
        logger.info("[WS] Connecting to %s", self.url)

        async with websockets.connect(
            self.url,
            ping_interval=WS_PING_INTERVAL_SECONDS,
            ping_timeout=WS_PING_TIMEOUT_SECONDS,
            close_timeout=5,
            max_queue=4096,
        ) as ws:
            self._ws = ws
            logger.info("[WS] Connected")

            await self._subscribe_all(ws)
            await self._listen(ws)

    async def _subscribe_all(self, ws) -> None:
        total = 0

        for symbol in self.symbols:
            for interval in self.mexc_intervals:
                payload = {
                    "method": "sub.kline",
                    "param": {
                        "symbol": symbol,
                        "interval": interval,
                    },
                }

                await ws.send(json.dumps(payload))
                total += 1

                logger.info("[WS] Subscribed %s %s", symbol, interval)

                # Small delay avoids sending many subscriptions in the same instant.
                await asyncio.sleep(0.05)

        logger.info("[WS] Subscription complete. total=%s", total)

    async def _listen(self, ws) -> None:
        while self._running:
            try:
                raw_message = await ws.recv()
            except ConnectionClosed:
                logger.warning("[WS] Connection closed")
                break

            await self._handle_raw_message(raw_message)

    async def _handle_raw_message(self, raw_message: Any) -> None:
        if isinstance(raw_message, bytes):
            try:
                raw_message = raw_message.decode("utf-8")
            except Exception:
                logger.debug("[WS] Ignoring non-text binary message")
                return

        try:
            message = json.loads(raw_message)
        except Exception:
            logger.debug("[WS] Ignoring non-JSON message: %s", raw_message)
            return

        # Subscription acknowledgements and pongs may not contain kline data.
        channel = message.get("channel") or message.get("method")

        if channel != "push.kline":
            logger.debug("[WS] Non-kline message: %s", message)
            return

        data = message.get("data") or {}

        symbol = (
            data.get("symbol")
            or message.get("symbol")
            or message.get("param", {}).get("symbol")
        )

        interval = data.get("interval")

        if not symbol or not interval:
            logger.debug("[WS] Kline message missing symbol/interval: %s", message)
            return

        candle = self._extract_candle(data)

        try:
            result = self.candle_cache.update_from_ws(
                symbol=symbol,
                interval=interval,
                candle=candle,
            )
        except Exception as e:
            logger.warning("[WS] Failed to update candle cache: %s | data=%s", e, data)
            return

        if result.closed_event:
            logger.info(
                "[WS] Candle closed %s %s %s close=%s",
                result.closed_event.symbol,
                result.closed_event.interval,
                result.closed_event.closed_timestamp,
                result.closed_event.closed_candle["close"],
            )
        else:
            logger.debug(
                "[WS] Candle update %s %s %s",
                result.symbol,
                result.interval,
                result.timestamp,
            )

        if self.on_candle_update:
            callback_result = self.on_candle_update(result)

            if asyncio.iscoroutine(callback_result):
                await callback_result

    @staticmethod
    def _extract_candle(data: dict[str, Any]) -> dict[str, Any]:
        """
        Normalize MEXC Futures kline payload.

        MEXC may provide both normal OHLC and real OHLC fields:
            o, h, l, c
            ro, rh, rl, rc

        Prefer real OHLC fields when present.
        """
        return {
            "timestamp": data.get("t"),
            "open": data.get("ro", data.get("o")),
            "high": data.get("rh", data.get("h")),
            "low": data.get("rl", data.get("l")),
            "close": data.get("rc", data.get("c")),
            "volume": data.get("v", data.get("q", data.get("a", 0.0))),
        }


async def run_ws_test() -> None:
    """
    Manual local test helper.

    Usage:
        python mexc_ws_client.py

    It subscribes to BTC_USDT Min5 + Min30 based on config defaults.
    """
    from config import WS_TEST_SYMBOLS, ENTRY_TF, TREND_TF, CANDLE_CACHE_LIMIT

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    cache = CandleCache(limit=CANDLE_CACHE_LIMIT)

    async def on_update(result: CandleUpdateResult):
        if result.closed_event:
            logger.info(
                "[WS-TEST] Closed event received: %s %s %s",
                result.closed_event.symbol,
                result.closed_event.interval,
                result.closed_event.closed_timestamp,
            )

    client = MexcWebSocketClient(
        candle_cache=cache,
        symbols=WS_TEST_SYMBOLS,
        app_intervals=[ENTRY_TF, TREND_TF],
        on_candle_update=on_update,
    )

    await client.start()


if __name__ == "__main__":
    asyncio.run(run_ws_test())