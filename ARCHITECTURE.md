# Aether (以太) — System Architecture

## Overview

Aether is a fully automated quantitative trading system for Binance USDT-M Futures. It supports backtesting, paper trading (testnet), and live trading across multiple strategies and symbols.

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Aether (以太)                              │
│                  Binance USDT-M Futures Auto Trading                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐     │
│  │  config/ │    │  data/   │    │strategy/ │    │execution/│     │
│  │settings  │    │collector │    │ manager  │    │  engine  │     │
│  │ .yaml    │    │ storage  │    │  base    │    │  client  │     │
│  └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘     │
│       │               │               │               │            │
│       ▼               ▼               ▼               ▼            │
│  ┌─────────────────────────────────────────────────────────┐      │
│  │                      main.py                              │      │
│  │          Orchestrator: backtest / paper / live            │      │
│  └──────────────────────────┬──────────────────────────────┘      │
│                             │                                      │
│       ┌─────────────────────┼─────────────────────┐               │
│       ▼                     ▼                     ▼                │
│  ┌──────────┐        ┌──────────┐         ┌──────────┐           │
│  │  risk/   │        │backtest/ │         │  main.py │           │
│  │ manager  │        │ engine   │         │--maint..│           │
│  └──────────┘        └──────────┘         └──────────┘           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Directory Structure

```
binance_quant/
├── main.py                    # Entry point — orchestration & CLI
├── config/
│   ├── settings.py            # Configuration (env vars, .env loading)
│   └── strategies.yaml        # Strategy declarations & parameters
├── data/
│   ├── collector.py           # Kline data fetching (ccxt)
│   ├── storage.py             # SQLite persistence & trade logging
│   ├── websocket_stream.py    # Real-time WebSocket streams
│   └── market.db              # SQLite database (auto-created)
├── strategy/
│   ├── base.py                # BaseStrategy, Signal, SignalType
│   ├── manager.py             # Multi-strategy orchestration
│   └── examples/
│       ├── ma_cross.py        # MA crossover + ATR stops
│       └── rsi_mean_reversion.py  # RSI mean reversion
├── execution/
│   ├── client.py              # Binance Futures REST client
│   └── engine.py              # Order execution (retry, precision)
├── risk/
│   └── manager.py             # Risk management (limits, stops)
├── backtest/
│   ├── engine.py              # Backtest engine (Sharpe, drawdown)
│   └── results/               # Backtest output (CSV, PNG)
├── ARCHITECTURE.md            # This file
├── CONTRIBUTING.md            # Contributor guide
└── USAGE.md                   # User guide
```

## Module Descriptions

### `config/` — Configuration

| File | Purpose | Key Interface |
|------|---------|---------------|
| `settings.py` | Load API keys, defaults from `.env` | `Config` dataclass, `get_config()` singleton |
| `strategies.yaml` | Declare active strategies and params | YAML dict → `StrategyManager.load_from_yaml()` |

### `data/` — Data Layer

| File | Purpose | Key Interface |
|------|---------|---------------|
| `collector.py` | Fetch kline data from Binance via ccxt | `fetch_historical()`, `fetch_current_klines()` |
| `storage.py` | SQLite persistence for klines, trades, trade logs | `save_klines()`, `log_trade()`, `vacuum()`, `prune_old_klines()` |
| `websocket_stream.py` | Real-time market data streams | WebSocket stream handler |

**Database Tables:**

| Table | Purpose |
|-------|---------|
| `klines` | OHLCV candle data (symbol, timeframe, open_time PK) |
| `trades` | Raw trade records (symbol, trade_id PK) |
| `trades_log` | Strategy trade journal (entry/exit, PnL, status) |

### `strategy/` — Strategy Layer

| File | Purpose | Key Interface |
|------|---------|---------------|
| `base.py` | Abstract base class for all strategies | `BaseStrategy`, `Signal`, `SignalType` enum |
| `manager.py` | Manages multiple strategies, routes data | `register()`, `load_from_yaml()`, `generate_all_signals()` |
| `examples/ma_cross.py` | Dual EMA crossover with ATR stops | `MACrossoverStrategy(fast_period, slow_period, atr_sl_mult, atr_tp_mult)` |
| `examples/rsi_mean_reversion.py` | RSI mean reversion | `RSIMeanReversionStrategy(rsi_period, oversold, overbought)` |

**Signal Types:** `LONG`, `SHORT`, `CLOSE_LONG`, `CLOSE_SHORT`, `HOLD`

### `execution/` — Order Execution

| File | Purpose | Key Interface |
|------|---------|---------------|
| `client.py` | Binance Futures REST API wrapper | `get_balance()`, `get_ticker()`, `place_order()` |
| `engine.py` | Order execution with retry & precision | `execute_signal()` |

### `risk/` — Risk Management

| File | Purpose | Key Interface |
|------|---------|---------------|
| `manager.py` | Position limits, leverage, daily loss limit | `check_signal()`, `record_trade_pnl()`, `update_daily_balance()` |

### `backtest/` — Backtesting

| File | Purpose | Key Interface |
|------|---------|---------------|
| `engine.py` | Backtest engine (Sharpe, max drawdown, win rate) | `run(df, signals)`, `print_report()` |

## Data Flow

```
                    ┌──────────────────────┐
                    │   Binance Exchange    │
                    │  (testnet or live)    │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  data/collector.py    │
                    │  (ccxt → DataFrame)   │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                 ▼
     ┌────────────┐   ┌──────────────┐   ┌──────────────┐
     │ storage.py │   │ strategy/     │   │ execution/   │
     │ (SQLite)   │   │ manager.py    │   │ engine.py    │
     │            │   │              │   │              │
     │ save_klines│   │ feed_data()  │   │execute_signal│
     │ log_trade  │   │ generate_    │   │ place_order  │
     │ prune      │   │ signal()     │   │              │
     └────────────┘   └──────┬───────┘   └──────┬───────┘
                             │                  │
                             ▼                  │
                    ┌──────────────┐            │
                    │ risk/manager │◄───────────┘
                    │ .check_signal│
                    │ (REJECT/     │
                    │  REDUCE/OK)  │
                    └──────────────┘
```

1. **Data Collection**: `BinanceDataCollector` fetches klines from Binance via ccxt
2. **Storage**: `MarketStorage` persists klines to SQLite, auto-prunes old data
3. **Strategy**: `StrategyManager` feeds data to registered strategies, collects `Signal` objects
4. **Risk**: `RiskManager` validates signals against position limits, leverage, daily loss
5. **Execution**: `OrderExecutionEngine` places orders via `BinanceFuturesClient`
6. **Trade Logging**: Open/close events logged to `trades_log` table for audit trail

## Error Handling Strategy

- **Data Layer**: Exceptions during data fetch are caught and logged; system continues with next iteration
- **Strategy Layer**: Strategies return `HOLD` signals when data is insufficient
- **Execution Layer**: Order failures are logged as errors; system retries on next iteration
- **Risk Layer**: Risk violations result in `REJECT` with clear reason; no order is placed
- **Main Loop**: Top-level exception handler catches and logs errors without crashing
- **Shutdown**: SIGINT/SIGTERM triggers graceful shutdown with database vacuum

## Extension Points

### Adding a New Strategy

1. Create a file in `strategy/examples/` (e.g., `my_strategy.py`)
2. Inherit from `BaseStrategy` and implement `generate_signal()`
3. Add entry to `config/strategies.yaml`:

```yaml
strategies:
  - name: My_Strat
    class: strategy.examples.my_strategy.MyStrategy
    enabled: true
    params:
      symbols: [BTC/USDT, ETH/USDT]
      timeframes: [1h]
      my_param: 42
```

### Adding a New Exchange

1. Implement a new client class (similar to `execution/client.py`)
2. Implement a new collector (similar to `data/collector.py`)
3. Create exchange-specific config in `config/` or `.env`
4. Update `main.py` to support the new exchange mode

### Adding New Risk Rules

1. Add methods to `risk/manager.py`
2. Call new checks from `RiskManager.check_signal()`
3. Return appropriate `RiskResult` with `REJECT` / `REDUCE` / `OK`

### Database Schema Extensions

1. Add new table definitions to `storage.py::_init_tables()`
2. Add CRUD methods for the new table
3. Tables are auto-created on first `MarketStorage()` instantiation
