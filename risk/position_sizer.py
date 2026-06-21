"""
Dynamic Position Sizer — volatility-targeted, Kelly-aware position sizing.

Implements the industry-standard approach used by professional CTAs:
    position_size = (account_balance × risk_per_trade) / stop_distance

Where stop_distance is either the signal's explicit stop-loss distance or
an ATR-based estimate. Accounts for:
  1. Account balance (dynamic, not hardcoded)
  2. Per-trade risk budget (default 1-2% of account)
  3. Volatility regime (ATR-based — bigger candles = smaller position)
  4. Fractional Kelly Criterion overlay (prevents overbetting)
  5. Maximum position cap (prevents overconcentration)
  6. Exchange minimum notional / step size awareness

Guardian can tune risk_per_trade, max_position_pct, and kelly_fraction
based on strategy backtest Sharpe/DSR from Prometheus.
"""

import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class PositionSize:
    """Result of position sizing calculation."""

    quantity: float          # Number of contracts/coins
    notional: float          # USD value of position
    risk_amount: float       # USD at risk if stop-loss hit
    account_pct: float       # Position notional ÷ balance (%)
    risk_pct: float           # Risk amount ÷ balance (%)
    sizing_method: str       # Which method produced the result
    kelly_fraction: Optional[float] = None  # Kelly fraction used
    atr: Optional[float] = None             # Current ATR value
    stop_distance_pct: Optional[float] = None  # Stop distance from entry


class DynamicPositionSizer:
    """Calculate position size using volatility-targeted risk budgeting.

    Core formula (volatility targeting):
        risk_amount = account_balance × risk_per_trade
        stop_distance = max(signal_stop, atr × atr_mult)
        quantity = risk_amount / (stop_distance × price)
        notional  = quantity × price

    Overlay — Fractional Kelly:
        kelly_f = win_rate - (1 - win_rate) / (avg_win / avg_loss)
        fraction = min(kelly_f * kelly_fraction, 1.0)
        quantity *= fraction

    Caps:
        - Notional ≤ account_balance × max_position_pct
        - Notional ≤ available_balance × max_leverage (exchange constraint)
        - Quantity rounded to exchange step size
    """

    def __init__(
        self,
        risk_per_trade: float = 0.015,      # 1.5% of account per trade
        max_position_pct: float = 0.30,     # Max 30% of balance per position
        max_leverage: float = 5.0,          # Max notional leverage
        atr_multiplier: float = 2.0,        # ATR multiplier for stop distance
        kelly_fraction: float = 0.25,       # Quarter-Kelly (conservative)
        min_notional: float = 12.0,         # Min order value in USDT
        default_stop_pct: float = 0.02,     # 2% stop if no signal stop / no ATR
    ):
        self.risk_per_trade = risk_per_trade
        self.max_position_pct = max_position_pct
        self.max_leverage = max_leverage
        self.atr_multiplier = atr_multiplier
        self.kelly_fraction = kelly_fraction
        self.min_notional = min_notional
        self.default_stop_pct = default_stop_pct

    def configure(self, **kwargs) -> None:
        """Update parameters at runtime (e.g., Guardian heartbeat)."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning("Unknown sizer parameter: %s", key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def size_position(
        self,
        signal: Dict[str, Any],
        account_info: Dict[str, Any],
        ohlcv_df: Optional[pd.DataFrame] = None,
        backtest_stats: Optional[Dict[str, float]] = None,
    ) -> PositionSize:
        """Calculate optimal position size for a trading signal.

        Args:
            signal: {'symbol', 'type', 'price', 'stop_loss', 'take_profit',
                     'leverage', 'quantity_hint', 'confidence'}
            account_info: {'balance', 'available', 'positions'}
            ohlcv_df: OHLCV DataFrame for ATR calculation (optional)
            backtest_stats: {'win_rate', 'avg_win', 'avg_loss', 'sharpe'}
                            from Prometheus for Kelly calculation

        Returns:
            PositionSize with calculated quantity and metadata.
        """
        balance = float(account_info.get("balance", 0))
        available = float(account_info.get("available", balance))
        price = float(signal.get("price", 0) or 0)
        signal_stop = signal.get("stop_loss")

        if balance <= 0 or price <= 0:
            return PositionSize(
                quantity=0, notional=0, risk_amount=0,
                account_pct=0, risk_pct=0, sizing_method="invalid_inputs",
            )

        # --- Step 1: Determine stop distance ---
        stop_distance_pct, atr_val = self._calc_stop_distance(
            signal_stop, price, ohlcv_df
        )

        # --- Step 2: Volatility-targeted position size ---
        risk_amount = balance * self.risk_per_trade
        stop_absolute = stop_distance_pct * price
        quantity = risk_amount / stop_absolute if stop_absolute > 0 else 0
        notional = quantity * price

        sizing_method = "vol_target"

        # --- Step 3: Fractional Kelly overlay ---
        kelly_f = None
        if backtest_stats:
            kelly_f = self._calc_kelly(backtest_stats)
            if kelly_f is not None and kelly_f > 0:
                fraction = min(kelly_f * self.kelly_fraction, 1.0)
                quantity *= fraction
                notional = quantity * price
                sizing_method = "vol_target+kelly"

        # --- Step 4: Apply caps ---
        # Cap 1: Max position as % of balance
        max_notional = balance * self.max_position_pct
        if notional > max_notional:
            quantity = max_notional / price
            notional = max_notional
            sizing_method += "+max_pct_cap"

        # Cap 2: Available balance / leverage constraint
        max_leverage_notional = available * self.max_leverage
        if notional > max_leverage_notional:
            quantity = max_leverage_notional / price
            notional = max_leverage_notional
            sizing_method += "+leverage_cap"

        # Cap 3: Minimum notional (exchange requirement)
        if notional < self.min_notional:
            if sizing_method.startswith("vol_target"):
                sizing_method = "below_min_notional"
            quantity = 0
            notional = 0
            risk_amount = 0

        # Cap 4: Don't exceed available balance
        if notional > available:
            quantity = available / price
            notional = available
            sizing_method += "+available_cap"

        # --- Step 5: Round to reasonable precision ---
        if price > 1000:      # BTC-like
            quantity = round(quantity, 4)
        elif price > 10:       # ETH-like
            quantity = round(quantity, 3)
        else:
            quantity = round(quantity, 1)

        account_pct = (notional / balance * 100) if balance > 0 else 0
        risk_pct = (risk_amount / balance * 100) if balance > 0 else 0

        return PositionSize(
            quantity=quantity,
            notional=round(notional, 2),
            risk_amount=round(risk_amount, 2),
            account_pct=round(account_pct, 1),
            risk_pct=round(risk_pct, 2),
            sizing_method=sizing_method,
            kelly_fraction=round(kelly_f, 4) if kelly_f else None,
            atr=round(atr_val, 2) if atr_val else None,
            stop_distance_pct=round(stop_distance_pct * 100, 2),
        )

    # ------------------------------------------------------------------
    # Internal calculations
    # ------------------------------------------------------------------

    def _calc_stop_distance(
        self,
        signal_stop: Optional[float],
        price: float,
        ohlcv_df: Optional[pd.DataFrame],
    ) -> tuple[float, Optional[float]]:
        """Determine stop distance as a fraction of price.

        Priority: signal stop > ATR-based > default 2%.
        Returns (stop_distance_pct, atr_value_or_None).
        """
        # Option 1: Explicit signal stop loss
        if signal_stop and signal_stop > 0 and not np.isnan(signal_stop):
            if signal_stop < price:  # Long stop
                dist = (price - signal_stop) / price
            else:  # Short stop
                dist = (signal_stop - price) / price
            return max(dist, 0.002), None  # Floor at 0.2% to avoid micro-stops

        # Option 2: ATR-based stop
        atr_val = self._calc_atr(ohlcv_df) if ohlcv_df is not None else None
        if atr_val and atr_val > 0:
            dist = (atr_val * self.atr_multiplier) / price
            return max(dist, 0.005), atr_val  # Floor at 0.5%

        # Option 3: Default fallback
        return self.default_stop_pct, None

    @staticmethod
    def _calc_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
        """Calculate Average True Range from OHLCV DataFrame."""
        if df is None or df.empty or len(df) < period + 1:
            return None
        try:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            close = df["close"].astype(float)
            prev_close = close.shift(1)

            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = true_range.rolling(window=period).mean().iloc[-1]
            return float(atr) if not np.isnan(atr) and atr > 0 else None
        except Exception:
            return None

    @staticmethod
    def _calc_kelly(stats: Dict[str, float]) -> Optional[float]:
        """Calculate Kelly fraction from backtest statistics.

        Kelly formula: f* = win_rate - (1 - win_rate) / (avg_win / avg_loss)

        Requires: win_rate, avg_win, avg_loss.
        """
        win_rate = stats.get("win_rate", 0)
        avg_win = stats.get("avg_win", 0)
        avg_loss = stats.get("avg_loss", 1)  # Avoid div/zero

        if avg_loss <= 0:
            # If avg_loss is zero or negative (always wins), cap Kelly
            avg_loss = avg_win * 0.1 if avg_win > 0 else 0.01

        if win_rate <= 0 or avg_win <= 0:
            return None

        payoff_ratio = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / payoff_ratio
        return max(kelly, 0.0)  # Never negative


# ------------------------------------------------------------------
# Convenience: Calculate position size for a list of signals
# ------------------------------------------------------------------

def size_all_signals(
    signals: Dict[str, Dict[str, Any]],
    account_info: Dict[str, Any],
    ohlcv_cache: Optional[Dict[str, pd.DataFrame]] = None,
    sizer: Optional[DynamicPositionSizer] = None,
) -> Dict[str, PositionSize]:
    """Calculate position sizes for a batch of trading signals.

    Args:
        signals: {strategy_name: signal_dict} from signals.json
        account_info: {'balance', 'available', 'positions'}
        ohlcv_cache: {symbol: DataFrame} for ATR calculation
        sizer: Pre-configured DynamicPositionSizer (uses defaults if None)

    Returns:
        {strategy_name: PositionSize}
    """
    if sizer is None:
        sizer = DynamicPositionSizer()

    results = {}
    for name, sig in signals.items():
        symbol = sig.get("symbol", "")
        df = ohlcv_cache.get(symbol) if ohlcv_cache else None
        results[name] = sizer.size_position(sig, account_info, ohlcv_df=df)

    return results
