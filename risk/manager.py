"""
Risk Manager for Binance Futures quant system.

Validates trading signals against configurable risk limits and
tracks daily PnL. Returns an ALLOW / REJECT / REDUCE decision
for each signal.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a risk check on a trading signal."""

    action: str  # 'ALLOW', 'REJECT', or 'REDUCE'
    reason: str  # Human-readable explanation
    adjusted_quantity: Optional[float] = None  # Reduced qty when action == 'REDUCE'


class RiskManager:
    """Validates signals against configurable position and loss limits.

    Configurable limits (set at init or via configure()):
        max_positions:         Maximum concurrent open positions (default 5)
        max_leverage:          Maximum allowed leverage (default 10)
        max_per_symbol_pct:    Max notional per symbol as fraction of balance (0.10 = 10%)
        max_total_position_pct: Max total notional as fraction of balance (0.50 = 50%)
        daily_loss_limit_pct:   Max daily realized loss as fraction of starting balance (0.05 = 5%)
        min_order_usdt:         Minimum order value in USDT (default 10)
    """

    def __init__(
        self,
        max_positions: int = 5,
        max_leverage: int = 10,
        max_per_symbol_pct: float = 0.10,
        max_total_position_pct: float = 0.50,
        daily_loss_limit_pct: float = 0.05,
        min_order_usdt: float = 10.0,
    ):
        self.max_positions = max_positions
        self.max_leverage = max_leverage
        self.max_per_symbol_pct = max_per_symbol_pct
        self.max_total_position_pct = max_total_position_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.min_order_usdt = min_order_usdt

        # Daily PnL tracking
        self._today: date = date.today()
        self._daily_starting_balance: float = 0.0
        self._daily_realized_pnl: float = 0.0

    def configure(self, **kwargs) -> None:
        """Update risk limits at runtime."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning("Unknown risk parameter: %s", key)

    # ------------------------------------------------------------------
    # Daily PnL tracking
    # ------------------------------------------------------------------

    def _reset_daily_if_needed(self) -> None:
        """Reset daily tracking state at midnight (UTC date roll)."""
        today = date.today()
        if today != self._today:
            self._today = today
            self._daily_starting_balance = 0.0
            self._daily_realized_pnl = 0.0
            logger.info("Daily PnL reset for %s", today)

    def update_daily_balance(self, balance: float) -> None:
        """Record current balance as the starting point for daily PnL tracking."""
        self._reset_daily_if_needed()
        if self._daily_starting_balance == 0.0:
            self._daily_starting_balance = balance

    def record_trade_pnl(self, pnl: float) -> None:
        """Record realized PnL from a closed trade."""
        self._reset_daily_if_needed()
        self._daily_realized_pnl += pnl

    @property
    def daily_pnl(self) -> float:
        """Current daily realized PnL."""
        self._reset_daily_if_needed()
        return self._daily_realized_pnl

    # ------------------------------------------------------------------
    # Signal validation
    # ------------------------------------------------------------------

    def check_signal(
        self,
        signal: Dict[str, Any],
        account_info: Dict[str, Any],
    ) -> RiskCheckResult:
        """Validate a trading signal against all risk limits.

        Args:
            signal: {'type', 'symbol', 'quantity', 'leverage', 'price', ...}
            account_info: {'balance': float, 'available': float,
                           'positions': list, ...}

        Returns:
            RiskCheckResult with action ALLOW / REJECT / REDUCE.
        """
        self._reset_daily_if_needed()

        signal_type = signal.get("type", "")
        symbol = signal.get("symbol", "")
        quantity = signal.get("quantity", 0)
        leverage = signal.get("leverage", 3)
        price = signal.get("price", 0) or 0

        balance = account_info.get("balance", 0)
        available = account_info.get("available", 0)
        positions: List[Dict] = account_info.get("positions", [])

        # For close signals, always allow (risk is reducing)
        if signal_type in ("CLOSE_LONG", "CLOSE_SHORT"):
            return RiskCheckResult(action="ALLOW", reason="Closing position")

        # ---- Check 1: Leverage limit ----
        if leverage > self.max_leverage:
            return RiskCheckResult(
                action="REJECT",
                reason=f"Leverage {leverage}x exceeds max {self.max_leverage}x",
            )

        # ---- Check 2: Position count ----
        if len(positions) >= self.max_positions:
            return RiskCheckResult(
                action="REJECT",
                reason=f"Max positions ({self.max_positions}) reached: {len(positions)} open",
            )

        # ---- Check 3: Daily loss limit ----
        if self._daily_starting_balance > 0:
            daily_loss_limit = self._daily_starting_balance * self.daily_loss_limit_pct
            if self._daily_realized_pnl <= -daily_loss_limit:
                return RiskCheckResult(
                    action="REJECT",
                    reason=(
                        f"Daily loss limit hit: PnL={self._daily_realized_pnl:.2f} "
                        f"vs limit={-daily_loss_limit:.2f}"
                    ),
                )

        # ---- Check 4: Order value too small ----
        order_value = quantity * price
        if price > 0 and order_value < self.min_order_usdt:
            return RiskCheckResult(
                action="REJECT",
                reason=f"Order value ${order_value:.2f} below min ${self.min_order_usdt}",
            )

        # ---- Check 5: Total position allocation ----
        total_notional = sum(
            abs(float(p.get("notional", 0))) for p in positions
        )
        max_total = balance * self.max_total_position_pct
        if total_notional >= max_total:
            return RiskCheckResult(
                action="REJECT",
                reason=f"Total position notional ${total_notional:.2f} exceeds max ${max_total:.2f}",
            )

        # ---- Check 6: Per-symbol allocation ----
        max_symbol_notional = balance * self.max_per_symbol_pct
        symbol_notional = sum(
            abs(float(p.get("notional", 0)))
            for p in positions
            if p.get("symbol", "").replace("/", "").upper() == symbol.replace("/", "").upper()
        )
        remaining_symbol = max_symbol_notional - symbol_notional

        if remaining_symbol <= 0:
            return RiskCheckResult(
                action="REJECT",
                reason=f"Symbol allocation exceeded for {symbol}: "
                f"${symbol_notional:.2f} of ${max_symbol_notional:.2f}",
            )

        # ---- All checks passed ----
        return RiskCheckResult(action="ALLOW", reason="All risk checks passed")
