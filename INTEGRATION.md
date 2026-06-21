# Integration Guide — Data Layer

This document explains how other agents (Execution, Strategy, Backtest, Risk, Monitor) can import and use the Data Layer modules.

---

## 1. Configuration Interface

```python
from config.settings import get_config, reset_config

cfg = get_config()  # Returns Config dataclass singleton

# Key attributes:
cfg.api_key          # str  — Binance API key from .env
cfg.api_secret       # str  — Binance API secret from .env
cfg.testnet          # bool — True when BINANCE_TESTNET=true
cfg.db_path          # str  — Absolute path to SQLite database (data/market.db)
cfg.default_symbol   # str  — "BTC/USDT"
cfg.default_timeframe # str — "1h"
cfg.default_leverage # int  — 5
cfg.symbols          # list — ["BTC/USDT", "ETH/USDT"]
```

The `.env` file at the project root is auto-loaded via `python-dotenv`.

---

## 2. Data Collector (`data.collector.BinanceDataCollector`)

Fetches historical klines from Binance Futures (auto-selects testnet/mainnet based on config).

```python
from data.collector import BinanceDataCollector

collector = BinanceDataCollector()

# Get recent candles
df = collector.fetch_current_klines("BTC/USDT", "1h", lookback_bars=500)
# Returns DataFrame with columns:
#   open_time, open, high, low, close, volume, quote_volume, trades_count

# Get a specific batch
df = collector.fetch_klines("ETH/USDT", "5m", since=1719000000000, limit=100)

# Get multi-day history (handles pagination >1000 candles)
df = collector.fetch_historical("BTC/USDT", "1h", days=30)

# Direct access to ccxt exchange for custom calls
info = collector.exchange.fetch_ticker("BTC/USDT")
```

---

## 3. WebSocket Streamer (`data.websocket_stream.BinanceWebSocket`)

Real-time streaming via Binance Futures WebSocket. Supports klines, mark price, and mini-tickers.

```python
import asyncio
from data.websocket_stream import BinanceWebSocket

ws = BinanceWebSocket(testnet=True)

# Register callbacks
@ws.on_kline
def handle_kline(data):
    print(f"Kline: {data['symbol']} close={data['kline']['close']}")

@ws.on_mark_price
def handle_mark(data):
    print(f"Mark: {data['symbol']} price={data['mark_price']}")

@ws.on_ticker
def handle_ticker(data):
    print(f"Ticker: {data['symbol']} last={data['close_price']}")

# Connect to combined streams
async def main():
    streams = [
        ws.build_stream_name("btcusdt", "kline_1h"),
        ws.build_stream_name("ethusdt", "kline_1m"),
        ws.build_stream_name("btcusdt", "markPrice@1s"),
        ws.build_stream_name("btcusdt", "miniTicker"),
    ]
    await ws.connect_combined_stream(streams)

asyncio.run(main())
```

**Supported stream types:**
- `kline_1m`, `kline_5m`, `kline_15m`, `kline_1h`, `kline_4h`, `kline_1d`
- `markPrice@1s`
- `miniTicker`

---

## 4. Storage Layer (`data.storage.MarketStorage`)

SQLite-backed persistence at `<project_root>/data/market.db`.

```python
from data.storage import MarketStorage

storage = MarketStorage()  # Uses db_path from config

# Save klines (DataFrame must have columns: open_time, open, high, low, close, volume, quote_volume, trades_count)
storage.save_klines(df, "BTC/USDT", "1h")

# Load klines with optional time range (timestamps in ms)
df = storage.load_klines("BTC/USDT", "1h", start=1719000000000, end=1720000000000)

# Save trades (list of dicts)
trades = [
    {"symbol": "BTC/USDT", "trade_id": 12345, "price": 65100.0, "quantity": 0.1,
     "time": 1719001000000, "is_buyer_maker": 0},
]
storage.save_trades(trades)
```

**Database tables:**
- `klines(symbol, timeframe, open_time, open, high, low, close, volume, quote_volume, trades_count)` — PK: (symbol, timeframe, open_time)
- `trades(symbol, trade_id, price, quantity, time, is_buyer_maker)` — PK: (symbol, trade_id)

---

## 5. Typical Workflow for Other Agents

```python
from config.settings import get_config
from data.collector import BinanceDataCollector
from data.storage import MarketStorage

cfg = get_config()
collector = BinanceDataCollector()
storage = MarketStorage()

# Fetch + store historical data
for symbol in cfg.symbols:
    df = collector.fetch_current_klines(symbol, cfg.default_timeframe, 500)
    storage.save_klines(df, symbol, cfg.default_timeframe)

# Load data for strategy/backtest
df = storage.load_klines("BTC/USDT", "1h")
print(f"Loaded {len(df)} candles")
```

---

## 6. File Locations

```
binance_quant/
├── config/settings.py        # Config dataclass, get_config()
├── data/
│   ├── collector.py          # BinanceDataCollector
│   ├── websocket_stream.py   # BinanceWebSocket
│   ├── storage.py            # MarketStorage
│   └── market.db             # SQLite database (created at runtime)
├── .env                      # API keys (gitignored)
└── requirements.txt          # Python dependencies
```
