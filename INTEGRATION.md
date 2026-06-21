# Aether (以太) — Integration Guide

## Overview

Aether is a modular, event-driven quantitative trading system for Binance USDT-M Futures. Each module is independently testable with well-defined interfaces.

## Module Interface Reference

### Config (`config.settings`)

```python
from config.settings import get_config
cfg = get_config()
# cfg.api_key, cfg.api_secret, cfg.testnet, cfg.symbols, cfg.default_timeframe
```

### Data Layer (`data.*`)

| Class | Import | Key Methods |
|-------|--------|-------------|
| BinanceDataCollector | `data.collector` | `fetch_klines()`, `fetch_historical()`, `fetch_current_klines()` |
| MarketStorage | `data.storage` | `save_klines()`, `load_klines()`, `log_trade()`, `vacuum()`, `prune_old_klines()` |
| BinanceWebSocket | `data.websocket_stream` | `connect_combined_stream()`, callbacks |

### Execution (`execution.*`)

| Class | Import | Key Methods |
|-------|--------|-------------|
| BinanceFuturesClient | `execution.client` | `get_balance()`, `get_positions()`, `place_order()`, `cancel_order()`, `get_ticker()`, `get_exchange_info()` |
| OrderExecutionEngine | `execution.engine` | `execute_signal(signal_dict, account_info)` |

### Risk (`risk.manager`)

```python
from risk.manager import RiskManager
risk = RiskManager(max_positions=3, max_leverage=10)
result = risk.check_signal(signal_dict, account_info_dict)
# result.action in ('ALLOW', 'REJECT', 'REDUCE')
```

### Strategy (`strategy.*`)

```python
from strategy.base import BaseStrategy, Signal, SignalType
from strategy.manager import StrategyManager

# Register strategies
mgr = StrategyManager()
mgr.load_from_yaml('config/strategies.yaml')  # or register() manually

# Feed data and get signals
mgr.feed_data_only('BTC/USDT', '1h', df)
signals = mgr.generate_all_signals('BTC/USDT')
```

### Backtest (`backtest.engine`)

```python
from backtest.engine import BacktestEngine
engine = BacktestEngine(initial_capital=10000, commission=0.0004)
result = engine.run(df, signal_series)
engine.print_report(result)
```

## Data Flow

```
Market Data (ccxt/REST)
    │
    ▼
BinanceDataCollector  ──► MarketStorage (SQLite)
    │
    ▼
StrategyManager.feed_data_only()
    │
    ▼
Strategy.generate_signal()  ──► Signal[]
    │
    ▼
RiskManager.check_signal()
    │
    ▼
OrderExecutionEngine.execute_signal()
    │
    ▼
BinanceFuturesClient.place_order()
    │
    ▼
Trade Logged → MarketStorage.log_trade()
```

## Adding a New Strategy

1. Create `strategy/examples/my_strategy.py`:
```python
from strategy.base import BaseStrategy, Signal, SignalType

class MyStrategy(BaseStrategy):
    def generate_signal(self, symbol: str) -> Signal:
        df = self.get_data(symbol)
        # Your logic here
        return Signal(SignalType.HOLD, symbol)
```

2. Register in `config/strategies.yaml`:
```yaml
strategies:
  - name: MyStrategy
    class: strategy.examples.my_strategy.MyStrategy
    enabled: true
    params:
      symbols: [BTC/USDT]
      timeframes: [1h]
```

## Error Handling

All external API calls have ccxt + direct REST fallback. Database operations use parameterized queries. Rate limiting is handled by ccxt's built-in rate limiter.
