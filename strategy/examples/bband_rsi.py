"""
Bollinger Band + RSI 均值回归策略 — 专为盘整市设计

逻辑:
- 价格触及布林带下轨 + RSI < 35 → 做多 (超卖反弹)
- 价格触及布林带上轨 + RSI > 65 → 做空 (超买回落)  
- 价格回归中轨 或 RSI回50 → 平仓
- 固定止损2%, 止盈4%
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class BBandMeanReversion(BaseStrategy):
    """布林带均值回归 — 盘整市盈利机器"""

    def __init__(
        self,
        name: str = "BB_MR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: float = 35,
        rsi_overbought: float = 65,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        cooldown_bars: int = 3,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["15m"])
        self.params.update({
            "bb_period": bb_period, "bb_std": bb_std,
            "rsi_period": rsi_period,
            "rsi_oversold": rsi_oversold, "rsi_overbought": rsi_overbought,
            "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
            "cooldown_bars": cooldown_bars,
        })
        self._bars_since_last_trade = cooldown_bars + 1

    def _compute_rsi(self, close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        # Bollinger Bands
        close = df["close"]
        ind["sma"] = close.rolling(p["bb_period"]).mean()
        std = close.rolling(p["bb_period"]).std()
        ind["upper"] = ind["sma"] + p["bb_std"] * std
        ind["lower"] = ind["sma"] - p["bb_std"] * std
        ind["bb_width"] = (ind["upper"] - ind["lower"]) / ind["sma"]  # relative width

        # RSI
        ind["rsi"] = self._compute_rsi(close, p["rsi_period"])

        # Touch signals
        ind["touch_lower"] = close <= ind["lower"]
        ind["touch_upper"] = close >= ind["upper"]
        ind["above_sma"] = close > ind["sma"]
        ind["below_sma"] = close < ind["sma"]

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        if ind is None or df is None or len(ind) < 50:
            return Signal(SignalType.HOLD, symbol, reason="Insufficient data", strategy_name=self.name)

        self._bars_since_last_trade += 1
        p = self.params
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        rsi = float(latest["rsi"])
        has_pos = self.has_position(symbol)

        # ---- 平仓 ----
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]

            # 止盈
            if pos["side"] == "LONG" and price >= entry * (1 + p["take_profit_pct"]):
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="Take profit +%.0f%%" % (p["take_profit_pct"]*100),
                              strategy_name=self.name)
            if pos["side"] == "SHORT" and price <= entry * (1 - p["take_profit_pct"]):
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="Take profit +%.0f%%" % (p["take_profit_pct"]*100),
                              strategy_name=self.name)

            # 止损
            if pos["side"] == "LONG" and price <= entry * (1 - p["stop_loss_pct"]):
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="Stop loss -%.0f%%" % (p["stop_loss_pct"]*100),
                              strategy_name=self.name)
            if pos["side"] == "SHORT" and price >= entry * (1 + p["stop_loss_pct"]):
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="Stop loss -%.0f%%" % (p["stop_loss_pct"]*100),
                              strategy_name=self.name)

            # RSI回归50 → 平仓
            if pos["side"] == "LONG" and rsi > 55:
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="RSI=%.0f returned to mean" % rsi, strategy_name=self.name)
            if pos["side"] == "SHORT" and rsi < 45:
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="RSI=%.0f returned to mean" % rsi, strategy_name=self.name)

            # 价格回归中轨
            if pos["side"] == "LONG" and float(latest["above_sma"]):
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="Price back above SMA", strategy_name=self.name)
            if pos["side"] == "SHORT" and float(latest["below_sma"]):
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="Price back below SMA", strategy_name=self.name)

            return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

        # ---- 开仓 ----
        if self._bars_since_last_trade <= p["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        # 做多: 触下轨 + RSI超卖
        if float(latest["touch_lower"]) and rsi < p["rsi_oversold"]:
            self._bars_since_last_trade = 0
            sl = price * (1 - p["stop_loss_pct"])
            tp = price * (1 + p["take_profit_pct"])
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason="BB lower+RSI=%.0f oversold" % rsi,
                          confidence=0.7, strategy_name=self.name)

        # 做空: 触上轨 + RSI超买
        if float(latest["touch_upper"]) and rsi > p["rsi_overbought"]:
            self._bars_since_last_trade = 0
            sl = price * (1 + p["stop_loss_pct"])
            tp = price * (1 - p["take_profit_pct"])
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason="BB upper+RSI=%.0f overbought" % rsi,
                          confidence=0.7, strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol, reason="No signal", strategy_name=self.name)
