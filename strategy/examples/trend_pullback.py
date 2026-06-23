"""
趋势回调策略 v2 — 修复版 + 增强

v1 Bug: near_ema 指标已计算但从未用于入场过滤，导致策略等同于 TrendFollow
        (永远在趋势方向开仓)，这是 v1 回撤 45.9% 的根因。

v2 改进:
- 修复: 入场必须同时满足 trend direction + near_ema (价格回调至EMA附近)
- 增强: 新增 RSI 过滤，避免在超买/超卖区域顺势入场
- 增强: EMA 双线对齐 (EMA50 > EMA100 才有上升趋势)
- 增强: 更宽的冷却期 (减少过度交易)
- 新增: BTC 支持

逻辑:
1. 趋势确认: EMA50斜率 + EMA双线对齐
2. 回调入场: 价格回调至EMA50附近 (1 ATR 内)
3. RSI 确认: 30 < RSI < 70 (不追极端)
4. ATR 动态止损, 固定盈亏比
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi


class TrendPullback(BaseStrategy):
    """趋势回调入场 — 顺势而为 v2"""

    def __init__(
        self,
        name: str = "TrendPB",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        ema_period: int = 100,
        ema_fast: int = 50,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 3.0,
        rsi_period: int = 14,
        rsi_low: float = 30.0,
        rsi_high: float = 70.0,
        cooldown_bars: int = 8,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["1h"])
        self.params.update({
            "ema_period": ema_period,
            "ema_fast": ema_fast,
            "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult,
            "atr_tp_mult": atr_tp_mult,
            "rsi_period": rsi_period,
            "rsi_low": rsi_low,
            "rsi_high": rsi_high,
            "cooldown_bars": cooldown_bars,
        })
        self._bars_since_last_trade = cooldown_bars + 1

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        close, h, l = df["close"], df["high"], df["low"]

        # EMA dual line: fast(50) + slow(100)
        ind["ema_fast"] = close.ewm(span=p["ema_fast"], adjust=False).mean()
        ind["ema_slow"] = close.ewm(span=p["ema_period"], adjust=False).mean()
        # Trend direction: fast > slow and fast slope > 0
        ind["ema_fast_slope"] = ind["ema_fast"].diff(5)
        ind["ema_aligned_up"] = (ind["ema_fast"] > ind["ema_slow"]) & (ind["ema_fast_slope"] > 0)
        ind["ema_aligned_down"] = (ind["ema_fast"] < ind["ema_slow"]) & (ind["ema_fast_slope"] < 0)

        # ATR
        tr1 = h - l
        tr2 = (h - close.shift()).abs()
        tr3 = (l - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=p["atr_period"], adjust=False).mean()

        # Pullback detection: price near fast EMA (within 1 ATR)
        dist_from_ema = (close - ind["ema_fast"]).abs()
        ind["near_ema"] = dist_from_ema < ind["atr"]
        ind["above_ema"] = close > ind["ema_fast"]

        # RSI
        ind["rsi"] = compute_rsi(close, p["rsi_period"])

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
        atr = float(latest["atr"])
        rsi = float(latest.get("rsi", 50))
        has_pos = self.has_position(symbol)

        uptrend = bool(latest["ema_aligned_up"])
        downtrend = bool(latest["ema_aligned_down"])
        near_ema = bool(latest["near_ema"])

        # ---- Position management ----
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]

            # Trend break → close immediately
            if pos["side"] == "LONG" and not uptrend:
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason="Trend break — EMA alignment lost", strategy_name=self.name)
            if pos["side"] == "SHORT" and not downtrend:
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason="Trend break — EMA alignment lost", strategy_name=self.name)

            # RSI exit: if RSI goes extreme, take profit early
            if pos["side"] == "LONG" and rsi > 75:
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason=f"RSI overbought {rsi:.0f} — early exit", strategy_name=self.name)
            if pos["side"] == "SHORT" and rsi < 25:
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason=f"RSI oversold {rsi:.0f} — early exit", strategy_name=self.name)

            # ATR stop loss / take profit (capped at 5% of price)
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

        # ---- Entry: trend + pullback + RSI filter (after cooldown) ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        rsi_ok = self.params["rsi_low"] < rsi < self.params["rsi_high"]

        # LONG: uptrend + price near EMA + RSI not overbought
        if uptrend and near_ema and rsi_ok:
            self._bars_since_last_trade = 0
            sl = price - atr * self.params["atr_sl_mult"]
            tp = price + atr * self.params["atr_tp_mult"]
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3, confidence=0.65,
                          reason=f"Uptrend pullback near EMA | RSI={rsi:.0f}",
                          strategy_name=self.name)

        # SHORT: downtrend + price near EMA + RSI not oversold
        if downtrend and near_ema and rsi_ok:
            self._bars_since_last_trade = 0
            sl = price + atr * self.params["atr_sl_mult"]
            tp = price - atr * self.params["atr_tp_mult"]
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3, confidence=0.65,
                          reason=f"Downtrend pullback near EMA | RSI={rsi:.0f}",
                          strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol,
                      reason=f"No setup: uptrend={uptrend} downtrend={downtrend} near_ema={near_ema} rsi={rsi:.0f}",
                      strategy_name=self.name)
