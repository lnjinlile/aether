"""RSI Mean Reversion Strategy."""

from typing import List, Optional

import numpy as np
import pandas as pd

from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi


class RSIMeanReversionStrategy(BaseStrategy):
    """RSI-based mean reversion strategy.

    Parameters:
        rsi_period: RSI calculation period (default 14)
        oversold: RSI threshold for oversold / long entry (default 30)
        overbought: RSI threshold for overbought / short entry (default 70)
        exit_rsi: RSI level to trigger exit (default 50)
        stop_loss_pct: Fixed stop loss as decimal (default 0.03 = 3%)
        take_profit_pct: Fixed take profit as decimal (default 0.06 = 6%)

    Rules:
        - RSI drops below oversold -> LONG (expect mean reversion up)
        - RSI rises above overbought -> SHORT (expect mean reversion down)
        - RSI crosses exit_rsi (50) from oversold direction -> CLOSE_LONG
        - RSI crosses exit_rsi (50) from overbought direction -> CLOSE_SHORT
    """

    def __init__(
        self,
        name: str = "RSI_MR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        exit_rsi: float = 50.0,
        stop_loss_pct: float = 0.03,
        take_profit_pct: float = 0.06,
        cooldown_bars: int = 5,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["15m"],
        )
        self.params = {
            "rsi_period": rsi_period,
            "oversold": oversold,
            "overbought": overbought,
            "exit_rsi": exit_rsi,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "cooldown_bars": cooldown_bars,
        }
        self._bars_since_last_trade = cooldown_bars + 1

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute RSI when data is fed."""
        key = (symbol, timeframe)
        period = self.params["rsi_period"]
        exit_level = self.params["exit_rsi"]

        ind = pd.DataFrame(index=df.index)
        ind["rsi"] = compute_rsi(df["close"], period)

        # Cross signals
        oversold = self.params["oversold"]
        overbought = self.params["overbought"]
        # Cross signals (original)
        ind["cross_below_oversold"] = (ind["rsi"] < oversold) & (ind["rsi"].shift(1) >= oversold)
        ind["cross_above_overbought"] = (ind["rsi"] > overbought) & (ind["rsi"].shift(1) <= overbought)
        ind["cross_above_exit"] = (ind["rsi"] > exit_level) & (ind["rsi"].shift(1) <= exit_level)
        ind["cross_below_exit"] = (ind["rsi"] < exit_level) & (ind["rsi"].shift(1) >= exit_level)
        
        # Sustained signals: trigger after N consecutive bars in extreme zone
        sustained_bars = 3
        ind["below_oversold"] = (ind["rsi"] < oversold).astype(int)
        ind["sustained_oversold"] = ind["below_oversold"].rolling(sustained_bars).sum() >= sustained_bars
        ind["sustained_oversold_trigger"] = ind["sustained_oversold"] & ~ind["sustained_oversold"].shift(1).fillna(False)
        
        ind["above_overbought"] = (ind["rsi"] > overbought).astype(int)
        ind["sustained_overbought"] = ind["above_overbought"].rolling(sustained_bars).sum() >= sustained_bars
        ind["sustained_overbought_trigger"] = ind["sustained_overbought"] & ~ind["sustained_overbought"].shift(1).fillna(False)

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        """Evaluate current market state and generate a signal."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)

        min_bars = self.params["rsi_period"] + 1
        if (early := self._check_ready(symbol, min_bars)):
            return early

        df = self._data.get(key)
        # Increment cooldown counter
        self._bars_since_last_trade += 1

        latest = ind.iloc[-1]
        current_price = float(df["close"].iloc[-1])
        has_pos = self.has_position(symbol)
        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]
        cooldown = self.params["cooldown_bars"]

        # Close signals
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG" and latest["cross_above_exit"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_LONG,
                    symbol=symbol,
                    price=current_price,
                    reason=f"RSI crossed above {self.params['exit_rsi']} - closing long",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )
            elif pos["side"] == "SHORT" and latest["cross_below_exit"]:
                self._bars_since_last_trade = 0
                return Signal(
                    type=SignalType.CLOSE_SHORT,
                    symbol=symbol,
                    price=current_price,
                    reason=f"RSI crossed below {self.params['exit_rsi']} - closing short",
                    strategy_name=self.name,
                    timestamp=df.index[-1],
                )

        # Entry signals — simple: if oversold, LONG. If overbought, SHORT.
        # Cooldown prevents overtrading. Has_position prevents doubling.
        if not has_pos and self._bars_since_last_trade > cooldown:
            if not np.isnan(latest["rsi"]):
                if latest["rsi"] < self.params["oversold"]:
                    self._bars_since_last_trade = 0
                    sl = current_price * (1.0 - sl_pct)
                    tp = current_price * (1.0 + tp_pct)
                    return Signal(
                        type=SignalType.LONG,
                        symbol=symbol, price=current_price, quantity=0.001,
                        stop_loss=sl, take_profit=tp,
                        reason=f"RSI={latest['rsi']:.1f} < {self.params['oversold']} (oversold)",
                        confidence=0.65, leverage=self.params.get("leverage", 3),
                        strategy_name=self.name, timestamp=df.index[-1],
                    )
                elif latest["rsi"] > self.params["overbought"]:
                    self._bars_since_last_trade = 0
                    sl = current_price * (1.0 + sl_pct)
                    tp = current_price * (1.0 - tp_pct)
                    return Signal(
                        type=SignalType.SHORT,
                        symbol=symbol, price=current_price, quantity=0.001,
                        stop_loss=sl, take_profit=tp,
                        reason=f"RSI={latest['rsi']:.1f} > {self.params['overbought']} (overbought)",
                        confidence=0.65, leverage=self.params.get("leverage", 3),
                        strategy_name=self.name, timestamp=df.index[-1],
                    )

        return Signal(
            type=SignalType.HOLD,
            symbol=symbol,
            reason=f"RSI={latest['rsi']:.1f}, no trigger",
            strategy_name=self.name,
            timestamp=df.index[-1] if df is not None else None,
        )

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": self.params["rsi_period"] * 3,
        }
