"""
趋势回调策略 — 简单但有效

逻辑:
- EMA100斜率决定趋势方向(上升/下降)
- 只在趋势方向交易
- 价格回调至EMA附近时入场
- ATR动态止损, 固定盈亏比2:1
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class TrendPullback(BaseStrategy):
    """趋势回调入场 — 顺势而为"""

    def __init__(
        self,
        name: str = "TrendPB",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        ema_period: int = 100,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 3.0,
        cooldown_bars: int = 5,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["1h"])
        self.params.update({
            "ema_period": ema_period, "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult, "atr_tp_mult": atr_tp_mult,
            "cooldown_bars": cooldown_bars,
        })
        self._bars_since_last_trade = cooldown_bars + 1

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        close, h, l = df["close"], df["high"], df["low"]

        # EMA + 斜率
        ind["ema"] = close.ewm(span=p["ema_period"], adjust=False).mean()
        ind["ema_slope"] = ind["ema"].diff(5)  # 5-bar slope

        # ATR
        tr1 = h - l
        tr2 = (h - close.shift()).abs()
        tr3 = (l - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=p["atr_period"], adjust=False).mean()

        # 回调检测:价格接近EMA(在1 ATR范围内)
        dist_from_ema = (close - ind["ema"]).abs()
        ind["near_ema"] = dist_from_ema < ind["atr"]
        ind["above_ema"] = close > ind["ema"]
        ind["below_ema"] = close < ind["ema"]

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
        prev = ind.iloc[-2]
        price = float(df["close"].iloc[-1])
        ema = float(latest["ema"])
        atr = float(latest["atr"])
        slope = float(latest["ema_slope"])
        has_pos = self.has_position(symbol)

        uptrend = slope > 0
        near_ema = bool(latest["near_ema"])
        above_ema = bool(latest["above_ema"])

        # ---- 平仓 ----
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]

            # 趋势反转 → 立即平仓
            if pos["side"] == "LONG" and slope < 0:
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="Trend reversed to down", strategy_name=self.name)
            if pos["side"] == "SHORT" and slope > 0:
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="Trend reversed to up", strategy_name=self.name)

            # ATR止损/止盈 (ATR capped at 5% of price to avoid extremes)
            atr_capped = min(atr, price * 0.05)
            sl_dist = atr_capped * self.params["atr_sl_mult"]
            tp_dist = atr_capped * self.params["atr_tp_mult"]
            if pos["side"] == "LONG":
                if price <= entry - sl_dist:
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason="Stop loss", strategy_name=self.name)
                if price >= entry + tp_dist:
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason="Take profit", strategy_name=self.name)
            else:
                if price >= entry + sl_dist:
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason="Stop loss", strategy_name=self.name)
                if price <= entry - tp_dist:
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason="Take profit", strategy_name=self.name)

            return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

        # ---- 开仓: 趋势方向直接入场 (冷却后) ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        # 上升趋势 → 做多
        if uptrend:
            self._bars_since_last_trade = 0
            sl = price - atr * self.params["atr_sl_mult"]
            tp = price + atr * self.params["atr_tp_mult"]
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3, confidence=0.65,
                          reason="Uptrend (slope=%+.0f)" % slope,
                          strategy_name=self.name)

        # 下降趋势 → 做空
        if not uptrend:
            self._bars_since_last_trade = 0
            sl = price + atr * self.params["atr_sl_mult"]
            tp = price - atr * self.params["atr_tp_mult"]
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3, confidence=0.65,
                          reason="Downtrend (slope=%+.0f)" % slope,
                          strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol, reason="No setup", strategy_name=self.name)
