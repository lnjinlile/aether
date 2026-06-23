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

State: LIVE — deployed with SR=0.539 DD=20.7%.
"""

from typing import List, Optional
import pandas as pd
from ._channel_mr_base import ChannelMRBaseStrategy


class BandMRStrategy(ChannelMRBaseStrategy):
    """Donchian-based mean reversion with relaxed RSI threshold and volume filter."""

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
            rsi_period=rsi_period,
            oversold=oversold,
            overbought=overbought,
            exit_level=exit_level,
            cooldown_bars=cooldown_bars,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            channel_params={
                "donchian_period": donchian_period,
                "volume_filter": volume_filter,
            },
        )
        self._vol_filter = volume_filter

    def _compute_channel_bands(self, df: pd.DataFrame, prev_c: pd.Series) -> dict:
        period = self.params["donchian_period"]
        upper = prev_c.rolling(window=period).max()
        lower = prev_c.rolling(window=period).min()
        mid = (upper + lower) / 2.0
        return {"upper": upper, "lower": lower, "mid": mid}

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Extend base preprocessing with volume surge indicator."""
        super()._preprocess(symbol, timeframe, df)
        key = (symbol, timeframe)
        ind = self._indicators[key]
        v = df["volume"]
        vol_ma = v.rolling(20).mean()
        ind["volume_surge"] = v > (vol_ma * self._vol_filter)

    def _additional_entry_check(self, latest: pd.Series, side: str) -> bool:
        """Require volume surge for entry."""
        return bool(latest.get("volume_surge", True))

    def _get_channel_display_name(self) -> str:
        return "DC"

    def _confidence_scale(self) -> float:
        """BandMR uses lower confidence (0.65 max, 0.45 base)."""
        return 0.65 / 0.75  # scale relative to base class defaults

    def _min_bars_required(self) -> int:
        return max(self.params["donchian_period"], self.params["rsi_period"]) + 10
