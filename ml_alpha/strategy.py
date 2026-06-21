"""ML Alpha Strategy — wraps the trained LightGBM model into the Aether
strategy framework so Athena and Prometheus can consume it directly.

Importable as: from ml_alpha.strategy import MLAlphaStrategy
"""

from typing import List, Optional

import numpy as np
import pandas as pd

from strategy.base import BaseStrategy, Signal, SignalType
from .features import FeatureEngineer
from .trainer import AlphaModel


class MLAlphaStrategy(BaseStrategy):
    """Machine-learning driven alpha strategy using LightGBM.

    Replaces old EMA-based strategies. Uses a trained AlphaModel to
    predict next-bar direction and emits LONG/SHORT signals with
    fixed 2% stop-loss and 4% take-profit.

    Constructor Parameters:
        model_path: Path to a saved AlphaModel (.pkl).
        symbols: List of trading symbols.
        timeframes: List of timeframes (uses first for signals).
        confidence_threshold: Probability threshold for signal (default 0.55).
    """

    def __init__(
        self,
        model_path: str = "ml_alpha/model.pkl",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        confidence_threshold: float = 0.55,
        name: str = "MLAlpha",
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.model_path = model_path
        self.confidence_threshold = confidence_threshold
        self.params = {
            "model_path": model_path,
            "confidence_threshold": confidence_threshold,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
        }

        # Load model
        self.model = AlphaModel()
        self.model.load(model_path)

        # Feature engineer
        self._engineer = FeatureEngineer()

        # Cache of pre-built features keyed by (symbol, timeframe)
        self._feature_cache: dict = {}

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Build and cache features when data is fed.

        Loads oracle features (orderbook, funding rates, open interest)
        and passes them to the feature engineer for richer ML features.
        """
        key = (symbol, timeframe)
        try:
            # Load oracle features
            oracle_df = self._load_oracle_features(symbol, df)
            X, _ = self._engineer.build_features(df, oracle_df=oracle_df)
            self._feature_cache[key] = X
        except Exception:
            # Not enough data yet or feature build failed; will be retried
            self._feature_cache[key] = pd.DataFrame()

    @staticmethod
    def _load_oracle_features(symbol: str, kline_df: pd.DataFrame):
        """Load and merge oracle features for the given symbol.

        Handles symbol format conversion (BTC/USDT → BTCUSDT) for
        oracle feature loading which uses Binance's raw symbol format.
        """
        try:
            from .oracle_features import merge_oracle_features
            oracle_symbol = symbol.replace("/", "")
            enriched = merge_oracle_features(kline_df, oracle_symbol)
            oracle_cols = [c for c in enriched.columns
                          if c not in kline_df.columns]
            if oracle_cols:
                return enriched[oracle_cols]
        except Exception:
            pass
        return None

    def generate_signal(self, symbol: str) -> Signal:
        """Generate a trading signal from the ML model.

        Returns LONG if model predicts UP with confidence > threshold,
        SHORT if DOWN with confidence > threshold, else HOLD.
        Includes 2% SL and 4% TP.
        """
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)

        X = self._feature_cache.get(key)
        df = self._data.get(key)

        if X is None or X.empty:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason="Feature cache not ready",
                strategy_name=self.name,
            )

        if df is None or df.empty:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason="No price data",
                strategy_name=self.name,
            )

        current_price = float(df["close"].iloc[-1])
        last_features = X.iloc[[-1]]

        try:
            prob = float(self.model.predict(last_features)[0])
        except Exception:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason="Model prediction failed",
                strategy_name=self.name,
            )

        # HOLD if already in position (simple single-position per symbol)
        if self.has_position(symbol):
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason=f"In position, prob={prob:.3f}",
                confidence=prob if prob >= 0.5 else 1 - prob,
                strategy_name=self.name,
                timestamp=df.index[-1],
            )

        if prob > self.confidence_threshold:
            sl = current_price * (1 - self.params["stop_loss_pct"])
            tp = current_price * (1 + self.params["take_profit_pct"])
            return Signal(
                type=SignalType.LONG,
                symbol=symbol,
                price=current_price,
                quantity=0.001,
                stop_loss=sl,
                take_profit=tp,
                reason=f"ML LONG signal (prob={prob:.4f})",
                confidence=prob,
                leverage=5,
                strategy_name=self.name,
                timestamp=df.index[-1],
            )
        elif prob < (1 - self.confidence_threshold):
            sl = current_price * (1 + self.params["stop_loss_pct"])
            tp = current_price * (1 - self.params["take_profit_pct"])
            return Signal(
                type=SignalType.SHORT,
                symbol=symbol,
                price=current_price,
                quantity=0.001,
                stop_loss=sl,
                take_profit=tp,
                reason=f"ML SHORT signal (prob={prob:.4f})",
                confidence=1 - prob,
                leverage=5,
                strategy_name=self.name,
                timestamp=df.index[-1],
            )

        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            reason=f"No signal (prob={prob:.4f})",
            confidence=prob if prob >= 0.5 else 1 - prob,
            strategy_name=self.name,
            timestamp=df.index[-1],
        )

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": 200,
        }
