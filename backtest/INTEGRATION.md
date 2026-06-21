# Strategy & Backtest Framework - Integration Guide

## Overview

The strategy and backtest modules provide:
- **Base strategy framework** with signal generation, position tracking, and indicator computation
- **Backtest engine** with full metrics (Sharpe, drawdown, win rate, profit factor)
- **Two example strategies**: MA Crossover and RSI Mean Reversion
- **Strategy manager** for orchestrating multiple strategies

## Module Structure

```
strategy/
├── __init__.py          # Exports: BaseStrategy, Signal, SignalType
├── base.py              # ABC BaseStrategy, Signal dataclass, SignalType enum
├── manager.py           # StrategyManager - multi-strategy orchestration
└── examples/
    ├── __init__.py
    ├── ma_cross.py      # MACrossoverStrategy - dual EMA crossover
    └── rsi_mean_reversion.py  # RSIMeanReversionStrategy

backtest/
├── __init__.py          # Exports: BacktestEngine
├── engine.py            # BacktestEngine - event-driven simulation
└── results/             # Output directory for reports/plots
```

## Quick Start

### 1. Creating a Custom Strategy

```python
from strategy.base import BaseStrategy, Signal, SignalType

class MyStrategy(BaseStrategy):
    def __init__(self, name="MyStrat", symbols=["BTC/USDT"], timeframes=["15m"]):
        super().__init__(name=name, symbols=symbols, timeframes=timeframes)
        self.params = {"lookback": 20}

    def _preprocess(self, symbol, timeframe, df):
        """Compute indicators when data arrives."""
        key = (symbol, timeframe)
        ind = pd.DataFrame(index=df.index)
        ind["sma"] = df["close"].rolling(self.params["lookback"]).mean()
        self._indicators[key] = ind

    def generate_signal(self, symbol):
        """Return a Signal for the current bar."""
        key = (symbol, self.timeframes[0])
        ind = self._indicators.get(key)
        df = self._data.get(key)
        if ind is None:
            return Signal(type=SignalType.HOLD, symbol=symbol)

        price = float(df["close"].iloc[-1])
        sma = float(ind["sma"].iloc[-1])

        if not self.has_position(symbol) and price > sma:
            return Signal(
                type=SignalType.LONG,
                symbol=symbol,
                price=price,
                quantity=0.001,
                stop_loss=price * 0.95,
                take_profit=price * 1.10,
                reason=f"Price above SMA({self.params['lookback']})",
                confidence=0.7,
                leverage=3,
                strategy_name=self.name,
            )
        elif self.has_position(symbol) and price < sma:
            return Signal(type=SignalType.CLOSE_LONG, symbol=symbol, price=price)

        return Signal(type=SignalType.HOLD, symbol=symbol)
```

### 2. Running a Backtest

```python
from backtest.engine import BacktestEngine
import pandas as pd

# Load or generate OHLCV data
df = pd.read_csv("data/BTC_USDT_15m.csv", index_col=0, parse_dates=True)

# Generate signals from a strategy
strategy = MyStrategy()
strategy.feed_data("BTC/USDT", "15m", df)

signals = []
for i in range(len(df)):
    strategy._preprocess("BTC/USDT", "15m", df.iloc[:i+1])
    signal = strategy.generate_signal("BTC/USDT")
    if signal.type == SignalType.LONG:
        signals.append(1)
    elif signal.type == SignalType.SHORT:
        signals.append(-1)
    else:
        signals.append(0 if i == 0 else signals[-1])  # maintain position

signal_series = pd.Series(signals, index=df.index)

# Run backtest
engine = BacktestEngine(initial_capital=10000, commission=0.0004, slippage=0.0001)
results = engine.run(df, signal_series, leverage=1)

# Print report
engine.print_report(results, title="My Strategy")

# Save equity curve plot
engine.plot_equity_curve(results, save_path="results/my_strat_equity.png")

# Access metrics
print(f"Sharpe: {results['sharpe_ratio']}, Win Rate: {results['win_rate']}%")
```

### 3. Using StrategyManager

```python
from strategy.manager import StrategyManager
from strategy.examples.ma_cross import MACrossoverStrategy
from strategy.examples.rsi_mean_reversion import RSIMeanReversionStrategy

mgr = StrategyManager()
mgr.register(MACrossoverStrategy(name="MA_Cross"))
mgr.register(RSIMeanReversionStrategy(name="RSI_MR"))

# On each new candle:
mgr.on_kline_update("BTC/USDT", "15m", new_candle_df)
signals = mgr.get_pending_signals()

for sig in signals:
    if sig.type != SignalType.HOLD:
        print(f"Strategy '{sig.strategy_name}': {sig.type.value} {sig.symbol} @ {sig.price}")

# Get all registered strategies
print(mgr.get_active_strategies())  # ['MA_Cross', 'RSI_MR']
```

## Signal Types

| SignalType    | Backtest Signal | Description                                |
|---------------|-----------------|--------------------------------------------|
| `LONG`        | `1`             | Open or switch to long position            |
| `SHORT`       | `-1`            | Open or switch to short position           |
| `CLOSE_LONG`  | `0`             | Close existing long position               |
| `CLOSE_SHORT` | `0`             | Close existing short position              |
| `HOLD`        | (same as prior) | No change to position                      |

## Backtest Engine API

### `BacktestEngine(initial_capital, commission, slippage)`

- `initial_capital`: Starting capital (default: 10000)
- `commission`: Fee per side (default: 0.0004 = 0.04%)
- `slippage`: Slippage fraction (default: 0.0001 = 0.01%)

### `run(df, signals, leverage) -> dict`

Returns a dictionary with:
- `equity_curve`: pd.Series of equity over time
- `total_return_pct`: Total return percentage
- `sharpe_ratio`: Annualized Sharpe ratio
- `max_drawdown_pct`: Maximum drawdown percentage
- `win_rate`: Percentage of winning trades
- `profit_factor`: Gross profit / gross loss
- `total_trades`: Number of completed trades
- `trade_log`: pd.DataFrame of individual trades
- `metrics`: dict with all computed metrics (+ avg win/loss, best/worst trade, final equity)

### `walk_forward(df, signal_func, train_days, test_days) -> List[dict]`

Rolling walk-forward backtest. `signal_func` receives (train_df, test_df, **kwargs).

### `generate_report(results, title) -> str`

Returns a formatted multi-line report string.

### `plot_equity_curve(results, save_path)`

Saves equity curve + drawdown as PNG. Falls back to ASCII art if matplotlib unavailable.

## Integration Points for Other Agents

### Data Layer → Strategy
```python
# data/collector.py or data/storage.py fetches OHLCV
df = collector.fetch_ohlcv("BTC/USDT", "15m", limit=500)

# Feed to strategy manager
strategy_manager.on_kline_update("BTC/USDT", "15m", df)
```

### Strategy → Trading Core
```python
# Trading core polls for signals
signals = strategy_manager.get_pending_signals()
for sig in signals:
    if sig.type == SignalType.LONG:
        trading_core.open_long(sig.symbol, sig.quantity, sig.leverage,
                               stop_loss=sig.stop_loss, take_profit=sig.take_profit)
    elif sig.type == SignalType.CLOSE_LONG:
        trading_core.close_position(sig.symbol, "LONG")
```

### Trading Core → Strategy (feedback)
```python
# When orders fill, notify strategy for position tracking
strategy.on_order_filled("BTC/USDT", "buy", 50000.0, 0.001)
```

## Example Strategies

### MACrossoverStrategy
- Dual EMA crossover (fast=7, slow=25)
- Golden cross → LONG, Death cross → SHORT
- ATR-based dynamic stop loss (2x ATR) and take profit (3x ATR)
- Reverse cross closes position
- Configurable cooldown bars between trades

### RSIMeanReversionStrategy
- RSI(14) with oversold=30, overbought=70 thresholds
- RSI < 30 → LONG, RSI > 70 → SHORT
- RSI crosses 50 → close position
- Fixed % stop loss and take profit
- Configurable cooldown bars

## Dependencies
- pandas, numpy
- matplotlib (optional, for equity curve plots)
- ccxt (for data collection in data/collector.py)
