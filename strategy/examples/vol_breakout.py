"""
Volatility Breakout Strategy — ATR通道突破

逻辑:
- 计算 ATR 波动率和 EMA 参考线
- 价格突破 EMA + N*ATR (上轨) → 做多，突破 EMA - N*ATR (下轨) → 做空
- 价格回归 EMA → 平仓
- ATR trailing stop 保护

优势: 纯波动率驱动，无震荡指标干扰，适合趋势行情
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class VolBreakoutStrategy(BaseStrategy):
    """ATR通道突破策略。当价格突破波动率通道时入场。"""

    def __init__(
        self,
        name: str = "VolBreakout",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        atr_period: int = 20,
        atr_mult: float = 2.0,       # 通道宽度倍数
        ema_period: int = 50,         # EMA参考线
        atr_sl_mult: float = 1.5,     # 止损ATR倍数
        atr_tp_mult: float = 3.0,     # 止盈ATR倍数(risk:reward = 1:2)
        cooldown_bars: int = 5,
        volume_filter: bool = True,   # 突破需放量确认
        vol_ma_period: int = 20,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["1h"])
        self.params.update({
            "atr_period": atr_period,
            "atr_mult": atr_mult,
            "ema_period": ema_period,
            "atr_sl_mult": atr_sl_mult,
            "atr_tp_mult": atr_tp_mult,
            "cooldown_bars": cooldown_bars,
            "volume_filter": volume_filter,
            "vol_ma_period": vol_ma_period,
        })
        self._bars_since_last_trade = cooldown_bars + 1
        self._trailing_stop = {}   # symbol -> trailing stop price

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        h, l, c = df["high"], df["low"], df["close"]

        # ATR
        tr1 = h - l
        tr2 = (h - c.shift()).abs()
        tr3 = (l - c.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=p["atr_period"], adjust=False).mean()

        # EMA参考线
        ind["ema"] = c.ewm(span=p["ema_period"], adjust=False).mean()

        # 通道上下轨
        ind["upper"] = ind["ema"] + ind["atr"] * p["atr_mult"]
        ind["lower"] = ind["ema"] - ind["atr"] * p["atr_mult"]

        # 突破信号 (当前bar收盘价 vs 上根bar通道)
        prev_close = c.shift(1)
        prev_upper = ind["upper"].shift(1)
        prev_lower = ind["lower"].shift(1)
        ind["break_up"] = (c > prev_upper) & (prev_close <= prev_upper)
        ind["break_down"] = (c < prev_lower) & (prev_close >= prev_lower)

        # 回归信号
        ind["cross_below_ema"] = (c < ind["ema"]) & (c.shift(1) >= ind["ema"].shift(1))
        ind["cross_above_ema"] = (c > ind["ema"]) & (c.shift(1) <= ind["ema"].shift(1))

        # 成交量均值 (可选放量过滤)
        if p["volume_filter"] and "volume" in df.columns:
            ind["vol_ma"] = df["volume"].rolling(p["vol_ma_period"]).mean()
            ind["vol_ratio"] = df["volume"] / ind["vol_ma"]
        else:
            ind["vol_ratio"] = 1.0  # 无过滤时总是通过

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = max(self.params["ema_period"], self.params["atr_period"]) + 5
        if ind is None or df is None or len(ind) < min_bars:
            return Signal(SignalType.HOLD, symbol,
                          reason=f"Need {min_bars} bars, have {len(ind) if ind is not None else 0}",
                          strategy_name=self.name)

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        atr = float(latest.get("atr", 0))
        ema = float(latest.get("ema", 0))
        has_pos = self.has_position(symbol)
        sl_mult = self.params["atr_sl_mult"]
        tp_mult = self.params["atr_tp_mult"]

        # ---- 持仓管理 ----
        if has_pos:
            pos = self._positions[symbol]

            # EMA回归 → 平仓
            if pos["side"] == "LONG" and latest.get("cross_below_ema", False):
                self._trailing_stop.pop(symbol, None)
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason=f"Price crossed below EMA({ema:.1f}), closing long",
                              strategy_name=self.name)
            if pos["side"] == "SHORT" and latest.get("cross_above_ema", False):
                self._trailing_stop.pop(symbol, None)
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason=f"Price crossed above EMA({ema:.1f}), closing short",
                              strategy_name=self.name)

            # ATR trailing stop
            trail = self._trailing_stop.get(symbol)
            if trail:
                if pos["side"] == "LONG":
                    new_trail = max(trail, price - atr * sl_mult)
                    self._trailing_stop[symbol] = new_trail
                    if price <= trail:
                        self._trailing_stop.pop(symbol, None)
                        return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                      reason=f"Trailing stop hit at {trail:.1f}",
                                      strategy_name=self.name)
                else:  # SHORT
                    new_trail = min(trail, price + atr * sl_mult)
                    self._trailing_stop[symbol] = new_trail
                    if price >= trail:
                        self._trailing_stop.pop(symbol, None)
                        return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                      reason=f"Trailing stop hit at {trail:.1f}",
                                      strategy_name=self.name)

            return Signal(SignalType.HOLD, symbol, reason="Holding position",
                          strategy_name=self.name)

        # ---- 开仓条件 ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        # 放量过滤
        vol_ratio = float(latest.get("vol_ratio", 1.0))
        if self.params["volume_filter"] and vol_ratio < 1.0:
            return Signal(SignalType.HOLD, symbol,
                          reason=f"Low volume ratio {vol_ratio:.2f}", strategy_name=self.name)

        # 向上突破 → 做多
        if latest.get("break_up", False):
            self._bars_since_last_trade = 0
            sl = price - atr * sl_mult if atr > 0 else float("nan")
            tp = price + atr * tp_mult if atr > 0 else float("nan")
            self._trailing_stop[symbol] = sl
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason=f"Break up: price {price:.1f} > upper channel",
                          confidence=0.6, strategy_name=self.name)

        # 向下突破 → 做空
        if latest.get("break_down", False):
            self._bars_since_last_trade = 0
            sl = price + atr * sl_mult if atr > 0 else float("nan")
            tp = price - atr * tp_mult if atr > 0 else float("nan")
            self._trailing_stop[symbol] = sl
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason=f"Break down: price {price:.1f} < lower channel",
                          confidence=0.6, strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol, reason="No breakout", strategy_name=self.name)
