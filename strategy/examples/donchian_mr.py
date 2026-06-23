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
from ._channel_mr_base import ChannelMRBaseStrategy


class DonchianMRStrategy(ChannelMRBaseStrategy):
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
            rsi_period=rsi_period,
            oversold=oversold,
            overbought=overbought,
            exit_level=exit_level,
            cooldown_bars=cooldown_bars,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            channel_params={"donchian_period": donchian_period},
        )

    def _compute_channel_bands(self, df: pd.DataFrame, prev_c: pd.Series) -> dict:
        period = self.params["donchian_period"]
        upper = prev_c.rolling(window=period).max()
        lower = prev_c.rolling(window=period).min()
        mid = (upper + lower) / 2.0
        return {"upper": upper, "lower": lower, "mid": mid}

    def _get_channel_display_name(self) -> str:
        return "DC"

    def _min_bars_required(self) -> int:
        return max(self.params["donchian_period"], self.params["rsi_period"]) + 5
