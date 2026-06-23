"""
Keltner Channel Mean Reversion Strategy.

逻辑: 当价格突破Keltner通道边界时，反向交易（均值回归）。
- LONG: 价格跌破Keltner下轨 + RSI超卖确认 → 预期回归通道内
- SHORT: 价格突破Keltner上轨 + RSI超买确认 → 预期回归通道内
- Exit: 价格回归到通道中线 或 RSI回到中性区

与DonchianMR互补: Keltner基于ATR(波动率)构建通道，Donchian基于价格极值。
震荡市中两者可能在不同时机触发，增加信号多样性。

Parameters:
    kc_period: EMA周期 (default 20)
    atr_mult: ATR倍数 (default 2.0)
    atr_period: ATR周期 (default 14)
    rsi_period: RSI计算周期 (default 14)
    oversold: RSI超卖阈值 (default 25)
    overbought: RSI超买阈值 (default 75)
    exit_level: 回归到此RSI水平平仓 (default 50)
    cooldown_bars: 交易冷却期 (default 5)
    stop_loss_pct: 固定止损百分比 (default 0.02 = 2%)
    take_profit_pct: 固定止盈百分比 (default 0.04 = 4%)
"""

from typing import List, Optional
import pandas as pd
from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi, compute_atr, compute_ema


class KeltnerMRStrategy(BaseStrategy):
    """Keltner Channel mean reversion — fade the volatility breakout."""

    def __init__(
        self,
        name: str = "KeltnerMR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        kc_period: int = 20,
        atr_mult: float = 2.0,
        atr_period: int = 14,
        rsi_period: int = 14,
        oversold: float = 25.0,
        overbought: float = 75.0,
        exit_level: float = 50.0,
        cooldown_bars: int = 5,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["ETH/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params.update({
            "kc_period": kc_period,
            "atr_mult": atr_mult,
            "atr_period": atr_period,
            "rsi_period": rsi_period,
            "oversold": oversold,
            "overbought": overbought,
            "exit_level": exit_level,
            "cooldown_bars": cooldown_bars,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
        })
        self._bars_since_last_trade = cooldown_bars + 1

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        h, l, c = df["high"], df["low"], df["close"]

        # Keltner Channel
        ema_mid = compute_ema(c, p["kc_period"])
        atr = compute_atr(h, l, c, p["atr_period"])
        ind["kc_upper"] = ema_mid + p["atr_mult"] * atr
        ind["kc_lower"] = ema_mid - p["atr_mult"] * atr
        ind["kc_mid"] = ema_mid

        # Break signals: current bar closes outside, previous close inside
        prev_c = c.shift(1)
        prev_upper = ind["kc_upper"].shift(1)
        prev_lower = ind["kc_lower"].shift(1)
        ind["break_lower"] = (c < ind["kc_lower"]) & (prev_c >= prev_lower)
        ind["break_upper"] = (c > ind["kc_upper"]) & (prev_c <= prev_upper)

        # Mean reversion signals: cross back above/below midline
        ind["cross_above_mid"] = (c > ind["kc_mid"]) & (c.shift(1) <= ind["kc_mid"].shift(1))
        ind["cross_below_mid"] = (c < ind["kc_mid"]) & (c.shift(1) >= ind["kc_mid"].shift(1))

        # RSI
        ind["rsi"] = compute_rsi(c, p["rsi_period"])
        ind["rsi_above_exit"] = (ind["rsi"] > p["exit_level"]) & (ind["rsi"].shift(1) <= p["exit_level"])
        ind["rsi_below_exit"] = (ind["rsi"] < p["exit_level"]) & (ind["rsi"].shift(1) >= p["exit_level"])

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = max(self.params["kc_period"], self.params["rsi_period"],
                       self.params["atr_period"]) + 5
        if (early := self._check_ready(symbol, min_bars)):
            return early

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        has_pos = self.has_position(symbol)
        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]

        # ---- Position management ----
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG":
                if latest.get("cross_above_mid", False) or latest.get("rsi_above_exit", False):
                    trigger = "mid" if latest.get("cross_above_mid", False) else "RSI"
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason=f"Reversion above {trigger} — closing long",
                                  strategy_name=self.name)
            else:  # SHORT
                if latest.get("cross_below_mid", False) or latest.get("rsi_below_exit", False):
                    trigger = "mid" if latest.get("cross_below_mid", False) else "RSI"
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason=f"Reversion below {trigger} — closing short",
                                  strategy_name=self.name)
            return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

        # ---- Entry ----
        cooldown = self.params["cooldown_bars"]
        if self._bars_since_last_trade <= cooldown:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        rsi = float(latest.get("rsi", 50))
        kc_upper = float(latest.get("kc_upper", 0))
        kc_lower = float(latest.get("kc_lower", 0))

        # LONG: price below lower band + RSI oversold
        # Use sustained condition (price < kc_lower) instead of break_lower
        # to handle engine restarts where the break happened before startup.
        if price < kc_lower and rsi < self.params["oversold"]:
            self._bars_since_last_trade = 0
            sl = price * (1.0 - sl_pct)
            tp = price * (1.0 + tp_pct)
            confidence = min(0.75, 0.55 + (kc_lower - price) / kc_lower * 10)
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"Keltner lower break ({kc_lower:.1f}) + RSI({rsi:.1f}) oversold → LONG reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        # SHORT: price above upper band + RSI overbought
        # Use sustained condition (price > kc_upper) instead of break_upper
        if price > kc_upper and rsi > self.params["overbought"]:
            self._bars_since_last_trade = 0
            sl = price * (1.0 + sl_pct)
            tp = price * (1.0 - tp_pct)
            confidence = min(0.75, 0.55 + (price - kc_upper) / kc_upper * 10)
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"Keltner upper break ({kc_upper:.1f}) + RSI({rsi:.1f}) overbought → SHORT reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        return Signal(SignalType.HOLD, symbol,
                      reason=f"RSI={rsi:.1f}, KC=[{kc_lower:.1f},{kc_upper:.1f}], no trigger",
                      strategy_name=self.name, timestamp=df.index[-1])

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["kc_period"], self.params["rsi_period"],
                                 self.params["atr_period"]) * 3,
        }
