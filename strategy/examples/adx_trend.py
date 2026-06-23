"""
ADX + EMA 趋势跟踪策略 — 为盈利而生

逻辑:
- ADX > 25 → 市场有趋势,允许交易
- 价格在EMA下方 → 下跌趋势 → 做空
- 价格在EMA上方 → 上涨趋势 → 做多
- ADX < 20 → 趋势衰竭 → 平仓
- ATR动态止损 (2x ATR trailing stop)
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class ADXTrendStrategy(BaseStrategy):
    """ADX确认的趋势跟踪策略。有效捕获单边行情。"""

    def __init__(
        self,
        name: str = "ADX_Trend",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        adx_period: int = 14,
        adx_threshold: int = 25,
        adx_exit: int = 20,
        ema_period: int = 50,
        atr_period: int = 14,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
        cooldown_bars: int = 3,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["1h"])
        self.params.update({
            "adx_period": adx_period,
            "adx_threshold": adx_threshold,
            "adx_exit": adx_exit,
            "ema_period": ema_period,
            "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult,
            "atr_tp_mult": atr_tp_mult,
            "cooldown_bars": cooldown_bars,
        })
        self._bars_since_last_trade = cooldown_bars + 1
        self._trailing_stop = {}  # per-symbol trailing stop price

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        # ATR
        h, l, c = df["high"], df["low"], df["close"]
        tr1 = h - l
        tr2 = (h - c.shift()).abs()
        tr3 = (l - c.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=p["atr_period"], adjust=False).mean()

        # ADX
        up = h.diff()
        down = -l.diff()
        plus_dm = np.where((up > down) & (up > 0), up, 0)
        minus_dm = np.where((down > up) & (down > 0), down, 0)
        atr_s = pd.Series(tr, index=df.index).ewm(span=p["adx_period"], adjust=False).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(span=p["adx_period"], adjust=False).mean() / atr_s.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(span=p["adx_period"], adjust=False).mean() / atr_s.replace(0, np.nan)
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
        ind["adx"] = dx.ewm(span=p["adx_period"], adjust=False).mean()
        ind["plus_di"] = plus_di
        ind["minus_di"] = minus_di

        # EMA
        ind["ema"] = c.ewm(span=p["ema_period"], adjust=False).mean()

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = 50
        if (early := self._check_ready(symbol, min_bars)):
            return early

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        prev = ind.iloc[-2]
        price = float(df["close"].iloc[-1])
        atr = float(latest.get("atr", 0))
        adx = float(latest.get("adx", 0))
        ema = float(latest.get("ema", 0))
        has_pos = self.has_position(symbol)

        # ---- 平仓条件 ----
        if has_pos:
            pos = self._positions[symbol]

            # ADX衰竭 → 平仓
            if adx < self.params["adx_exit"]:
                self._trailing_stop.pop(symbol, None)
                return Signal(
                    SignalType.CLOSE_LONG if pos["side"] == "LONG" else SignalType.CLOSE_SHORT,
                    symbol, price=price,
                    reason=f"ADX={adx:.1f}<{self.params['adx_exit']} trend weakening",
                    strategy_name=self.name,
                )

            # 方向反转 → 平仓
            if pos["side"] == "LONG" and price < ema and adx > self.params["adx_threshold"]:
                self._trailing_stop.pop(symbol, None)
                return Signal(
                    SignalType.CLOSE_LONG, symbol, price=price,
                    reason=f"Price {price:.1f} < EMA {ema:.1f} reversal",
                    strategy_name=self.name,
                )
            if pos["side"] == "SHORT" and price > ema and adx > self.params["adx_threshold"]:
                self._trailing_stop.pop(symbol, None)
                return Signal(
                    SignalType.CLOSE_SHORT, symbol, price=price,
                    reason=f"Price {price:.1f} > EMA {ema:.1f} reversal",
                    strategy_name=self.name,
                )

            # Trailing stop
            trail = self._trailing_stop.get(symbol)
            if trail:
                if pos["side"] == "LONG":
                    new_trail = max(trail, price - atr * self.params["atr_sl_mult"])
                    self._trailing_stop[symbol] = new_trail
                    if price < trail:
                        self._trailing_stop.pop(symbol, None)
                        return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                      reason=f"Trailing stop hit at {trail:.1f}",
                                      strategy_name=self.name)
                else:
                    new_trail = min(trail, price + atr * self.params["atr_sl_mult"])
                    self._trailing_stop[symbol] = new_trail
                    if price > trail:
                        self._trailing_stop.pop(symbol, None)
                        return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                      reason=f"Trailing stop hit at {trail:.1f}",
                                      strategy_name=self.name)
            return Signal(SignalType.HOLD, symbol, reason="Holding position", strategy_name=self.name)

        # ---- 开仓条件 ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        if adx < self.params["adx_threshold"]:
            return Signal(SignalType.HOLD, symbol,
                          reason=f"ADX={adx:.1f}<{self.params['adx_threshold']} no trend",
                          strategy_name=self.name)

        sl_mult = self.params["atr_sl_mult"]
        tp_mult = self.params["atr_tp_mult"]

        # 下跌趋势做空
        if price < ema and latest["minus_di"] > latest["plus_di"]:
            self._bars_since_last_trade = 0
            sl = price + atr * sl_mult if atr > 0 else float("nan")
            tp = price - atr * tp_mult if atr > 0 else float("nan")
            self._trailing_stop[symbol] = sl
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason=f"Downtrend ADX={adx:.0f} price<EMA({price:.0f}<{ema:.0f})",
                          confidence=min(0.9, adx/50), strategy_name=self.name)

        # 上涨趋势做多
        if price > ema and latest["plus_di"] > latest["minus_di"]:
            self._bars_since_last_trade = 0
            sl = price - atr * sl_mult if atr > 0 else float("nan")
            tp = price + atr * tp_mult if atr > 0 else float("nan")
            self._trailing_stop[symbol] = sl
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason=f"Uptrend ADX={adx:.0f} price>EMA({price:.0f}>{ema:.0f})",
                          confidence=min(0.9, adx/50), strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol, reason="No signal", strategy_name=self.name)
