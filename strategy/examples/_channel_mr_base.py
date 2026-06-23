"""
Channel Mean Reversion Base Strategy.

Extracts the shared logic across DonchianMR, KeltnerMR, and BandMR strategies
(~50% duplication in PERF-091). Subclasses only implement channel computation
and optional entry filters — position management, cooldown, RSI exit, and
signal construction are all handled here.

Subclass contract:
    1. Implement _compute_channel_bands(df, prev_c) → dict with keys:
       'upper', 'lower', 'mid' (all pd.Series aligned to df.index)
    2. Optionally override _additional_entry_check(latest, side) → bool
    3. Optionally override _get_channel_display_name() → str (default "DC")
    4. Override get_required_data() if channel-specific lookback differs
"""

from typing import List, Optional
import pandas as pd
from ..base import BaseStrategy, Signal, SignalType
from ..indicators import compute_rsi


class ChannelMRBaseStrategy(BaseStrategy):
    """Base class for channel-based mean reversion strategies."""

    def __init__(
        self,
        name: str,
        symbols: List[str],
        timeframes: List[str],
        rsi_period: int = 14,
        oversold: float = 25.0,
        overbought: float = 75.0,
        exit_level: float = 50.0,
        cooldown_bars: int = 5,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        channel_params: Optional[dict] = None,
    ):
        super().__init__(name=name, symbols=symbols, timeframes=timeframes)
        self.params.update({
            "rsi_period": rsi_period,
            "oversold": oversold,
            "overbought": overbought,
            "exit_level": exit_level,
            "cooldown_bars": cooldown_bars,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
        })
        if channel_params:
            self.params.update(channel_params)
        self._bars_since_last_trade = cooldown_bars + 1

    # ── Subclass override points ──────────────────────────────────────

    def _compute_channel_bands(self, df: pd.DataFrame, prev_c: pd.Series) -> dict:
        """Override: return {'upper': Series, 'lower': Series, 'mid': Series}.

        All series must be the same length as df.index and use df.index.
        """
        raise NotImplementedError

    def _additional_entry_check(self, latest: pd.Series, side: str) -> bool:
        """Override: extra filter before entry (e.g., volume surge). Default: pass."""
        return True

    def _get_channel_display_name(self) -> str:
        """Override: e.g., 'DC', 'KC' — used in reason strings."""
        return "DC"

    def _confidence_scale(self) -> float:
        """Override: scale factor for confidence. Default 0.75 max, 0.55 base."""
        return 1.0

    def _entry_confidence(self, price: float, band: float, side: str) -> float:
        """Compute entry confidence from distance to band."""
        base = 0.55 * self._confidence_scale()
        max_conf = 0.75 * self._confidence_scale()
        if side == "LONG":
            raw = base + (band - price) / band * 10
        else:  # SHORT
            raw = base + (price - band) / band * 10
        return min(max_conf, raw)

    # ── Shared preprocessing ──────────────────────────────────────────

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        key = (symbol, timeframe)
        p = self.params
        c = df["close"]

        # Delegate channel computation to subclass
        ch = self._compute_channel_bands(df, c.shift(1))
        upper = ch["upper"]
        lower = ch["lower"]
        mid = ch["mid"]

        ind = pd.DataFrame(index=df.index)
        ind["ch_upper"] = upper
        ind["ch_lower"] = lower
        ind["ch_mid"] = mid

        # Break signals: current bar closes outside, previous inside
        prev_c = c.shift(1)
        prev_upper = upper.shift(1)
        prev_lower = lower.shift(1)
        ind["break_lower"] = (c < lower) & (prev_c >= prev_lower)
        ind["break_upper"] = (c > upper) & (prev_c <= prev_upper)

        # Mean reversion exit: cross back above/below midline
        prev_mid = mid.shift(1)
        ind["cross_above_mid"] = (c > mid) & (c.shift(1) <= prev_mid)
        ind["cross_below_mid"] = (c < mid) & (c.shift(1) >= prev_mid)

        # RSI
        ind["rsi"] = compute_rsi(c, p["rsi_period"])
        ind["rsi_above_exit"] = (ind["rsi"] > p["exit_level"]) & (ind["rsi"].shift(1) <= p["exit_level"])
        ind["rsi_below_exit"] = (ind["rsi"] < p["exit_level"]) & (ind["rsi"].shift(1) >= p["exit_level"])

        self._indicators[key] = ind

    # ── Shared signal generation ──────────────────────────────────────

    def generate_signal(self, symbol: str) -> Signal:
        tf = self.timeframes[0]
        key = (symbol, tf)
        ind = self._indicators.get(key)
        df = self._data.get(key)

        min_bars = self._min_bars_required()
        if (early := self._check_ready(symbol, min_bars)):
            return early

        self._bars_since_last_trade += 1
        latest = ind.iloc[-1]
        price = float(df["close"].iloc[-1])
        has_pos = self.has_position(symbol)
        sl_pct = self.params["stop_loss_pct"]
        tp_pct = self.params["take_profit_pct"]
        ch_name = self._get_channel_display_name()

        # ── Position management ──
        if has_pos:
            pos = self._positions[symbol]
            if pos["side"] == "LONG":
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

        # ── Entry cooldown ──
        if self._bars_since_last_trade <= self.params["cooldown_bars"]:
            return Signal(SignalType.HOLD, symbol, reason="Cooldown", strategy_name=self.name)

        rsi = float(latest.get("rsi", 50))
        ch_lower = float(latest.get("ch_lower", 0))
        ch_upper = float(latest.get("ch_upper", 0))

        # LONG: price below lower band + RSI oversold
        if price < ch_lower and rsi < self.params["oversold"] and self._additional_entry_check(latest, "LONG"):
            self._bars_since_last_trade = 0
            sl = price * (1.0 - sl_pct)
            tp = price * (1.0 + tp_pct)
            confidence = self._entry_confidence(price, ch_lower, "LONG")
            return Signal(SignalType.LONG, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"{ch_name} lower break ({ch_lower:.1f}) + RSI({rsi:.1f}) oversold → LONG reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        # SHORT: price above upper band + RSI overbought
        if price > ch_upper and rsi > self.params["overbought"] and self._additional_entry_check(latest, "SHORT"):
            self._bars_since_last_trade = 0
            sl = price * (1.0 + sl_pct)
            tp = price * (1.0 - tp_pct)
            confidence = self._entry_confidence(price, ch_upper, "SHORT")
            return Signal(SignalType.SHORT, symbol, price=price, quantity=0.001,
                          stop_loss=sl, take_profit=tp,
                          reason=f"{ch_name} upper break ({ch_upper:.1f}) + RSI({rsi:.1f}) overbought → SHORT reversion",
                          confidence=confidence, leverage=3, strategy_name=self.name,
                          timestamp=df.index[-1])

        return Signal(SignalType.HOLD, symbol,
                      reason=f"RSI={rsi:.1f}, {ch_name}=[{ch_lower:.1f},{ch_upper:.1f}], no trigger",
                      strategy_name=self.name, timestamp=df.index[-1])

    def _min_bars_required(self) -> int:
        """Minimum bars needed before generating signals. Override if needed."""
        return self.params.get("rsi_period", 14) + 10

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": self._min_bars_required() * 3,
        }
