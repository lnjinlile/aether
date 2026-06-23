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
from ._channel_mr_base import ChannelMRBaseStrategy
from ..indicators import compute_atr, compute_ema


class KeltnerMRStrategy(ChannelMRBaseStrategy):
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
            rsi_period=rsi_period,
            oversold=oversold,
            overbought=overbought,
            exit_level=exit_level,
            cooldown_bars=cooldown_bars,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            channel_params={
                "kc_period": kc_period,
                "atr_mult": atr_mult,
                "atr_period": atr_period,
            },
        )

    def _compute_channel_bands(self, df: pd.DataFrame, prev_c: pd.Series) -> dict:
        p = self.params
        h, l, c = df["high"], df["low"], df["close"]
        ema_mid = compute_ema(c, p["kc_period"])
        atr = compute_atr(h, l, c, p["atr_period"])
        upper = ema_mid + p["atr_mult"] * atr
        lower = ema_mid - p["atr_mult"] * atr
        mid = ema_mid
        return {"upper": upper, "lower": lower, "mid": mid}

    def _get_channel_display_name(self) -> str:
        return "KC"

    def _min_bars_required(self) -> int:
        return max(self.params["kc_period"], self.params["rsi_period"],
                   self.params["atr_period"]) + 5
