"""Stochastic RSI Mean Reversion Strategy.

StochRSI is a refinement of RSI: it applies the Stochastic oscillator formula
to RSI values, producing a double-smoothed oscillator in [0,1] range.
This filters noise better than plain RSI while preserving mean-reversion signals.

Crossover/under of 0.2 (oversold) and 0.8 (overbought) thresholds.
Exit when StochRSI %K crosses the 0.5 midline.

Ref: Chande & Kroll, "The New Technical Trader" (1994)
"""

from typing import List, Optional

import pandas as pd
import numpy as np

from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi


class StochRSIMeanReversionStrategy(BaseStrategy):
    """Stochastic RSI-based mean reversion.

    Parameters:
        rsi_period: RSI calculation period (default 14)
        stoch_period: StochRSI %K smoothing period (default 14)
        smooth_k: %K SMA smoothing period (default 3)
        smooth_d: %D SMA smoothing period (default 3)
        oversold: StochRSI threshold for oversold / long entry (default 0.20)
        overbought: StochRSI threshold for overbought / short entry (default 0.80)
        stop_loss_pct: Fixed stop loss as decimal (default 0.02 = 2%)
        take_profit_pct: Fixed take profit as decimal (default 0.04 = 4%)
        cooldown_bars: Minimum bars between trades (default 5)

    Rules:
        - StochRSI %K drops below oversold (0.2) -> LONG
        - StochRSI %K rises above overbought (0.8) -> SHORT
        - StochRSI %K crosses 0.5 midline from below -> CLOSE_LONG
        - StochRSI %K crosses 0.5 midline from above -> CLOSE_SHORT
    """

    def __init__(
        self,
        name: str = "StochRSI_MR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        rsi_period: int = 14,
        stoch_period: int = 14,
        smooth_k: int = 3,
        smooth_d: int = 3,
        oversold: float = 0.20,
        overbought: float = 0.80,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        cooldown_bars: int = 5,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["ETH/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params = {
            "rsi_period": rsi_period,
            "stoch_period": stoch_period,
            "smooth_k": smooth_k,
            "smooth_d": smooth_d,
            "oversold": oversold,
            "overbought": overbought,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "cooldown_bars": cooldown_bars,
            "leverage": 3,
        }
        self._bars_since_last_trade = cooldown_bars + 1

    @staticmethod
    def _compute_stoch_rsi(
        rsi: pd.Series, stoch_period: int, smooth_k: int, smooth_d: int
    ) -> tuple:
        """Compute Stochastic RSI %K and %D.

        StochRSI = (RSI - min(RSI, n)) / (max(RSI, n) - min(RSI, n))
        %K = SMA(StochRSI, smooth_k)
        %D = SMA(%K, smooth_d)
        """
        rsi_min = rsi.rolling(stoch_period).min()
        rsi_max = rsi.rolling(stoch_period).max()
        stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
        stoch_rsi = stoch_rsi.clip(0.0, 1.0)

        k_line = stoch_rsi.rolling(smooth_k).mean()
        d_line = k_line.rolling(smooth_d).mean()

        return k_line, d_line

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute Stochastic RSI indicators."""
        key = (symbol, timeframe)
        rsi_period = self.params["rsi_period"]
        stoch_period = self.params["stoch_period"]
        smooth_k = self.params["smooth_k"]
        smooth_d = self.params["smooth_d"]

        ind = pd.DataFrame(index=df.index)

        # Compute RSI first
        ind["rsi"] = compute_rsi(df["close"], rsi_period)

        # Compute Stochastic RSI
        ind["stoch_k"], ind["stoch_d"] = self._compute_stoch_rsi(
            ind["rsi"], stoch_period, smooth_k, smooth_d
        )

        oversold = self.params["oversold"]
        overbought = self.params["overbought"]

        # Entry cross signals on %K
        ind["cross_below_oversold"] = (
            (ind["stoch_k"] < oversold) & (ind["stoch_k"].shift(1) >= oversold)
        )
        ind["cross_above_overbought"] = (
            (ind["stoch_k"] > overbought) & (ind["stoch_k"].shift(1) <= overbought)
        )

        # Exit cross signals on midline (0.5)
        midline = 0.5
        ind["cross_above_mid"] = (
            (ind["stoch_k"] > midline) & (ind["stoch_k"].shift(1) <= midline)
        )
        ind["cross_below_mid"] = (
            (ind["stoch_k"] < midline) & (ind["stoch_k"].shift(1) >= midline)
        )

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        """Evaluate current market state and generate a signal."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)

        min_bars = (
            self.params["rsi_period"]
            + self.params["stoch_period"]
            + self.params["smooth_k"]
            + self.params["smooth_d"]
            + 5
        )
        if ind is None or len(ind) < min_bars:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason="Insufficient data for StochRSI calculation",
                strategy_name=self.name,
            )

        df = self._data.get(key)
        if df is None:
            return Signal(
                type=SignalType.HOLD,
                symbol=symbol,
                reason="No data",
                strategy_name=self.name,
            )

        self._bars_since_last_trade += 1

        latest = ind.iloc[-1]
        current_price = float(df["close"].iloc[-1])
        has_pos = self.has_position(symbol)
        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]
        cooldown = self.params["cooldown_bars"]

        # Close signals (in position)
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG" and latest["cross_above_mid"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_LONG,
                    symbol=symbol,
                    price=current_price,
                    reason=f"StochRSI %K crossed above 0.5 - closing long",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif pos["side"] == "SHORT" and latest["cross_below_mid"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_SHORT,
                    symbol=symbol,
                    price=current_price,
                    reason=f"StochRSI %K crossed below 0.5 - closing short",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        # Entry signals (no position, cooldown passed)
        if not has_pos and self._bars_since_last_trade > cooldown:
            stoch_k = latest["stoch_k"]
            if pd.notna(stoch_k) and latest["cross_below_oversold"]:
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
                    reason=f"StochRSI %K={stoch_k:.3f} crossed below {self.params['oversold']} (oversold)",
                    confidence=0.65,
                    leverage=self.params.get("leverage", 3),
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif pd.notna(stoch_k) and latest["cross_above_overbought"]:
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
                    reason=f"StochRSI %K={stoch_k:.3f} crossed above {self.params['overbought']} (overbought)",
                    confidence=0.65,
                    leverage=self.params.get("leverage", 3),
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        stoch_val = latest.get("stoch_k", None)
        stoch_str = f"{stoch_val:.3f}" if pd.notna(stoch_val) else "N/A"
        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            reason=f"StochRSI %K={stoch_str}, no trigger",
            strategy_name=self.name,
            timestamp=df.index[-1] if df is not None else None,
        )

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": (
                self.params["rsi_period"]
                + self.params["stoch_period"]
                + self.params["smooth_k"]
                + self.params["smooth_d"]
                + 100
            ),
        }
