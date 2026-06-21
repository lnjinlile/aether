# Contributing to Aether (以太)

## Development Environment Setup

### Prerequisites

- Python 3.11+
- Git
- A Binance account (testnet recommended for development)

### Setup

```bash
# Clone the repository
cd ~/binance_quant

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

If `requirements.txt` doesn't exist yet, install the core dependencies:

```bash
pip install pandas numpy ccxt python-dotenv pyyaml sqlite3
```

### Configure API Keys

Copy `.env.example` to `.env` (or create from scratch):

```bash
# .env
BINANCE_API_KEY=your_testnet_api_key
BINANCE_API_SECRET=your_testnet_api_secret
BINANCE_TESTNET=true
```

Get testnet API keys from: https://testnet.binancefuture.com/

### Verify Setup

```bash
# Test database connectivity
python3 -c "from data.storage import MarketStorage; s=MarketStorage(); print('DB OK')"

# Test strategy loading
python3 -c "
from strategy.manager import StrategyManager
m = StrategyManager.load_from_yaml('config/strategies.yaml')
print(f'Loaded {len(m)} strategies: {m.get_active_strategies()}')
"
```

## How to Add a New Strategy

### Step 1: Create the Strategy Class

Create a file `strategy/examples/my_strategy.py`:

```python
"""My custom trading strategy."""

from typing import List, Optional
import pandas as pd
from ..base import BaseStrategy, Signal, SignalType

class MyStrategy(BaseStrategy):
    """Description of what this strategy does."""

    def __init__(
        self,
        name: str = "My_Strat",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        my_param: int = 42,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params = {"my_param": my_param}

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute indicators when data is fed."""
        key = (symbol, timeframe)
        # Compute your indicators here
        # Store in self._indicators[key] = DataFrame

    def generate_signal(self, symbol: str) -> Signal:
        """Evaluate current state and generate a signal."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)

        df = self._data.get(key)
        if df is None:
            return Signal(type=SignalType.HOLD, symbol=symbol,
                         reason="No data", strategy_name=self.name)

        # Your signal logic here
        # Return Signal(SignalType.LONG, symbol, ...) for entries
        # Return Signal(SignalType.HOLD, symbol, ...) when no action

        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            reason="No signal",
            strategy_name=self.name,
        )
```

### Step 2: Register in YAML Config

Add to `config/strategies.yaml`:

```yaml
  - name: My_Strat
    class: strategy.examples.my_strategy.MyStrategy
    enabled: true
    params:
      symbols: [BTC/USDT, ETH/USDT]
      timeframes: [1h]
      my_param: 42
```

### Step 3: Test

```bash
# Test strategy loading
python3 -c "
from strategy.manager import StrategyManager
m = StrategyManager.load_from_yaml('config/strategies.yaml')
print(m.get_active_strategies())
"
```

## How to Run Tests

```bash
# Run the existing test suite
cd ~/binance_quant
source venv/bin/activate
python3 test_framework.py
python3 test_trading.py

# Quick smoke tests
python3 -c "from data.storage import MarketStorage; s=MarketStorage(); s.log_trade({'symbol':'BTCUSDT','side':'LONG','entry_price':64000,'quantity':0.001,'strategy_name':'test','status':'OPEN'}); print('trade logged')"

python3 -c "from strategy.manager import StrategyManager; m=StrategyManager(); m.load_from_yaml('config/strategies.yaml'); print(f'Loaded {len(m)} strategies: {m.get_active_strategies()}')"

# Database maintenance
python3 main.py --maintenance
```

### Writing New Tests

Tests go in the project root with `test_` prefix or in `tests/` directory:

```python
# test_my_strategy.py
import pandas as pd
from strategy.examples.my_strategy import MyStrategy
from strategy.base import SignalType

def test_my_strategy_creates_signal():
    strat = MyStrategy(symbols=["BTC/USDT"], timeframes=["1h"])
    # Create some test data
    df = pd.DataFrame({...})
    strat.feed_data("BTC/USDT", "1h", df)
    signal = strat.generate_signal("BTC/USDT")
    assert signal.type in (SignalType.LONG, SignalType.SHORT, SignalType.HOLD)
```

## Code Style Guidelines

### Python Style

- Follow [PEP 8](https://peps.python.org/pep-0008/)
- 4 spaces for indentation (no tabs)
- Max line length: 100 characters (relaxed from 79 for readability)
- Use type hints for all function signatures
- Document classes and public methods with docstrings (Google style)

### Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Modules | `snake_case` | `ma_cross.py` |
| Classes | `PascalCase` | `MACrossoverStrategy` |
| Functions/methods | `snake_case` | `generate_signal()` |
| Variables | `snake_case` | `entry_price` |
| Constants | `UPPER_SNAKE` | `MAX_POSITIONS` |
| Private members | `_leading_underscore` | `_positions` |

### Strategy Design Principles

1. **Single Responsibility**: A strategy does one thing (e.g., MA cross, RSI reversion)
2. **Configurable**: All parameters exposed via `self.params` dict
3. **Stateless Signal Generation**: `generate_signal()` uses only stored data + indicators
4. **Handle Missing Data**: Return `HOLD` when data is insufficient
5. **Use `_preprocess()`**: Compute indicators once when data arrives, not on every `generate_signal()`

### Database Best Practices

- Always use **parameterized queries** (never string interpolation)
- Close connections in `finally` blocks
- Use `IF NOT EXISTS` for table creation
- Add indexes on frequently queried columns
- Call `vacuum()` periodically for space reclamation

### Git Workflow

1. Create a feature branch: `git checkout -b feature/my-strategy`
2. Make focused, atomic commits
3. Write clear commit messages: `feat: add Bollinger Band strategy`
4. Test before submitting: run `test_framework.py` and `test_trading.py`
5. Update documentation if adding new features

### Commit Message Format

```
<type>: <short description>

<optional body with details>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

## Project Configuration Reference

### Strategy YAML Schema

```yaml
strategies:
  - name: string          # Unique strategy identifier
    class: string         # Python import path (module.Class)
    enabled: boolean      # true = active, false = skipped
    params:               # Strategy-specific kwargs
      symbols: [string]   # List of trading symbols
      timeframes: [string] # List of timeframes
      # ... strategy-specific parameters
```

### Database Tables

| Table | Columns | Purpose |
|-------|---------|---------|
| `klines` | symbol, timeframe, open_time (PK), OHLCV + volume | Candlestick data |
| `trades` | symbol, trade_id (PK), price, quantity, time | Raw exchange trades |
| `trades_log` | id (PK), symbol, side, entry/exit price/time, qty, pnl, fee, strategy_name, reason, status | Strategy trade journal |

## Getting Help

- Architecture overview: `ARCHITECTURE.md`
- User guide: `USAGE.md`
- Integration notes: `INTEGRATION.md`, `backtest/INTEGRATION.md`
