"""Dynamic Grid Trading Strategy — harvests volatility without predicting direction.

Based on arXiv:2506.11921 concept:
- Place a grid of limit buy/sell orders around current price
- Buy low, sell high within a ±band
- Grid center adjusts dynamically as price moves
- Complements trend-following by profiting in ranging/choppy markets
"""

from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from ..base import BaseStrategy, Signal, SignalType


@dataclass
class GridLevel:
    """A single grid level tracking buy/sell pair."""
    buy_price: float
    sell_price: float
    qty: float
    buy_filled: bool = False
    buy_fill_time: Optional[datetime] = None
    sell_filled: bool = False
    realized_pnl: float = 0.0


class DynamicGridStrategy(BaseStrategy):
    """Dynamic Grid Trading — buy low, sell high in a moving price band.

    Parameters:
        grid_range_pct: Total grid range as percent of price (e.g., 3.0 = ±1.5%)
        num_levels: Number of grid levels on each side (e.g., 5 = 5 buy + 5 sell)
        qty_per_level: Position size per grid level in base asset
        rebalance_interval_bars: How often to recenter the grid (bars)
        min_spread_pct: Minimum profit spread per grid pair (e.g., 0.2%)
        leverage: Leverage for orders
    """

    def __init__(
        self,
        name: str = "DynamicGrid",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        grid_range_pct: float = 3.0,
        num_levels: int = 5,
        qty_per_level: float = 0.001,
        rebalance_interval_bars: int = 16,  # ~4h on 15m candles
        min_spread_pct: float = 0.2,
        leverage: int = 3,
        cooldown_bars: int = 0,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["15m"],
        )
        self.params = {
            "grid_range_pct": grid_range_pct,
            "num_levels": num_levels,
            "qty_per_level": qty_per_level,
            "rebalance_interval_bars": rebalance_interval_bars,
            "min_spread_pct": min_spread_pct,
            "leverage": leverage,
            "cooldown_bars": cooldown_bars,
        }
        # Grid state per symbol
        self._grids: Dict[str, Dict] = {}
        self._bars_since_rebalance: Dict[str, int] = {}

    def _init_grid(self, symbol: str, center_price: float):
        """Initialize or recenter the grid around a price."""
        p = self.params
        half_range = p["grid_range_pct"] / 2.0
        step = (half_range * 2) / (p["num_levels"] * 2)  # spacing between levels

        levels = []
        # Buy levels below center, sell levels above center
        for i in range(p["num_levels"]):
            buy_px = center_price * (1 - half_range / 100 + i * step / 100)
            sell_px = buy_px * (1 + p["min_spread_pct"] / 100 + step / 100)
            # Ensure sell is always above buy by at least min_spread
            if sell_px <= buy_px * (1 + p["min_spread_pct"] / 100):
                sell_px = buy_px * (1 + p["min_spread_pct"] / 100 + step / 100)
            levels.append(GridLevel(
                buy_price=round(buy_px, 1),
                sell_price=round(sell_px, 1),
                qty=p["qty_per_level"],
            ))

        self._grids[symbol] = {
            "center": center_price,
            "levels": levels,
            "high_water": center_price * (1 + half_range / 100),
            "low_water": center_price * (1 - half_range / 100),
        }
        self._bars_since_rebalance[symbol] = 0

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute grid state on data arrival — no traditional indicators needed."""
        key = (symbol, timeframe)
        # Store a simple indicator frame (required by framework)
        ind = pd.DataFrame(index=df.index)
        ind["close"] = df["close"]
        ind["high"] = df["high"]
        ind["low"] = df["low"]
        # Volatility estimate for dynamic range adjustment
        ind["atr_pct"] = (
            (df["high"] - df["low"]) / df["close"] * 100
        ).rolling(20).mean()
        self._indicators[key] = ind

    def generate_signal(self, symbol: str) -> Signal:
        """Check grid state and generate entry/exit signals.

        DGT operates differently from standard strategies — it may generate
        multiple signals per bar (one per grid level crossing).
        However, to fit the framework, we return the best single signal here.
        Full grid management is handled by the backtest loop or execution engine.
        """
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        if ind is None or df is None or len(df) < 2:
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason="Insufficient data", strategy_name=self.name,
            )

        current_price = float(df["close"].iloc[-1])
        prev_price = float(df["close"].iloc[-2])
        current_high = float(df["high"].iloc[-1])
        current_low = float(df["low"].iloc[-1])

        # Initialize grid if needed
        if symbol not in self._grids:
            self._init_grid(symbol, current_price)

        grid = self._grids[symbol]
        self._bars_since_rebalance[symbol] = self._bars_since_rebalance.get(symbol, 0) + 1

        # Rebalance grid periodically
        p = self.params
        if self._bars_since_rebalance[symbol] >= p["rebalance_interval_bars"]:
            self._init_grid(symbol, current_price)

        # Check each grid level for fills
        signals_generated = []
        for i, level in enumerate(grid["levels"]):
            # Buy fill: price crossed below buy level
            if not level.buy_filled:
                if current_low <= level.buy_price or (
                    prev_price > level.buy_price and current_price <= level.buy_price
                ):
                    level.buy_filled = True
                    level.buy_fill_time = df.index[-1] if hasattr(df.index[-1], 'to_pydatetime') else datetime.now(timezone.utc)
                    signals_generated.append(Signal(
                        type=SignalType.LONG,
                        symbol=symbol,
                        price=level.buy_price,
                        quantity=level.qty,
                        stop_loss=level.buy_price * 0.97,  # 3% hard stop
                        take_profit=level.sell_price,
                        reason=f"Grid buy level {i+1}/{p['num_levels']} @ {level.buy_price:.1f}",
                        confidence=0.85,
                        leverage=p["leverage"],
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    ))

            # Sell fill (take profit at sell level)
            if level.buy_filled and not level.sell_filled:
                if current_high >= level.sell_price or (
                    prev_price < level.sell_price and current_price >= level.sell_price
                ):
                    level.sell_filled = True
                    level.realized_pnl = (level.sell_price - level.buy_price) * level.qty
                    signals_generated.append(Signal(
                        type=SignalType.CLOSE_LONG,
                        symbol=symbol,
                        price=level.sell_price,
                        quantity=level.qty,
                        reason=f"Grid sell level {i+1} — PnL: {level.realized_pnl:.2f} USDT",
                        confidence=0.9,
                        strategy_name=self.name,
                        timestamp=df.index[-1],
                    ))
                    # Reset level for reuse at new grid center on next rebalance
                    level.buy_filled = False
                    level.sell_filled = False
                    level.buy_fill_time = None
                    level.realized_pnl = 0.0

        if signals_generated:
            # Return the most impactful signal (prefer fills over anything else)
            return signals_generated[0]

        return Signal(
            type=SignalType.HOLD, symbol=symbol,
            reason=f"Grid active: {self._active_levels_count(symbol)}/{p['num_levels']} levels filled",
            strategy_name=self.name,
            timestamp=df.index[-1],
        )

    def _active_levels_count(self, symbol: str) -> int:
        """Count currently filled (awaiting sell) grid levels."""
        if symbol not in self._grids:
            return 0
        return sum(1 for lv in self._grids[symbol]["levels"] if lv.buy_filled and not lv.sell_filled)

    def grid_state(self, symbol: str) -> Optional[Dict]:
        """Return current grid state for reporting."""
        if symbol not in self._grids:
            return None
        g = self._grids[symbol]
        levels = []
        for i, lv in enumerate(g["levels"]):
            levels.append({
                "idx": i,
                "buy": lv.buy_price,
                "sell": lv.sell_price,
                "qty": lv.qty,
                "filled": lv.buy_filled and not lv.sell_filled,
                "pnl": lv.realized_pnl if lv.sell_filled else 0,
            })
        return {
            "center": g["center"],
            "levels": levels,
            "active": self._active_levels_count(symbol),
        }

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": 100,
        }
