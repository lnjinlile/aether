"""
Donchian Channel Trend Following Strategy (Turtle-style).

逻辑: 与 DonchianMR 相反 — 顺势突破而非逆势回归。
- LONG: 价格突破 Donchian N日高点 → 趋势向上，做多跟随
- SHORT: 价格突破 Donchian N日低点 → 趋势向下，做空跟随
- Exit: 反向突破（突破反向通道边界）或 ATR trailing stop
- ADX 过滤器: ADX > threshold 时才入场，避免震荡市假突破

与现有策略的关系:
- DonchianMR: 突破边界 → 反向交易（均值回归）→ 适合震荡
- DonchianTrend: 突破边界 → 顺势交易（趋势跟随）→ 适合趋势
- 两者互补，相关性低

Parameters:
    donchian_period: Donchian通道周期 (default 20)
    adx_period: ADX计算周期 (default 14)
    adx_threshold: ADX最低阈值，低于此值不交易 (default 25)
    atr_period: ATR周期，用于止损止盈 (default 14)
    atr_sl_mult: 止损ATR倍数 (default 2.0)
    atr_tp_mult: 止盈ATR倍数 (default 4.0)
    cooldown_bars: 交易冷却期 (default 5)
"""

from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_atr


def _compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Compute Average Directional Index (ADX)."""
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    up_move = high - high.shift()
    down_move = low.shift() - low

    plus_dm = pd.Series(0.0, index=high.index)
    minus_dm = pd.Series(0.0, index=high.index)

    mask_plus = (up_move > down_move) & (up_move > 0)
    mask_minus = (down_move > up_move) & (down_move > 0)
    plus_dm[mask_plus] = up_move[mask_plus]
    minus_dm[mask_minus] = down_move[mask_minus]

    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(span=period, adjust=False).mean()

    return adx


class DonchianTrendStrategy(BaseStrategy):
    """Donchian Channel trend following — trade the breakout, don't fade it."""

    def __init__(
        self,
        name: str = "DonchianTrend",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        donchian_period: int = 20,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        atr_period: int = 14,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
        cooldown_bars: int = 5,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params.update({
            "donchian_period": donchian_period,
            "adx_period": adx_period,
            "adx_threshold": adx_threshold,
            "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult,
            "atr_tp_mult": atr_tp_mult,
            "cooldown_bars": cooldown_bars,
        })
        self._bars_since_last_trade = cooldown_bars + 1
        self._trailing_stop = {}  # symbol -> trailing stop level

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = pd.DataFrame(index=df.index)

        h, l, c = df["high"], df["low"], df["close"]

        # Donchian Channel (shifted to avoid look-ahead)
        prev_c = c.shift(1)
        ind["dc_upper"] = prev_c.rolling(window=p["donchian_period"]).max()
        ind["dc_lower"] = prev_c.rolling(window=p["donchian_period"]).min()

        # Breakout signals: current bar breaks through previous-bar channel
        ind["break_upper"] = (c > ind["dc_upper"]) & (c.shift(1) <= ind["dc_upper"].shift(1))
        ind["break_lower"] = (c < ind["dc_lower"]) & (c.shift(1) >= ind["dc_lower"].shift(1))

        # ADX for trend filter
        ind["adx"] = _compute_adx(h, l, c, p["adx_period"])

        # ATR for stop/target
        ind["atr"] = compute_atr(h, l, c, p["atr_period"])

        # Reverse breakout for exit
        ind["break_lower_exit"] = (c < ind["dc_lower"])  # exit long when breaks lower
        ind["break_upper_exit"] = (c > ind["dc_upper"])  # exit short when breaks upper

        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = max(self.params["donchian_period"], self.params["adx_period"],
                       self.params["atr_period"]) + 5
        if (early := self._check_ready(symbol, min_bars)):
            return early

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        atr = float(latest.get("atr", 0))
        has_pos = self.has_position(symbol)
        sl_mult = self.params["atr_sl_mult"]
        tp_mult = self.params["atr_tp_mult"]

        # ---- 持仓管理 ----
        if has_pos:
            pos = self._positions[symbol]
            # 反向突破 → 平仓
            if pos["side"] == "LONG" and latest.get("break_lower_exit", False):
                self._trailing_stop.pop(symbol, None)
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason=f"Price broke below DC lower — closing long",
                              strategy_name=self.name)
            if pos["side"] == "SHORT" and latest.get("break_upper_exit", False):
                self._trailing_stop.pop(symbol, None)
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason=f"Price broke above DC upper — closing short",
                              strategy_name=self.name)

            # ATR trailing stop
            trail = self._trailing_stop.get(symbol)
            if trail and atr > 0:
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

            return Signal(SignalType.HOLD, symbol, reason="Holding",
                          strategy_name=self.name, timestamp=df.index[-1])

        # ---- 开仓 ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown",
                          strategy_name=self.name, timestamp=df.index[-1])

        # ADX 趋势过滤器
        adx = float(latest.get("adx", 0))
        if adx < self.params["adx_threshold"]:
            return Signal(SignalType.HOLD, symbol,
                          reason=f"ADX={adx:.1f} < {self.params['adx_threshold']} (no trend)",
                          strategy_name=self.name, timestamp=df.index[-1])

        if atr <= 0:
            return Signal(SignalType.HOLD, symbol, reason="ATR not computed",
                          strategy_name=self.name)

        # 向上突破 → 做多（趋势跟随）
        if latest.get("break_upper", False):
            self._bars_since_last_trade = 0
            sl = price - atr * sl_mult
            tp = price + atr * tp_mult
            self._trailing_stop[symbol] = sl
            confidence = min(0.75, 0.55 + (adx - self.params["adx_threshold"]) / 30)
            dc_upper = float(latest.get("dc_upper", 0))
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason=f"DC upper breakout ({dc_upper:.1f}) + ADX({adx:.1f}) → LONG trend",
                          confidence=confidence, strategy_name=self.name,
                          timestamp=df.index[-1])

        # 向下突破 → 做空（趋势跟随）
        if latest.get("break_lower", False):
            self._bars_since_last_trade = 0
            sl = price + atr * sl_mult
            tp = price - atr * tp_mult
            self._trailing_stop[symbol] = sl
            confidence = min(0.75, 0.55 + (adx - self.params["adx_threshold"]) / 30)
            dc_lower = float(latest.get("dc_lower", 0))
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=3,
                          reason=f"DC lower breakout ({dc_lower:.1f}) + ADX({adx:.1f}) → SHORT trend",
                          confidence=confidence, strategy_name=self.name,
                          timestamp=df.index[-1])

        return Signal(SignalType.HOLD, symbol,
                      reason=f"ADX={adx:.1f}, no breakout",
                      strategy_name=self.name, timestamp=df.index[-1])

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["donchian_period"],
                                self.params["adx_period"],
                                self.params["atr_period"]) * 3,
        }
