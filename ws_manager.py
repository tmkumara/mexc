"""
WebSocket price manager for MEXC Futures live ticker data.

Maintains latest_prices[symbol] = float updated in real time.
Splits symbols into batches of WS_SYMBOLS_PER_CONNECTION and manages
one WebSocket connection per batch with auto-reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import websockets
from websockets.exceptions import ConnectionClosed

from config import (
    MEXC_WS_URL,
    WS_SYMBOLS_PER_CONNECTION,
    WS_RECONNECT_SECONDS,
    WS_PING_INTERVAL_SECONDS,
    WS_PING_TIMEOUT_SECONDS,
    WS_SUBSCRIBE_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)


class WsPriceManager:
    """
    Live price manager using MEXC Futures WebSocket ticker streams.

    Usage:
        manager = WsPriceManager(symbols)
        task = asyncio.create_task(manager.start())
        ...
        price = manager.get_price("BTC_USDT")
        ...
        await manager.stop()
    """

    def __init__(self, symbols: list[str]):
        self.symbols       = [s.strip().upper() for s in symbols if s.strip()]
        self.latest_prices: dict[str, float] = {}
        self._last_update:  dict[str, float] = {}
        self._running      = False
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self._running = True
        if not self.symbols:
            logger.warning("[WS-PRICE] No symbols to subscribe — price manager idle")
            return

        batches = [
            self.symbols[i : i + WS_SYMBOLS_PER_CONNECTION]
            for i in range(0, len(self.symbols), WS_SYMBOLS_PER_CONNECTION)
        ]

        logger.info(
            "[WS-PRICE] Starting %d connection(s) for %d symbols",
            len(batches), len(self.symbols),
        )

        self._tasks = [
            asyncio.create_task(
                self._run_connection(batch, conn_id=idx),
                name=f"ws_price_{idx}",
            )
            for idx, batch in enumerate(batches)
        ]

        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        logger.info("[WS-PRICE] Stopped")

    def get_price(self, symbol: str) -> float | None:
        return self.latest_prices.get(symbol.upper())

    def is_alive(self) -> bool:
        """True if at least one price was received in the last 90 seconds."""
        if not self._last_update:
            return False
        now = time.monotonic()
        return any(now - t < 90.0 for t in self._last_update.values())

    def status_str(self) -> str:
        n_prices = len(self.latest_prices)
        if n_prices == 0:
            return "connecting"
        age = time.monotonic() - max(self._last_update.values()) if self._last_update else 9999
        return f"live ({n_prices} prices, last {age:.0f}s ago)"

    # ── internal ──────────────────────────────────────────────────

    async def _run_connection(self, symbols: list[str], conn_id: int) -> None:
        while self._running:
            try:
                await self._connect_and_listen(symbols, conn_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[WS-PRICE:%d] connection error: %s", conn_id, e)

            if self._running:
                logger.info(
                    "[WS-PRICE:%d] reconnecting in %ds", conn_id, WS_RECONNECT_SECONDS
                )
                await asyncio.sleep(WS_RECONNECT_SECONDS)

    async def _connect_and_listen(self, symbols: list[str], conn_id: int) -> None:
        logger.info("[WS-PRICE:%d] connecting to %s (%d symbols)", conn_id, MEXC_WS_URL, len(symbols))

        async with websockets.connect(
            MEXC_WS_URL,
            ping_interval=WS_PING_INTERVAL_SECONDS,
            ping_timeout=WS_PING_TIMEOUT_SECONDS,
            close_timeout=5,
            max_queue=4096,
        ) as ws:
            logger.info("[WS-PRICE:%d] connected", conn_id)

            # Subscribe to ticker for each symbol
            for symbol in symbols:
                await ws.send(json.dumps({
                    "method": "sub.ticker",
                    "param":  {"symbol": symbol},
                }))
                await asyncio.sleep(WS_SUBSCRIBE_DELAY_SECONDS)

            logger.info("[WS-PRICE:%d] subscribed to %d tickers", conn_id, len(symbols))

            # Heartbeat task
            hb_task = asyncio.create_task(
                self._heartbeat(ws, conn_id),
                name=f"ws_price_hb_{conn_id}",
            )

            try:
                await self._listen(ws, conn_id)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _heartbeat(self, ws, conn_id: int) -> None:
        while self._running:
            await asyncio.sleep(20)
            if not self._running:
                return
            try:
                await ws.send(json.dumps({"method": "ping"}))
                logger.debug("[WS-PRICE:%d] ping sent", conn_id)
            except ConnectionClosed:
                return
            except Exception as e:
                logger.warning("[WS-PRICE:%d] heartbeat error: %s", conn_id, e)
                return

    async def _listen(self, ws, conn_id: int) -> None:
        while self._running:
            try:
                raw = await ws.recv()
            except ConnectionClosed as e:
                logger.warning(
                    "[WS-PRICE:%d] connection closed code=%s reason=%s",
                    conn_id, getattr(e, "code", None), getattr(e, "reason", None),
                )
                break
            except Exception as e:
                logger.warning("[WS-PRICE:%d] recv error: %s", conn_id, e)
                break

            await self._handle(raw, conn_id)

    async def _handle(self, raw: str | bytes, conn_id: int) -> None:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except Exception:
                return

        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Server ping → pong
        ch = str(msg.get("channel") or msg.get("method") or "").lower()
        if ch == "ping":
            try:
                if hasattr(self, "_ws"):
                    pass  # handled via the ws reference in heartbeat
            except Exception:
                pass
            return

        if ch == "pong":
            logger.debug("[WS-PRICE:%d] pong received", conn_id)
            return

        if ch != "push.ticker":
            logger.debug("[WS-PRICE:%d] ignored channel: %s", conn_id, ch)
            return

        data = msg.get("data") or {}
        symbol = (
            data.get("symbol")
            or msg.get("symbol")
            or (msg.get("param") or {}).get("symbol")
        )
        last_price = data.get("lastPrice") or data.get("last")

        if symbol and last_price is not None:
            try:
                price = float(last_price)
                if price > 0:
                    self.latest_prices[symbol.upper()] = price
                    self._last_update[symbol.upper()]  = time.monotonic()
                    logger.debug("[WS-PRICE:%d] %s = %.6g", conn_id, symbol, price)
            except (ValueError, TypeError):
                pass
