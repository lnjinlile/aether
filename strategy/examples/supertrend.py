"""
Supertrend Strategy — ATR-based trend following with dynamic trailing stop.

逻辑:
- 计算 ATR 和 Supertrend 上下轨
- 收盘价上穿上轨 → 趋势转多 → LONG
- 收盘价下穿下轨 → 趋势转空 → SHORT
- 止损设在反向轨道（风险可控，随趋势移动）
- 趋势反转 → 反向开仓（自动平仓+开仓）

优势: 趋势市捕捉大波段，震荡市由轨道宽度自然过滤假突破。
与RSI_MR互补: RSI_MR在震荡市均值回归，Supertrend在趋势市跟踪。
"""
from typing import List, Optional
import pandas as pd
import numpy as np
from ..base import BaseStrategy, Signal, SignalType


class SupertrendStrategy(BaseStrategy):
    """Supertrend 趋势跟踪策略。

    Parameters:
        atr_period: ATR周期 (default 10)
        atr_mult: ATR倍数确定轨道宽度 (default 3.0)
        cooldown_bars: 平仓后冷却bar数 (default 3)
        leverage: 杠杆倍数 (default 3)
    """

    def __init__(
        self,
        name: str = "Supertrend",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        atr_period: int = 10,
        atr_mult: float = 3.0,
        cooldown_bars: int = 3,
        leverage: int = 3,
    ):
        super().__init__(name, symbols or ["BTC/USDT"], timeframes or ["1h"])
        self.params.update({
            "atr_period": atr_period,
            "atr_mult": atr_mult,
            "cooldown_bars": cooldown_bars,
            "leverage": leverage,
        })
        self._bars_since_last_trade = cooldown_bars + 1
        # Track per-symbol: -1=downtrend, 1=uptrend, 0=unset
        self._trend = {}

    @staticmethod
    def _compute_supertrend(
        high: pd.Series, low: pd.Series, close: pd.Series,
        atr_period: int, atr_mult: float
    ) -> pd.DataFrame:
        """Compute Supertrend indicator.

        Returns DataFrame with columns: trend (1=UP, -1=DOWN), upper, lower
        """
        n = len(close)

        # ATR
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(span=atr_period, adjust=False).mean()

        # Basic bands
        hl2 = (high + low) / 2
        basic_upper = hl2 + atr_mult * atr
        basic_lower = hl2 - atr_mult * atr

        # Final bands and trend
        final_upper = pd.Series(np.nan, index=close.index)
        final_lower = pd.Series(np.nan, index=close.index)
        trend = pd.Series(0, index=close.index)

        # Find first valid ATR bar
        first_valid = atr.first_valid_index()
        if first_valid is None:
            return pd.DataFrame({
                "trend": trend, "upper": final_upper, "lower": final_lower,
                "atr": atr, "signal_bull": pd.Series(False, index=close.index),
                "signal_bear": pd.Series(False, index=close.index),
            }, index=close.index)

        first_idx = close.index.get_loc(first_valid)

        # Initialize first valid bar
        final_upper.iloc[first_idx] = basic_upper.iloc[first_idx]
        final_lower.iloc[first_idx] = basic_lower.iloc[first_idx]
        # Trend initialized to 0 (unset) until we get a crossover

        for i in range(first_idx + 1, n):
            prev_idx = i - 1
            # Final Upper Band
            if basic_upper.iloc[i] < final_upper.iloc[i-1] or close.iloc[i-1] > final_upper.iloc[i-1]:
                final_upper.iloc[i] = basic_upper.iloc[i]
            else:
                final_upper.iloc[i] = final_upper.iloc[i-1]

            # Final Lower Band
            if basic_lower.iloc[i] > final_lower.iloc[i-1] or close.iloc[i-1] < final_lower.iloc[i-1]:
                final_lower.iloc[i] = basic_lower.iloc[i]
            else:
                final_lower.iloc[i] = final_lower.iloc[i-1]

            # Trend
            if pd.isna(final_lower.iloc[i-1]):
                trend.iloc[i] = 0
            elif close.iloc[i] > final_lower.iloc[i-1]:
                trend.iloc[i] = 1  # Uptrend
            elif close.iloc[i] < final_upper.iloc[i-1]:
                trend.iloc[i] = -1  # Downtrend
            else:
                trend.iloc[i] = trend.iloc[i-1]

        # Signal: trend flip (1 bar before current)
        signal_bull = (trend == 1) & (trend.shift(1) == -1)
        signal_bear = (trend == -1) & (trend.shift(1) == 1)

        return pd.DataFrame({
            "trend": trend,
            "upper": final_upper,
            "lower": final_lower,
            "atr": atr,
            "signal_bull": signal_bull,
            "signal_bear": signal_bear,
        }, index=close.index)

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        ind = self._compute_supertrend(
            df["high"], df["low"], df["close"],
            p["atr_period"], p["atr_mult"]
        )
        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = self.params["atr_period"] + 5
        if ind is None or df is None or len(ind) < min_bars:
            return Signal(SignalType.HOLD, symbol,
                          reason=f"Need {min_bars} bars, have {len(ind) if ind is not None else 0}",
                          strategy_name=self.name)

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        trend = int(latest["trend"])
        atr = float(latest.get("atr", 0))
        upper = float(latest.get("upper", 0))
        lower = float(latest.get("lower", 0))
        has_pos = self.has_position(symbol)
        lev = self.params["leverage"]

        # ---- 持仓管理 ----
        if has_pos:
            pos = self._positions[symbol]

            # 趋势反转 → 反手（平仓+反向开仓）
            if pos["side"] == "LONG" and trend == -1:
                self._bars_since_last_trade = 0
                sl = upper if upper > price else price + atr * 1.5
                tp = price - atr * 3.0 if atr > 0 else float("nan")
                return Signal(SignalType.CLOSE_LONG, symbol, price=price,
                              reason=f"Trend flipped to DOWN, closing long",
                              strategy_name=self.name)

            if pos["side"] == "SHORT" and trend == 1:
                self._bars_since_last_trade = 0
                sl = lower if lower < price else price - atr * 1.5
                tp = price + atr * 3.0 if atr > 0 else float("nan")
                return Signal(SignalType.CLOSE_SHORT, symbol, price=price,
                              reason=f"Trend flipped to UP, closing short",
                              strategy_name=self.name)

            return Signal(SignalType.HOLD, symbol, reason="Holding position",
                          strategy_name=self.name)

        # ---- 开仓条件 ----
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        # 多头信号：趋势从 DOWN 翻转为 UP
        if latest.get("signal_bull", False):
            self._bars_since_last_trade = 0
            sl = lower if lower < price else price - atr * 1.5
            tp = price + atr * 3.0 if atr > 0 else float("nan")
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=lev,
                          reason=f"Supertrend flip UP: price {price:.1f} crossed above lower band {lower:.1f}",
                          confidence=0.65, strategy_name=self.name)

        # 空头信号：趋势从 UP 翻转为 DOWN
        if latest.get("signal_bear", False):
            self._bars_since_last_trade = 0
            sl = upper if upper > price else price + atr * 1.5
            tp = price - atr * 3.0 if atr > 0 else float("nan")
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp, leverage=lev,
                          reason=f"Supertrend flip DOWN: price {price:.1f} crossed below upper band {upper:.1f}",
                          confidence=0.65, strategy_name=self.name)

        return Signal(SignalType.HOLD, symbol,
                      reason=f"No trend flip (trend={'UP' if trend==1 else 'DOWN' if trend==-1 else 'UNSET'})",
                      strategy_name=self.name)
