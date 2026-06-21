"""Dual Moving Average Crossover Strategy with ATR-based stops."""

from typing import List, Optional

import pandas as pd
import numpy as np

from ..base import BaseStrategy, Signal, SignalType


class MACrossoverStrategy(BaseStrategy):
    """Golden Cross / Death Cross strategy with ATR-based dynamic exits.

    Parameters:
        fast_period: Fast EMA period (default 7)
        slow_period: Slow EMA period (default 25)
        atr_period: ATR lookback period (default 14)
        atr_sl_mult: ATR multiplier for stop loss (default 2)
        atr_tp_mult: ATR multiplier for take profit (default 3)

    Rules:
        - Golden cross (fast > slow after fast <= slow) -> LONG
        - Death cross (fast < slow after fast >= slow) -> SHORT
        - Reverse cross -> close existing position
    """

    def __init__(
        self,
        name: str = "MA_Cross",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        fast_period: int = 7,
        slow_period: int = 25,
        atr_period: int = 14,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 3.0,
        cooldown_bars: int = 5,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["15m"],
        )
        self.params = {
            "fast_period": fast_period,
            "slow_period": slow_period,
            "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult,
            "atr_tp_mult": atr_tp_mult,
            "cooldown_bars": cooldown_bars,
        }
        self._bars_since_last_trade = cooldown_bars + 1  # ready immediately

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute EMAs and ATR when data is fed."""
        key = (symbol, timeframe)
        fp = self.params["fast_period"]
        sp = self.params["slow_period"]
        ap = self.params["atr_period"]

        ind = pd.DataFrame(index=df.index)
        ind["fast_ema"] = df["close"].ewm(span=fp, adjust=False).mean()
        ind["slow_ema"] = df["close"].ewm(span=sp, adjust=False).mean()

        # ATR
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift(1)).abs()
        low_close = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=ap, adjust=False).mean()

        # Cross detection
        ind["cross_above"] = (ind["fast_ema"] > ind["slow_ema"]) & (
            ind["fast_ema"].shift(1) <= ind["slow_ema"].shift(1)
        )
        ind["cross_below"] = (ind["fast_ema"] < ind["slow_ema"]) & (
            ind["fast_ema"].shift(1) >= ind["slow_ema"].shift(1)
        )

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        """Evaluate current market state and generate a signal."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)

        if ind is None or len(ind) < 2:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason="Insufficient data",
                strategy_name=self.name,
            )

        df = self._data.get(key)
        if df is None:
            return Signal(type=SignalType.HOLD, symbol=symbol, reason="No data", strategy_name=self.name)

        # Increment cooldown counter
        self._bars_since_last_trade += 1

        latest = ind.iloc[-1]
        current_price = float(df["close"].iloc[-1])
        atr_val = float(latest.get("atr", 0))
        has_pos = self.has_position(symbol)

        # Close signals from reverse cross
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG" and latest["cross_below"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_LONG,
                    symbol=symbol,
                    price=current_price,
                    reason=f"Death cross - closing long",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif pos["side"] == "SHORT" and latest["cross_above"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_SHORT,
                    symbol=symbol,
                    price=current_price,
                    reason=f"Golden cross - closing short",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        # Entry signals (only if cooldown passed)
        sl_mult = self.params["atr_sl_mult"]
        tp_mult = self.params["atr_tp_mult"]
        cooldown = self.params["cooldown_bars"]

        if not has_pos and self._bars_since_last_trade > cooldown:
            if latest["cross_above"]:
                self._bars_since_last_trade = 0
                sl = current_price - (atr_val * sl_mult) if atr_val > 0 else float("nan")
                tp = current_price + (atr_val * tp_mult) if atr_val > 0 else float("nan")
                return Signal(
                    type=SignalType.LONG,
                    symbol=symbol,
                    price=current_price,
                    quantity=0.001,
                    stop_loss=sl,
                    take_profit=tp,
                    reason=f"Golden cross (fast={self.params['fast_period']}, slow={self.params['slow_period']})",
                    confidence=0.7,
                    leverage=5,
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif latest["cross_below"]:
                self._bars_since_last_trade = 0
                sl = current_price + (atr_val * sl_mult) if atr_val > 0 else float("nan")
                tp = current_price - (atr_val * tp_mult) if atr_val > 0 else float("nan")
                return Signal(
                    type=SignalType.SHORT,
                    symbol=symbol,
                    price=current_price,
                    quantity=0.001,
                    stop_loss=sl,
                    take_profit=tp,
                    reason=f"Death cross (fast={self.params['fast_period']}, slow={self.params['slow_period']})",
                    confidence=0.7,
                    leverage=5,
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            reason="No signal",
            strategy_name=self.name,
            timestamp=df.index[-1] if df is not None else None,
        )

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["slow_period"], self.params["atr_period"]) * 2,
        }
