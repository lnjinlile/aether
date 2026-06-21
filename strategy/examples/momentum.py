"""
动量 + 波动率自适应策略 — 始终有持仓,在趋势中盈利

逻辑:
- 始终持仓(多或空), 永不空仓
- MACD方向决定多空: MACD>0→做多, MACD<0→做空
- ATR动态止损止盈
- 信号反转时平仓反手
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class MomentumStrategy(BaseStrategy):
    """动量跟踪 — 始终在场,吃趋势利润"""

    def __init__(
        self,
        name: str = "Momentum",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        fast_ema: int = 12,
        slow_ema: int = 26,
        signal_period: int = 9,
        atr_period: int = 14,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 3.5,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["15m"])
        self.params.update({
            "fast_ema": fast_ema, "slow_ema": slow_ema,
            "signal_period": signal_period, "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult, "atr_tp_mult": atr_tp_mult,
        })
        self._first_signal = True  # 第一次必然发信号

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        close = df["close"]
        h, l = df["high"], df["low"]

        # MACD
        ema_fast = close.ewm(span=p["fast_ema"], adjust=False).mean()
        ema_slow = close.ewm(span=p["slow_ema"], adjust=False).mean()
        ind["macd"] = ema_fast - ema_slow
        ind["macd_signal"] = ind["macd"].ewm(span=p["signal_period"], adjust=False).mean()
        ind["macd_hist"] = ind["macd"] - ind["macd_signal"]
        ind["macd_direction"] = ind["macd_hist"] > 0  # True=做多, False=做空
        ind["macd_flip"] = ind["macd_direction"] != ind["macd_direction"].shift(1)

        # ATR
        tr1 = h - l
        tr2 = (h - close.shift()).abs()
        tr3 = (l - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=p["atr_period"], adjust=False).mean()

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        if ind is None or df is None or len(ind) < 60:
            return Signal(SignalType.HOLD, symbol, reason="Warming up", strategy_name=self.name)

        latest = ind.iloc[-1]
        prev = ind.iloc[-2]
        price = float(df["close"].iloc[-1])
        macd_dir = bool(latest["macd_direction"])
        prev_dir = bool(prev["macd_direction"])
        atr = float(latest["atr"])
        flipped = bool(latest["macd_flip"])
        has_pos = self.has_position(symbol)

        # 第一信号:无条件入场
        if self._first_signal and not has_pos:
            self._first_signal = False
            return self._entry_signal(symbol, price, macd_dir, atr)

        # ---- 平仓(信号反转) ----
        if has_pos and flipped:
            pos = self._positions[symbol]
            if pos["side"] == "LONG":
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="MACD flipped bearish", strategy_name=self.name)
            else:
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="MACD flipped bullish", strategy_name=self.name)

        # ---- 开仓 ----
        if not has_pos and flipped:
            return self._entry_signal(symbol, price, macd_dir, atr)

        # ---- 持有中的止损 ----
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]
            sl_mult = self.params["atr_sl_mult"]
            tp_mult = self.params["atr_tp_mult"]

            if pos["side"] == "LONG":
                if price <= entry - atr * sl_mult:
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason="ATR stop loss", strategy_name=self.name)
                if price >= entry + atr * tp_mult:
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason="ATR take profit", strategy_name=self.name)
            else:
                if price >= entry + atr * sl_mult:
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason="ATR stop loss", strategy_name=self.name)
                if price <= entry - atr * tp_mult:
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason="ATR take profit", strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

    def _entry_signal(self, symbol, price, go_long, atr):
        sl_mult = self.params["atr_sl_mult"]
        tp_mult = self.params["atr_tp_mult"]
        if go_long:
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=price-atr*sl_mult, take_profit=price+atr*tp_mult,
                          leverage=3, confidence=0.65,
                          reason="MACD bullish", strategy_name=self.name)
        else:
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=price+atr*sl_mult, take_profit=price-atr*tp_mult,
                          leverage=3, confidence=0.65,
                          reason="MACD bearish", strategy_name=self.name)
