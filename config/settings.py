"""Configuration management for Binance Quant system."""

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# Load .env from project root (one level up from config/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(_ENV_PATH)


@dataclass
class Config:
    """Application configuration loaded from environment/.env."""

    # Binance API
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = True

    # Database
    db_path: str = os.path.join(_PROJECT_ROOT, "data", "market.db")

    # Trading defaults
    default_symbol: str = "BTC/USDT"
    default_timeframe: str = "1h"
    default_leverage: int = 5
    default_quantity: float = 0.001

    # Trading symbols
    symbols: list = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])

    # Risk management
    max_position_pct: float = 0.95
    default_stop_loss_pct: float = 0.05
    default_take_profit_pct: float = 0.10

    # Backtesting
    initial_capital: float = 10000.0
    commission: float = 0.0004
    slippage: float = 0.0001

    # Data
    data_dir: str = "data/cache"
    max_candles: int = 1000

    # Extra
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        """Load values from environment variables after dotenv has set them."""
        self.api_key = os.getenv("BINANCE_API_KEY", self.api_key)
        self.api_secret = os.getenv("BINANCE_API_SECRET", self.api_secret)
        testnet_val = os.getenv("BINANCE_TESTNET", str(self.testnet))
        self.testnet = testnet_val.lower() in ("true", "1", "yes")

        # Resolve relative db_path against project root
        if not os.path.isabs(self.db_path):
            self.db_path = os.path.join(_PROJECT_ROOT, self.db_path)

    @classmethod
    def from_env(cls, env_file: Optional[str] = None) -> "Config":
        """Load config, optionally specifying a custom .env file."""
        if env_file and os.path.exists(env_file):
            load_dotenv(env_file, override=True)
        return cls()


_config: Optional[Config] = None


def get_config() -> Config:
    """Get or create the global configuration singleton."""
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def reset_config():
    """Reset the config singleton (useful for testing)."""
    global _config
    _config = None
