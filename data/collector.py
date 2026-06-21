"""
Historical kline data collector using ccxt.
Fetches data from Binance Futures (testnet or mainnet).
"""

import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import ccxt


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

    def _create_exchange(self) -> ccxt.Exchange:
        """Create and configure the ccxt Binance Futures exchange."""
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

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            timeframe: Kline interval (e.g. '1h', '1m', '5m').
            since: Start time in milliseconds (UTC). Defaults to 500 candles before now.
            limit: Max number of candles to fetch (max 1500 for Binance).

        Returns:
            DataFrame with columns: open_time, open, high, low, close,
                                    volume, quote_volume, trades_count
        """
        raw = self.exchange.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            since=since,
            limit=limit,
        )

        df = pd.DataFrame(
            raw,
            columns=["open_time", "open", "high", "low", "close", "volume"]
        )

        # ccxt Binance futures returns additional columns
        if raw and len(raw[0]) > 6:
            df["quote_volume"] = [r[6] if len(r) > 6 else 0.0 for r in raw]
            df["trades_count"] = [int(r[7]) if len(r) > 7 else 0 for r in raw]
        else:
            df["quote_volume"] = 0.0
            df["trades_count"] = 0

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
        timeframe_ms = self._timeframe_to_ms(timeframe)
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time = end_time - (days * 24 * 60 * 60 * 1000)

        all_frames = []
        current_since = start_time

        while current_since < end_time:
            raw = self.exchange.fetch_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                since=current_since,
                limit=1000,
            )

            if not raw:
                break

            df = pd.DataFrame(
                raw,
                columns=["open_time", "open", "high", "low", "close", "volume"]
            )
            if raw and len(raw[0]) > 6:
                df["quote_volume"] = [r[6] if len(r) > 6 else 0.0 for r in raw]
                df["trades_count"] = [int(r[7]) if len(r) > 7 else 0 for r in raw]
            else:
                df["quote_volume"] = 0.0
                df["trades_count"] = 0

            all_frames.append(df)

            # Advance past the last candle
            last_open_time = df["open_time"].iloc[-1]
            current_since = last_open_time + timeframe_ms

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
