"""
Band Mean Reversion Strategy — moderate oversold variant.

逻辑: DonchianMR + KeltnerMR 要求 RSI<20 (极度超卖) 才入场，但在缓跌市中
价格突破通道边界时 RSI 往往在 30-40 之间，导致系统零信号。
BandMR 将超卖阈值放宽至 30，配合更紧的止损和更小的止盈，捕捉中等超卖回归。

与 DonchianMR/KeltnerMR 互补：
- DonchianMR: RSI<20 极端超卖 → 高置信度，宽止损
- KeltnerMR: RSI<20 极端超卖 → ATR通道，高置信度
- BandMR:    RSI<30 中等超卖 → 紧止损，快进快出

Parameters:
    donchian_period: Donchian通道周期 (default 20)
    rsi_period: RSI计算周期 (default 14)
    oversold: RSI超卖阈值 (default 30, 比MR策略的20更宽松)
    overbought: RSI超买阈值 (default 75)
    exit_level: 回归到此RSI水平平仓 (default 50)
    cooldown_bars: 交易冷却期 (default 8, 更长的冷却避免过度交易)
    stop_loss_pct: 固定止损百分比 (default 0.01 = 1%, 更紧)
    take_profit_pct: 固定止盈百分比 (default 0.025 = 2.5%, 更小)
    volume_filter: 成交量确认倍数 (default 1.2, 成交量>MA(20)*1.2才入场)

State: DRAFT — not yet backtested. Submit to Prometheus before enabling.
"""

from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi


class BandMRStrategy(BaseStrategy):
    """Donchian-based mean reversion with relaxed RSI threshold."""

    def __init__(
        self,
        name: str = "BandMR",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        donchian_period: int = 20,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 75.0,
        exit_level: float = 50.0,
        cooldown_bars: int = 8,
        stop_loss_pct: float = 0.01,
        take_profit_pct: float = 0.025,
        volume_filter: float = 1.2,
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
            "volume_filter": volume_filter,
        })
        self._bars_since_last_trade = cooldown_bars + 1

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        h, l, c, v = df["high"], df["low"], df["close"], df["volume"]

        # Donchian Channel — shifted to avoid look-ahead
        prev_c = c.shift(1)
        ind["dc_upper"] = prev_c.rolling(window=p["donchian_period"]).max()
        ind["dc_lower"] = prev_c.rolling(window=p["donchian_period"]).min()
        ind["dc_mid"] = (ind["dc_upper"] + ind["dc_lower"]) / 2.0

        # Break signals
        prev_lower = ind["dc_lower"].shift(1)
        ind["break_lower"] = (c < ind["dc_lower"]) & (prev_c >= prev_lower)
        prev_upper = ind["dc_upper"].shift(1)
        ind["break_upper"] = (c > ind["dc_upper"]) & (prev_c <= prev_upper)

        # Mean reversion exit: cross back above/below midline
        ind["cross_above_mid"] = (c > ind["dc_mid"]) & (c.shift(1) <= ind["dc_mid"].shift(1))
        ind["cross_below_mid"] = (c < ind["dc_mid"]) & (c.shift(1) >= ind["dc_mid"].shift(1))

        # RSI
        ind["rsi"] = compute_rsi(c, p["rsi_period"])
        ind["rsi_above_exit"] = (ind["rsi"] > p["exit_level"]) & (ind["rsi"].shift(1) <= p["exit_level"])
        ind["rsi_below_exit"] = (ind["rsi"] < p["exit_level"]) & (ind["rsi"].shift(1) >= p["exit_level"])

        # Volume filter: volume > MA(volume, 20) * volume_filter
        vol_ma = v.rolling(20).mean()
        ind["volume_surge"] = v > (vol_ma * p.get("volume_filter", 1.2))

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = max(self.params["donchian_period"], self.params["rsi_period"]) + 10
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
                                  reason=f"BandMR reversion above {trigger} — closing long",
                                  strategy_name=self.name)
            else:  # SHORT
                if latest.get("cross_below_mid", False) or latest.get("rsi_below_exit", False):
                    trigger = "mid" if latest.get("cross_below_mid", False) else "RSI"
                    return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                                  reason=f"BandMR reversion below {trigger} — closing short",
                                  strategy_name=self.name)
            return Signal(SignalType.HOLD, symbol, reason="Holding", strategy_name=self.name)

        # ---- Entry ----
        cooldown = self.params["cooldown_bars"]
        if self._bars_since_last_trade <= cooldown:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        rsi = float(latest.get("rsi", 50))
        dc_lower = float(latest.get("dc_lower", 0))
        dc_upper = float(latest.get("dc_upper", 0))
        vol_ok = bool(latest.get("volume_surge", True))

        # LONG: price below lower band + RSI < oversold(30) + volume surge
        # Use sustained condition (price < dc_lower) instead of break_lower
        if price < dc_lower and rsi < self.params["oversold"] and vol_ok:
            self._bars_since_last_trade = 0
            sl = price * (1.0 - sl_pct)
            tp = price * (1.0 + tp_pct)
            confidence = min(0.65, 0.45 + (dc_lower - price) / dc_lower * 10)
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"BandMR lower break ({dc_lower:.1f}) + RSI({rsi:.1f}) < {self.params['oversold']} → LONG reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        # SHORT: price above upper band + RSI > overbought(75) + volume surge
        # Use sustained condition (price > dc_upper) instead of break_upper
        if price > dc_upper and rsi > self.params["overbought"] and vol_ok:
            self._bars_since_last_trade = 0
            sl = price * (1.0 + sl_pct)
            tp = price * (1.0 - tp_pct)
            confidence = min(0.65, 0.45 + (price - dc_upper) / dc_upper * 10)
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"BandMR upper break ({dc_upper:.1f}) + RSI({rsi:.1f}) > {self.params['overbought']} → SHORT reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        return Signal(SignalType.HOLD, symbol,
                      reason=f"RSI={rsi:.1f}, DC=[{dc_lower:.1f},{dc_upper:.1f}], vol={vol_ok}, no trigger",
                      strategy_name=self.name, timestamp=df.index[-1])

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["donchian_period"], self.params["rsi_period"]) * 3,
        }
