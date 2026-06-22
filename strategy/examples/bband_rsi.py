""""
Bollinger Band + RSI 均值回归策略 — 专为盘整市设计 (v2: TP/SL-only exits)

逻辑:
- 价格触及布林带下轨 + RSI < 35 → 做多 (超卖反弹)
- 价格触及布林带上轨 + RSI > 65 → 做空 (超买回落)  
- 仅由 TP/SL 平仓，不再中途 RSI/SMA 提前退出
- 固定止损2%, 止盈5% (1:2.5 R:R)
"""
from typing import List, Optional
import pandas as pd
from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi, compute_bollinger_bands


class BBandMeanReversion(BaseStrategy):
    """布林带均值回归 — 盘整市盈利机器"""

    def __init__(
        self,
        name: str = "BB_MR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        bb_period: int = 20,
        bb_std: float = 2.5,
        rsi_period: int = 14,
        rsi_oversold: float = 30,
        rsi_overbought: float = 70,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.05,
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

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        # Bollinger Bands
        close = df["close"]
        bb = compute_bollinger_bands(close, p["bb_period"], p["bb_std"])
        ind["sma"] = bb["middle"]
        ind["upper"] = bb["upper"]
        ind["lower"] = bb["lower"]
        ind["bb_width"] = bb["bandwidth"]

        # RSI
        ind["rsi"] = compute_rsi(close, p["rsi_period"])

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

            # RSI/SMA 退出已移除 — 保留给 TP/SL 完成完整风险收益比
            # 62% WR with 1:2 R:R needs trades to actually reach TP, not exit prematurely

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
