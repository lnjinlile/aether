"""
趋势跟踪策略 — 简单、稳健、盈利导向

逻辑:
- EMA100斜率决定趋势方向
- 顺势入场,逆势反转平仓
- 固定百分比止损止盈 + 最大持仓时间
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class TrendFollow(BaseStrategy):
    """趋势跟踪 — 顺势而为,简单致胜"""

    def __init__(
        self,
        name: str = "TrendFollow",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        ema_period: int = 100,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        cooldown_bars: int = 5,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["1h"])
        self.params.update({
            "ema_period": ema_period,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "cooldown_bars": cooldown_bars,
        })
        self._bars_since_last_trade = cooldown_bars + 1
        self._entry_bar = {}

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)
        ind["ema"] = df["close"].ewm(span=p["ema_period"], adjust=False).mean()
        ind["ema_slope"] = ind["ema"].diff(5)
        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)
        if ind is None or df is None or len(ind) < 120:
            return Signal(SignalType.HOLD, symbol, reason="Warming up", strategy_name=self.name)

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        slope = float(latest["ema_slope"])
        uptrend = slope > 0
        has_pos = self.has_position(symbol)

        # ---- 平仓 ----
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]
            sl_pct = self.params["stop_loss_pct"]
            tp_pct = self.params["take_profit_pct"]

            # 趋势反转
            if pos["side"] == "LONG" and not uptrend:
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="Trend reversed", strategy_name=self.name)
            if pos["side"] == "SHORT" and uptrend:
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="Trend reversed", strategy_name=self.name)

            # 止损止盈
            if pos["side"] == "LONG":
                if price <= entry * (1 - sl_pct):
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason="SL -%.0f%%" % (sl_pct*100), strategy_name=self.name)
                if price >= entry * (1 + tp_pct):
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason="TP +%.0f%%" % (tp_pct*100), strategy_name=self.name)
            else:
                if price >= entry * (1 + sl_pct):
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason="SL -%.0f%%" % (sl_pct*100), strategy_name=self.name)
                if price <= entry * (1 - tp_pct):
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason="TP +%.0f%%" % (tp_pct*100), strategy_name=self.name)

            return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

        # ---- 开仓 ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]

        if uptrend:
            self._bars_since_last_trade = 0
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=price*(1-sl_pct), take_profit=price*(1+tp_pct),
                          leverage=3, confidence=0.65,
                          reason="Uptrend", strategy_name=self.name)
        else:
            self._bars_since_last_trade = 0
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=price*(1+sl_pct), take_profit=price*(1-tp_pct),
                          leverage=3, confidence=0.65,
                          reason="Downtrend", strategy_name=self.name)
