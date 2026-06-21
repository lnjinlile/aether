"""
Binance Futures WebSocket stream handler.
Connects to testnet or mainnet WebSocket for real-time market data.
"""

import json
import asyncio
import logging
from typing import Callable, Optional

import websockets

logger = logging.getLogger(__name__)


class BinanceWebSocket:
    """
    Binance Futures WebSocket client for real-time data streams.

    Supports combined streams for klines, mark price, and mini tickers.
    """

    # WebSocket base URLs
    MAINNET_WS_URL = "wss://fstream.binance.com/ws"
    TESTNET_WS_URL = "wss://testnet.binancefuture.com/ws"

    # Supported stream types and their payload keys
    STREAM_TYPES = {
        "kline_1m": "kline",
        "kline_5m": "kline",
        "kline_15m": "kline",
        "kline_1h": "kline",
        "kline_4h": "kline",
        "kline_1d": "kline",
        "markPrice@1s": "markPrice",
        "miniTicker": "miniTicker",
    }

    def __init__(self, testnet: bool = True):
        """
        Initialize the WebSocket client.

        Args:
            testnet: Whether to connect to testnet (default True).
        """
        self.testnet = testnet
        self.base_url = self.TESTNET_WS_URL if testnet else self.MAINNET_WS_URL

        # Callbacks
        self._kline_callback: Optional[Callable] = None
        self._mark_price_callback: Optional[Callable] = None
        self._ticker_callback: Optional[Callable] = None

        # Connection state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._tasks: list = []

    def on_kline(self, callback: Callable):
        """
        Register a callback for kline stream events.

        Callback signature: callback(data: dict)
        data includes: symbol, kline (open_time, open, high, low, close,
                       volume, close_time, quote_volume, trades, ...)
        """
        self._kline_callback = callback
        return callback

    def on_mark_price(self, callback: Callable):
        """
        Register a callback for mark price stream events.

        Callback signature: callback(data: dict)
        data includes: symbol, markPrice, indexPrice, fundingRate, ...
        """
        self._mark_price_callback = callback
        return callback

    def on_ticker(self, callback: Callable):
        """
        Register a callback for mini ticker stream events.

        Callback signature: callback(data: dict)
        data includes: symbol, closePrice, openPrice, highPrice, lowPrice,
                       volume, quoteVolume
        """
        self._ticker_callback = callback
        return callback

    async def connect_combined_stream(self, streams: list) -> None:
        """
        Connect to a combined WebSocket stream for multiple symbols/streams.

        Stream names should be in lowercase Binance format:
            e.g. 'btcusdt@kline_1h', 'ethusdt@miniTicker', 'btcusdt@markPrice@1s'

        Args:
            streams: List of stream name strings.

        Example:
            >>> ws = BinanceWebSocket(testnet=True)
            >>> asyncio.run(ws.connect_combined_stream([
            ...     'btcusdt@kline_1h',
            ...     'ethusdt@kline_1m',
            ... ]))
        """
        # Build combined stream URL
        stream_path = "/".join(streams)
        url = f"{self.base_url}/stream?streams={stream_path}"

        self._running = True
        logger.info(f"Connecting to WebSocket: {url}")

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    logger.info("WebSocket connected successfully")

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except websockets.ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}. Reconnecting...")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting...")
                await asyncio.sleep(5)

        logger.info("WebSocket disconnected")

    async def _handle_message(self, message: str):
        """Parse and dispatch incoming WebSocket messages."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse message: {message[:200]}")
            return

        # Combined streams wrap in { "stream": "...", "data": {...} }
        if "stream" in data and "data" in data:
            stream_name = data["stream"]
            payload = data["data"]
        else:
            # Single stream format
            stream_name = None
            payload = data

        # Dispatch by event type
        event_type = payload.get("e", "")

        if event_type == "kline":
            if self._kline_callback:
                kline = payload.get("k", {})
                self._kline_callback({
                    "event_type": "kline",
                    "symbol": payload.get("s", ""),
                    "event_time": payload.get("E", 0),
                    "kline": {
                        "open_time": kline.get("t", 0),
                        "close_time": kline.get("T", 0),
                        "symbol": kline.get("s", ""),
                        "open": float(kline.get("o", 0)),
                        "high": float(kline.get("h", 0)),
                        "low": float(kline.get("l", 0)),
                        "close": float(kline.get("c", 0)),
                        "volume": float(kline.get("v", 0)),
                        "quote_volume": float(kline.get("q", 0)),
                        "trades": int(kline.get("n", 0)),
                        "is_closed": kline.get("x", False),
                    }
                })

        elif event_type == "markPriceUpdate":
            if self._mark_price_callback:
                self._mark_price_callback({
                    "event_type": "markPrice",
                    "symbol": payload.get("s", ""),
                    "event_time": payload.get("E", 0),
                    "mark_price": float(payload.get("p", 0)),
                    "index_price": float(payload.get("i", 0)),
                    "funding_rate": float(payload.get("r", 0)),
                    "next_funding_time": payload.get("T", 0),
                })

        elif event_type == "24hrMiniTicker":
            if self._ticker_callback:
                self._ticker_callback({
                    "event_type": "miniTicker",
                    "symbol": payload.get("s", ""),
                    "event_time": payload.get("E", 0),
                    "close_price": float(payload.get("c", 0)),
                    "open_price": float(payload.get("o", 0)),
                    "high_price": float(payload.get("h", 0)),
                    "low_price": float(payload.get("l", 0)),
                    "volume": float(payload.get("v", 0)),
                    "quote_volume": float(payload.get("q", 0)),
                })

    async def disconnect(self):
        """Close the WebSocket connection and stop reconnection loop."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    @staticmethod
    def build_stream_name(symbol: str, stream_type: str) -> str:
        """
        Build a Binance stream name from symbol and stream type.

        Args:
            symbol: Trading symbol (e.g. 'btcusdt', 'ethusdt').
            stream_type: Stream type (e.g. 'kline_1h', 'miniTicker', 'markPrice@1s').

        Returns:
            Formatted stream name (e.g. 'btcusdt@kline_1h').
        """
        return f"{symbol.lower()}@{stream_type}"
