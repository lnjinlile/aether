"""
Historical kline data collector using ccxt.
Fetches data from Binance Futures (testnet or mainnet).
"""

import time
from datetime import datetime, timedelta, timezone
import pandas as pd


class BinanceDataCollector:
    """
    Collect historical klines from Binance Futures via ccxt.

    Automatically targets testnet when BINANCE_TESTNET=true in config.
    """

    def __init__(self, api_key: str = None, api_secret: str = None, testnet: bool = None):
        """
        Initialize the collector with Binance Futures credentials.

        Args:
            api_key: Binance API key. Loaded from config if None.
            api_secret: Binance API secret. Loaded from config if None.
            testnet: Whether to use testnet. Loaded from config if None.
        """
        from config.settings import get_config
        cfg = get_config()

        self.api_key = api_key or cfg.api_key
        self.api_secret = api_secret or cfg.api_secret
        self.testnet = testnet if testnet is not None else cfg.testnet

        self.exchange = self._create_exchange()

    def _create_exchange(self) -> "ccxt.Exchange":
        """Create and configure the ccxt Binance Futures exchange."""
        import ccxt  # lazy import: ~0.5s cost
        exchange = ccxt.binanceusdm({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "fetchCurrencies": False,
                "fetchLeverageBrackets": False,
                "fetchLeverageTiers": False,
            },
        })

        if self.testnet:
            # Manual URL override for testnet (avoid deprecated set_sandbox_mode)
            base = "https://testnet.binancefuture.com"
            api = exchange.urls.get("api", {})
            for key in list(api.keys()):
                if key.startswith("fapi"):
                    api[key] = base + "/fapi/v1"
                elif key.startswith("dapi"):
                    api[key] = base + "/dapi/v1"
                elif key in ("public", "private"):
                    api[key] = base + "/fapi/v1"
            # Monkey-patch unsupported endpoints
            exchange.fetch_leverage_brackets = lambda *a, **kw: {}
            exchange.fetch_leverage_tiers = lambda *a, **kw: {}

        return exchange

    def fetch_klines(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: int = None,
        limit: int = 500
    ) -> pd.DataFrame:
        """
        Fetch a single batch of klines from the exchange.

        Uses Binance's native REST endpoint (public_get_klines) to get all 12
        fields including quote_volume and trades_count, which ccxt's normalized
        fetch_ohlcv strips down to 6 fields.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            timeframe: Kline interval (e.g. '1h', '1m', '5m').
            since: Start time in milliseconds (UTC). Defaults to 500 candles before now.
            limit: Max number of candles to fetch (max 1500 for Binance).

        Returns:
            DataFrame with columns: open_time, open, high, low, close,
                                    volume, quote_volume, trades_count
        """
        # Convert CCXT symbol format (BTC/USDT) to Binance raw format (BTCUSDT)
        raw_symbol = symbol.replace("/", "")

        params = {
            "symbol": raw_symbol,
            "interval": timeframe,
            "limit": limit,
        }
        if since is not None:
            params["startTime"] = since

        raw = self.exchange.public_get_klines(params)

        # Binance kline array fields:
        # 0:openTime  1:open  2:high  3:low  4:close  5:volume
        # 6:closeTime  7:quoteAssetVolume  8:numberOfTrades
        # 9:takerBuyBaseVol  10:takerBuyQuoteVol  11:ignore
        data = []
        for r in raw:
            data.append({
                "open_time": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "quote_volume": float(r[7]),
                "trades_count": int(r[8]),
            })

        df = pd.DataFrame(data, columns=[
            "open_time", "open", "high", "low", "close",
            "volume", "quote_volume", "trades_count"
        ])
        return df

    def fetch_historical(
        self,
        symbol: str,
        timeframe: str = "1h",
        days: int = 30
    ) -> pd.DataFrame:
        """
        Fetch multiple days of historical klines with pagination.

        Binance limits each request to ~1500 candles. This method paginates
        through multiple requests to fetch the full requested range.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            timeframe: Kline interval.
            days: Number of days of historical data to fetch.

        Returns:
            DataFrame with all klines in the requested range, sorted by time.
        """
        import logging
        logger = logging.getLogger("collector")

        timeframe_ms = self._timeframe_to_ms(timeframe)
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time = end_time - (days * 24 * 60 * 60 * 1000)

        # Calculate expected total bars for progress tracking
        total_ms = end_time - start_time
        expected_bars = total_ms // timeframe_ms

        all_frames = []
        current_since = start_time
        bars_fetched = 0
        page = 0
        MAX_LIMIT = 1500  # Binance max per request

        logger.info(
            "Fetching %s %s — %d days (~%d bars expected, %d per page)",
            symbol, timeframe, days, expected_bars, MAX_LIMIT
        )

        while current_since < end_time:
            page += 1
            df = self.fetch_klines(
                symbol=symbol,
                timeframe=timeframe,
                since=current_since,
                limit=MAX_LIMIT,
            )

            if df.empty:
                logger.info(
                    "  [%s %s] page %d: 0 bars (API returned empty, stopping)",
                    symbol, timeframe, page
                )
                break

            batch_size = len(df)
            all_frames.append(df)
            bars_fetched += batch_size

            # Progress log
            pct = min(100, round(bars_fetched / max(1, expected_bars) * 100))
            first_ts = datetime.fromtimestamp(df["open_time"].iloc[0] / 1000, tz=timezone.utc)
            last_ts = datetime.fromtimestamp(df["open_time"].iloc[-1] / 1000, tz=timezone.utc)
            logger.info(
                "  [%s %s] page %d: %d bars | %s → %s | %d/%d (%d%%)",
                symbol, timeframe, page, batch_size,
                first_ts.strftime("%Y-%m-%d"), last_ts.strftime("%Y-%m-%d"),
                bars_fetched, expected_bars, pct
            )

            # Advance past the last candle
            last_open_time = df["open_time"].iloc[-1]
            current_since = last_open_time + timeframe_ms

            # Stop if we've reached the present time
            if current_since >= end_time:
                logger.info(
                    "  [%s %s] reached present time — stopping",
                    symbol, timeframe
                )
                break

            # Rate limit
            time.sleep(0.1)

        if not all_frames:
            return pd.DataFrame(columns=[
                "open_time", "open", "high", "low", "close",
                "volume", "quote_volume", "trades_count"
            ])

        result = pd.concat(all_frames, ignore_index=True)
        result.drop_duplicates(subset=["open_time"], inplace=True)
        result.sort_values("open_time", inplace=True)
        result.reset_index(drop=True, inplace=True)

        logger.info(
            "  [%s %s] DONE: %d total bars over %d pages",
            symbol, timeframe, len(result), page
        )
        return result

    def fetch_current_klines(
        self,
        symbol: str,
        timeframe: str = "1h",
        lookback_bars: int = 500
    ) -> pd.DataFrame:
        """
        Fetch the most recent N candles.

        Args:
            symbol: Trading symbol.
            timeframe: Kline interval.
            lookback_bars: Number of recent candles to fetch (max 1500).

        Returns:
            DataFrame with recent klines.
        """
        timeframe_ms = self._timeframe_to_ms(timeframe)
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        since = end_time - (lookback_bars * timeframe_ms)

        return self.fetch_klines(
            symbol=symbol,
            timeframe=timeframe,
            since=since,
            limit=lookback_bars,
        )

    def fetch_orderbook(self, symbol: str, depth: int = 20) -> dict:
        """
        Fetch order book depth data from Binance Futures.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            depth: Number of price levels to fetch per side (default 20).

        Returns:
            dict with keys: bids, asks, timestamp.
            bids/asks are lists of [price, quantity] pairs.
        """
        ob = self.exchange.fetch_order_book(symbol, depth)
        return {
            "bids": ob["bids"],
            "asks": ob["asks"],
            "timestamp": ob.get("timestamp") or ob.get("datetime") or (time.time() * 1000),
        }

    def fetch_funding_rate(self, symbol: str, limit: int = 100) -> list:
        """
        Fetch historical funding rates from Binance Futures.

        Uses ccxt's fetch_funding_rate_history which hits
        GET /fapi/v1/fundingRate under the hood.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            limit: Max number of funding rate records to fetch.

        Returns:
            List of dicts, each with keys: fundingTime, fundingRate, markPrice.
        """
        rates = self.exchange.fetch_funding_rate_history(symbol, limit=limit)
        result = []
        for r in rates:
            result.append({
                "fundingTime": r.get("timestamp", r.get("fundingTime", 0)),
                "fundingRate": r.get("fundingRate", r.get("rate", 0.0)),
                "markPrice": r.get("markPrice", 0.0),
            })
        return result

    @staticmethod
    def _timeframe_to_ms(timeframe: str) -> int:
        """Convert a timeframe string (e.g. '1h', '5m') to milliseconds."""
        unit = timeframe[-1]
        value = int(timeframe[:-1])

        unit_map = {
            "m": 60 * 1000,
            "h": 60 * 60 * 1000,
            "d": 24 * 60 * 60 * 1000,
            "w": 7 * 24 * 60 * 60 * 1000,
        }

        if unit not in unit_map:
            raise ValueError(f"Unsupported timeframe unit: {unit}")

        return value * unit_map[unit]
