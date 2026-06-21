"""Strategy Manager - orchestrates multiple strategies."""

import importlib
import logging
import os
from typing import Dict, List, Optional

import pandas as pd
import yaml

from .base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class StrategyManager:
    """Manages multiple strategies, routing data and collecting signals.

    Usage:
        mgr = StrategyManager()
        mgr.register(ma_cross_strategy)
        mgr.register(rsi_strategy)

        # Or load from YAML config:
        mgr.load_from_yaml('config/strategies.yaml')

        # On new kline data:
        mgr.on_kline_update("BTC/USDT", "15m", df)
        signals = mgr.get_pending_signals()
    """

    def __init__(self):
        self._strategies: Dict[str, BaseStrategy] = {}
        self._pending_signals: List[Signal] = []

    def register(self, strategy: BaseStrategy):
        """Register a strategy for management."""
        self._strategies[strategy.name] = strategy

    def unregister(self, name: str):
        """Remove a strategy by name."""
        self._strategies.pop(name, None)

    def get_strategy(self, name: str) -> Optional[BaseStrategy]:
        """Get a registered strategy by name."""
        return self._strategies.get(name)

    def get_active_strategies(self) -> List[str]:
        """Return list of registered strategy names."""
        return list(self._strategies.keys())

    def on_kline_update(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Feed new kline data to all relevant strategies and collect signals.

        A strategy receives data if the symbol and timeframe match its
        configuration. After feeding, generate_signal() is called for
        each strategy-symbol pair.

        Args:
            symbol: Trading symbol (e.g., 'BTC/USDT')
            timeframe: Candle timeframe (e.g., '15m')
            df: OHLCV DataFrame
        """
        new_signals = []

        for name, strategy in self._strategies.items():
            if symbol not in strategy.symbols or timeframe not in strategy.timeframes:
                continue

            strategy.feed_data(symbol, timeframe, df)
            signal = strategy.generate_signal(symbol)
            new_signals.append(signal)

            # Track position when a non-HOLD signal is generated
            if signal.type.name.startswith("CLOSE"):
                # Remove position on close signals
                strategy.on_order_filled(symbol, "sell", signal.price, signal.quantity)
            elif signal.type in (type(signal.type).LONG, type(signal.type).SHORT):
                side = "buy" if signal.type.value == "LONG" else "sell"
                strategy.on_order_filled(symbol, side, signal.price, signal.quantity)

        self._pending_signals = new_signals

    def get_pending_signals(self) -> List[Signal]:
        """Return and clear the list of pending signals from last update."""
        signals = self._pending_signals
        self._pending_signals = []
        return signals

    def feed_data_only(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Feed data to all relevant strategies without generating signals."""
        for name, strategy in self._strategies.items():
            if symbol in strategy.symbols and timeframe in strategy.timeframes:
                strategy.feed_data(symbol, timeframe, df)

    def generate_all_signals(self, symbol: str) -> Dict[str, Signal]:
        """Generate signals for a symbol from all registered strategies.

        Returns:
            dict mapping strategy name -> Signal
        """
        results = {}
        for name, strategy in self._strategies.items():
            if symbol in strategy.symbols:
                results[name] = strategy.generate_signal(symbol)
        return results

    @classmethod
    def load_from_yaml(cls, yaml_path: str) -> "StrategyManager":
        """Create a StrategyManager and load strategies from a YAML config file.

        The YAML file should have the following structure:

            strategies:
              - name: MA_Cross
                class: strategy.examples.ma_cross.MACrossoverStrategy
                enabled: true
                params:
                  symbols: [BTC/USDT, ETH/USDT]
                  timeframes: [1h]
                  fast_period: 7
                  ...

        Only strategies with ``enabled: true`` are registered.

        Args:
            yaml_path: Path to the YAML configuration file.

        Returns:
            StrategyManager instance with strategies loaded and registered.

        Raises:
            FileNotFoundError: If the YAML file doesn't exist.
            ImportError: If a strategy class cannot be imported.
        """
        if not os.path.isabs(yaml_path):
            # Resolve relative paths from the project root
            from config.settings import _PROJECT_ROOT
            yaml_path = os.path.join(_PROJECT_ROOT, yaml_path)

        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)

        mgr = cls()
        strategies = config.get("strategies", [])

        for strat_cfg in strategies:
            if not strat_cfg.get("enabled", True):
                logger.info("Strategy '%s' is disabled, skipping.", strat_cfg.get("name", "unknown"))
                continue

            name = strat_cfg["name"]
            class_path = strat_cfg["class"]
            params = strat_cfg.get("params", {})

            # Dynamically import the strategy class
            module_path, class_name = class_path.rsplit(".", 1)
            try:
                module = importlib.import_module(module_path)
                strategy_cls = getattr(module, class_name)
            except (ImportError, AttributeError) as e:
                logger.error("Failed to import strategy '%s' from '%s': %s",
                             name, class_path, e)
                raise ImportError(f"Cannot import {class_path}: {e}") from e

            # Instantiate with params from YAML (name is passed explicitly)
            try:
                params_with_name = {"name": name, **params}
                strategy = strategy_cls(**params_with_name)
                mgr.register(strategy)
                logger.info("Loaded strategy '%s' from %s (params: %s)", name, class_path, params)
            except TypeError as e:
                # Bad config params — log and skip this strategy, don't crash the whole load
                logger.error("Failed to instantiate strategy '%s': %s — skipping (check params)", name, e)
                continue

        logger.info("Loaded %d strategies from %s", len(mgr), yaml_path)
        return mgr

    def __len__(self) -> int:
        return len(self._strategies)

    def __repr__(self) -> str:
        return f"StrategyManager(strategies={list(self._strategies.keys())})"
