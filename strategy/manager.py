"""Strategy Manager - orchestrates multiple strategies."""

from typing import Dict, List, Optional
import pandas as pd

from .base import BaseStrategy, Signal


class StrategyManager:
    """Manages multiple strategies, routing data and collecting signals.

    Usage:
        mgr = StrategyManager()
        mgr.register(ma_cross_strategy)
        mgr.register(rsi_strategy)

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

    def __len__(self) -> int:
        return len(self._strategies)

    def __repr__(self) -> str:
        return f"StrategyManager(strategies={list(self._strategies.keys())})"
