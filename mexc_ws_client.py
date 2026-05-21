"""
MEXC Futures WebSocket client for kline updates.

Purpose:
    - Connect to MEXC Futures WebSocket.
    - Subscribe to kline streams.
    - Send application-level heartbeat.
    - Update CandleCache.
    - Detect closed candles via CandleCache.
    - Provide safe callbacks for future strategy integration.

This file does not directly send Telegram signals.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
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
    WS_APP_HEARTBEAT_ENABLED,
    WS_APP_HEARTBEAT_SECONDS,
    WS_SUBSCRIBE_DELAY_SECONDS,
    WS_SUBSCRIBE_BATCH_SIZE,
    WS_SUBSCRIBE_BATCH_PAUSE_SECONDS,
)

logger = logging.getLogger(__name__)

CandleUpdateCallback = Callable[[CandleUpdateResult], Awaitable[None] | None]


class MexcWebSocketClient:
    """
    WebSocket client for MEXC Futures klines.

    Subscription format:
        {
            "method": "sub.kline",
            "param": {
                "symbol": "BTC_USDT",
                "interval": "Min5"
            }
        }

    Expected kline channel:
        push.kline

    Heartbeat:
        Sends {"method": "ping"} every WS_APP_HEARTBEAT_SECONDS.
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
        self._heartbeat_task: asyncio.Task | None = None
        self._last_message_at: datetime | None = None
        self._last_pong_at: datetime | None = None

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
        Start reconnect loop.

        Runs until stop() is called or task is cancelled.
        """
        self._running = True

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[WS] Client error: %s", e, exc_info=True)

            await self._cancel_heartbeat()

            if self._running:
                logger.info("[WS] Reconnecting in %s seconds", WS_RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(WS_RECONNECT_DELAY_SECONDS)

    async def stop(self) -> None:
        self._running = False

        await self._cancel_heartbeat()

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
            self._last_message_at = datetime.now(timezone.utc)
            self._last_pong_at = None

            logger.info("[WS] Connected")

            if WS_APP_HEARTBEAT_ENABLED:
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(ws),
                    name="mexc_ws_heartbeat",
                )
                logger.info("[WS] Application heartbeat started every %ss", WS_APP_HEARTBEAT_SECONDS)

            await self._subscribe_all(ws)
            await self._listen(ws)

    async def _cancel_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None

        if task is None:
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("[WS] Heartbeat task cleanup error", exc_info=True)

    async def _heartbeat_loop(self, ws) -> None:
        while self._running:
            await asyncio.sleep(WS_APP_HEARTBEAT_SECONDS)

            if not self._running:
                return

            try:
                payload = {"method": "ping"}
                await ws.send(json.dumps(payload))
                logger.debug("[WS-HB] Ping sent")
            except ConnectionClosed as e:
                logger.warning(
                    "[WS-HB] Ping failed because connection closed code=%s reason=%s",
                    getattr(e, "code", None),
                    getattr(e, "reason", None),
                )
                return
            except Exception as e:
                logger.warning("[WS-HB] Ping failed: %s", e)
                return

    async def _subscribe_all(self, ws) -> None:
        total = 0
        batch_count = 0

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
                batch_count += 1

                logger.info("[WS] Subscribed %s %s", symbol, interval)

                await asyncio.sleep(WS_SUBSCRIBE_DELAY_SECONDS)

                if batch_count >= WS_SUBSCRIBE_BATCH_SIZE:
                    batch_count = 0
                    await asyncio.sleep(WS_SUBSCRIBE_BATCH_PAUSE_SECONDS)

        logger.info("[WS] Subscription complete. total=%s", total)

    async def _listen(self, ws) -> None:
        while self._running:
            try:
                raw_message = await ws.recv()
            except ConnectionClosed as e:
                logger.warning(
                    "[WS] Connection closed code=%s reason=%s",
                    getattr(e, "code", None),
                    getattr(e, "reason", None),
                )
                break
            except Exception as e:
                logger.warning("[WS] Receive error: %s", e, exc_info=True)
                break

            self._last_message_at = datetime.now(timezone.utc)
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

        if self._is_ping_message(message):
            await self._respond_to_server_ping(message)
            return

        if self._is_pong_message(message):
            self._last_pong_at = datetime.now(timezone.utc)
            logger.debug("[WS-HB] Pong received: %s", message)
            return

        # Subscription acknowledgements and other non-kline messages.
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
    def _is_ping_message(message: dict[str, Any]) -> bool:
        channel = str(message.get("channel") or "").lower()
        method = str(message.get("method") or "").lower()
        msg = str(message.get("msg") or "").lower()

        return channel == "ping" or method == "ping" or msg == "ping"

    @staticmethod
    def _is_pong_message(message: dict[str, Any]) -> bool:
        channel = str(message.get("channel") or "").lower()
        method = str(message.get("method") or "").lower()
        msg = str(message.get("msg") or "").lower()

        return channel == "pong" or method == "pong" or msg == "pong"

    async def _respond_to_server_ping(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            return

        # Keep it simple and compatible with MEXC-style app heartbeat.
        payload = {"method": "pong"}

        try:
            await self._ws.send(json.dumps(payload))
            logger.debug("[WS-HB] Server ping received, pong sent: %s", message)
        except Exception as e:
            logger.warning("[WS-HB] Failed to send pong: %s", e)

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

    symbols = WS_TEST_SYMBOLS or ["BTC_USDT"]

    client = MexcWebSocketClient(
        candle_cache=cache,
        symbols=symbols,
        app_intervals=[ENTRY_TF, TREND_TF],
        on_candle_update=on_update,
    )

    await client.start()


if __name__ == "__main__":
    asyncio.run(run_ws_test())