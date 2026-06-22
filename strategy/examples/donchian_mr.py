"""
Donchian Channel Mean Reversion Strategy.

逻辑: 当价格突破Donchian通道边界时，反向交易（均值回归）。
- LONG: 价格跌破Donchian下轨(period内最低价) → 预期回归通道内
- SHORT: 价格突破Donchian上轨(period内最高价) → 预期回归通道内
- Exit: 价格回归到通道中线 或 RSI回到中性区
- RSI确认过滤器: 仅在RSI极度超卖/超买时入场

与VolBreakout相反: VolBreakout是顺势突破，DonchianMR是逆势回归。
适合震荡/区间市场。

Parameters:
    donchian_period: Donchian通道周期 (default 20)
    rsi_period: RSI计算周期 (default 14)
    oversold: RSI超卖阈值，低于此值才做多 (default 25)
    overbought: RSI超买阈值，高于此值才做空 (default 75)
    exit_level: 回归到此RSI水平平仓 (default 50)
    cooldown_bars: 交易冷却期 (default 5)
    stop_loss_pct: 固定止损百分比 (default 0.02 = 2%)
    take_profit_pct: 固定止盈百分比 (default 0.04 = 4%)
"""

from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class DonchianMRStrategy(BaseStrategy):
    """Donchian Channel mean reversion — fade the breakout."""

    def __init__(
        self,
        name: str = "DonchianMR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        donchian_period: int = 20,
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
            "donchian_period": donchian_period,
            "rsi_period": rsi_period,
            "oversold": oversold,
            "overbought": overbought,
            "exit_level": exit_level,
            "cooldown_bars": cooldown_bars,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
        })
        self._bars_since_last_trade = cooldown_bars + 1

    @staticmethod
    def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
        """Compute RSI with Wilder's smoothing."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        rsi[avg_loss == 0] = 100.0
        rsi[avg_gain == 0] = 0.0
        return rsi

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        h, l, c = df["high"], df["low"], df["close"]

        # Donchian Channel — close-based, shifted so current bar can break through.
        # The channel is computed on the PREVIOUS N bars (shift(1)), then the
        # current bar's close is compared against it. Without the shift, rolling()
        # includes the current bar making breakout impossible.
        prev_c = c.shift(1)
        ind["dc_upper"] = prev_c.rolling(window=p["donchian_period"]).max()
        ind["dc_lower"] = prev_c.rolling(window=p["donchian_period"]).min()
        ind["dc_mid"] = (ind["dc_upper"] + ind["dc_lower"]) / 2.0

        # RSI
        ind["rsi"] = self._compute_rsi(c, p["rsi_period"])

        # 突破下轨信号: 前一根收盘价还在通道内，当前bar跌破下轨
        ind["break_lower"] = (c < ind["dc_lower"]) & (prev_c >= ind["dc_lower"])

        # 突破上轨信号: 前一根bar还在上轨内，当前bar突破上轨
        ind["break_upper"] = (c > ind["dc_upper"]) & (prev_c <= ind["dc_upper"])

        # 回归中线: 从下方/上方穿回中线附近
        ind["cross_above_mid"] = (c > ind["dc_mid"]) & (c.shift(1) <= ind["dc_mid"].shift(1))
        ind["cross_below_mid"] = (c < ind["dc_mid"]) & (c.shift(1) >= ind["dc_mid"].shift(1))

        # RSI 回归中性
        ind["rsi_above_exit"] = (ind["rsi"] > p["exit_level"]) & (ind["rsi"].shift(1) <= p["exit_level"])
        ind["rsi_below_exit"] = (ind["rsi"] < p["exit_level"]) & (ind["rsi"].shift(1) >= p["exit_level"])

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = max(self.params["donchian_period"], self.params["rsi_period"]) + 5
        if ind is None or df is None or len(ind) < min_bars:
            return Signal(SignalType.HOLD, symbol,
                          reason=f"Need {min_bars} bars, have {len(ind) if ind is not None else 0}",
                          strategy_name=self.name)

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        has_pos = self.has_position(symbol)
        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]
        exit_lvl = self.params["exit_level"]

        # ---- 持仓管理 ----
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG":
                # 回归中线 → 平多
                if latest.get("cross_above_mid", False) or latest.get("rsi_above_exit", False):
                    trigger = "mid" if latest.get("cross_above_mid", False) else "RSI"
                    return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                                  reason=f"Price/Reverted above {trigger} — closing long",
                                  strategy_name=self.name)
            else:  # SHORT
                if latest.get("cross_below_mid", False) or latest.get("rsi_below_exit", False):
                    trigger = "mid" if latest.get("cross_below_mid", False) else "RSI"
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason=f"Price/Reverted below {trigger} — closing short",
                                  strategy_name=self.name)
            return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

        # ---- 开仓 ----
        cooldown = self.params["cooldown_bars"]
        if self._bars_since_last_trade <= cooldown:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        rsi = float(latest.get("rsi", 50))
        dc_upper = float(latest.get("dc_upper", 0))
        dc_lower = float(latest.get("dc_lower", 0))
        dc_mid = float(latest.get("dc_mid", 0))

        # 做多: 跌破下轨 + RSI超卖确认
        if latest.get("break_lower", False) and rsi < self.params["oversold"]:
            self._bars_since_last_trade = 0
            sl = price * (1.0 - sl_pct)
            tp = price * (1.0 + tp_pct)
            # confidence based on how far below lower band
            confidence = min(0.75, 0.55 + (dc_lower - price) / dc_lower * 10)
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"Donchian lower break ({dc_lower:.1f}) + RSI({rsi:.1f}) oversold → LONG reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        # 做空: 突破上轨 + RSI超买确认
        if latest.get("break_upper", False) and rsi > self.params["overbought"]:
            self._bars_since_last_trade = 0
            sl = price * (1.0 + sl_pct)
            tp = price * (1.0 - tp_pct)
            confidence = min(0.75, 0.55 + (price - dc_upper) / dc_upper * 10)
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"Donchian upper break ({dc_upper:.1f}) + RSI({rsi:.1f}) overbought → SHORT reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        return Signal(SignalType.HOLD, symbol,
                      reason=f"RSI={rsi:.1f}, DC=[{dc_lower:.1f},{dc_upper:.1f}], no trigger",
                      strategy_name=self.name, timestamp=df.index[-1])

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["donchian_period"], self.params["rsi_period"]) * 3,
        }
