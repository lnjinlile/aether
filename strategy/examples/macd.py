"""MACD Crossover Strategy.

Classic trend-following strategy based on MACD (Moving Average Convergence Divergence)
crossovers. Long on bullish crossover (MACD crosses above signal), short on bearish
crossover (MACD crosses below signal).

Parameters:
    fast_period: Fast EMA period (default 12)
    slow_period: Slow EMA period (default 26)
    signal_period: Signal line EMA period (default 9)
    stop_loss_pct: Fixed stop loss as decimal (default 0.02 = 2%)
    take_profit_pct: Fixed take profit as decimal (default 0.04 = 4%)
    cooldown_bars: Bars to wait after trade before next entry (default 5)
    leverage: Leverage multiplier (default 3)

Rules:
    - MACD crosses above signal line -> LONG (bullish momentum)
    - MACD crosses below signal line -> SHORT (bearish momentum)
    - MACD crosses back toward zero -> CLOSE (trend weakening)
    - Histogram confirms direction strength
"""

from typing import List, Optional

import pandas as pd
import numpy as np

from ..base import BaseStrategy, Signal, SignalType


class MACDCrossoverStrategy(BaseStrategy):
    """MACD crossover strategy with histogram confirmation."""

    def __init__(
        self,
        name: str = "MACD",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        cooldown_bars: int = 5,
        leverage: int = 3,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params = {
            "fast_period": fast_period,
            "slow_period": slow_period,
            "signal_period": signal_period,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "cooldown_bars": cooldown_bars,
            "leverage": leverage,
        }
        self._bars_since_last_trade = cooldown_bars + 1

    @staticmethod
    def _compute_macd(
        close: pd.Series, fast: int, slow: int, signal: int
    ) -> pd.DataFrame:
        """Compute MACD line, signal line, and histogram."""
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return pd.DataFrame(
            {
                "macd": macd_line,
                "signal": signal_line,
                "histogram": histogram,
            },
            index=close.index,
        )

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute MACD indicators when data is fed."""
        key = (symbol, timeframe)
        fp = self.params["fast_period"]
        sp = self.params["slow_period"]
        sigp = self.params["signal_period"]

        ind = self._compute_macd(df["close"], fp, sp, sigp)

        # Cross signals
        ind["cross_above_signal"] = (
            (ind["macd"] > ind["signal"])
            & (ind["macd"].shift(1) <= ind["signal"].shift(1))
        )
        ind["cross_below_signal"] = (
            (ind["macd"] < ind["signal"])
            & (ind["macd"].shift(1) >= ind["signal"].shift(1))
        )
        # MACD crossing zero line (trend direction change)
        ind["cross_above_zero"] = (
            (ind["macd"] > 0) & (ind["macd"].shift(1) <= 0)
        )
        ind["cross_below_zero"] = (
            (ind["macd"] < 0) & (ind["macd"].shift(1) >= 0)
        )

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        """Evaluate MACD state and generate trading signal."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)

        min_bars = max(
            self.params["slow_period"],
            self.params["signal_period"],
        ) + 2

        if (early := self._check_ready(symbol, min_bars)):
            return early

        df = self._data.get(key)

        self._bars_since_last_trade += 1

        latest = ind.iloc[-1]
        current_price = float(df["close"].iloc[-1])
        has_pos = self.has_position(symbol)
        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]
        cd = self.params["cooldown_bars"]

        # Close signals: MACD direction reverses
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG" and latest["cross_below_signal"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_LONG,
                    symbol=symbol,
                    price=current_price,
                    reason=f"MACD crossed below signal - closing long",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif pos["side"] == "SHORT" and latest["cross_above_signal"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_SHORT,
                    symbol=symbol,
                    price=current_price,
                    reason=f"MACD crossed above signal - closing short",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        # Entry signals (cooldown enforced)
        if not has_pos and self._bars_since_last_trade > cd:
            if latest["cross_above_signal"]:
                self._bars_since_last_trade = 0
                sl = current_price * (1.0 - sl_pct)
                tp = current_price * (1.0 + tp_pct)
                return Signal(
                    type=SignalType.LONG,
                    symbol=symbol,
                    price=current_price,
                    quantity=0.001,
                    stop_loss=sl,
                    take_profit=tp,
                    reason=(
                        f"MACD({self.params['fast_period']}/{self.params['slow_period']}/"
                        f"{self.params['signal_period']}) bullish crossover | "
                        f"hist={latest['histogram']:.4f}"
                    ),
                    confidence=0.55,
                    leverage=self.params.get("leverage", 3),
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif latest["cross_below_signal"]:
                self._bars_since_last_trade = 0
                sl = current_price * (1.0 + sl_pct)
                tp = current_price * (1.0 - tp_pct)
                return Signal(
                    type=SignalType.SHORT,
                    symbol=symbol,
                    price=current_price,
                    quantity=0.001,
                    stop_loss=sl,
                    take_profit=tp,
                    reason=(
                        f"MACD({self.params['fast_period']}/{self.params['slow_period']}/"
                        f"{self.params['signal_period']}) bearish crossover | "
                        f"hist={latest['histogram']:.4f}"
                    ),
                    confidence=0.55,
                    leverage=self.params.get("leverage", 3),
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            reason=(
                f"MACD={latest['macd']:.4f} signal={latest['signal']:.4f} "
                f"hist={latest['histogram']:.4f}, no crossover"
            ),
            strategy_name=self.name,
            timestamp=df.index[-1],
        )

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(
                self.params["slow_period"],
                self.params["signal_period"],
            ) * 3,
        }
