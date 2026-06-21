"""
Order Execution Engine for Binance Futures.

Wraps BinanceFuturesClient to provide signal-based execution with
retry logic, quantity/price rounding, and position lifecycle management.
"""

import logging
import time
from typing import Any, Dict, Optional

from .client import BinanceFuturesClient

logger = logging.getLogger(__name__)


class OrderExecutionEngine:
    """Executes trading signals with retry logic and exchange precision handling.

    Signal format:
        {
            'type': 'LONG' | 'SHORT' | 'CLOSE_LONG' | 'CLOSE_SHORT',
            'symbol': 'BTCUSDT' | 'BTC/USDT',
            'quantity': float,
            'price': Optional[float],        # None for market orders
            'leverage': int,                  # default from config
            'stop_loss': Optional[float],
            'take_profit': Optional[float],
        }

    The engine automatically:
      - Sets leverage and margin mode before opening
      - Rounds quantities and prices to exchange precision
      - Retries on transient failures (up to max_retries)
      - Uses reduce_only=True for closing orders
    """

    def __init__(
        self,
        client: BinanceFuturesClient,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ):
        self._client = client
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        # Lazy exchange info cache
        self._exchange_info: Optional[Dict] = None

    def _get_exchange_info(self) -> Dict[str, Any]:
        """Lazy-load and cache exchange info (filters)."""
        if self._exchange_info is None:
            self._exchange_info = self._client.get_exchange_info()
        return self._exchange_info

    # ------------------------------------------------------------------
    # Precision helpers
    # ------------------------------------------------------------------

    def _round_quantity(self, symbol: str, qty: float) -> float:
        """Round quantity to the exchange step size for the given symbol."""
        info = self._get_exchange_info()
        bin_symbol = self._client.to_binance_symbol(symbol)
        symbol_info = info.get(bin_symbol, {})
        step_size = symbol_info.get("step_size", 1e-8)
        min_qty = symbol_info.get("min_qty", 0)

        if step_size <= 0:
            return qty

        precision = 0
        step_str = f"{step_size:.10f}".rstrip("0")
        if "." in step_str:
            precision = len(step_str.split(".")[1])

        rounded = round(qty // step_size * step_size, precision)
        return max(rounded, min_qty)

    def _round_price(self, symbol: str, price: float) -> float:
        """Round price to the exchange tick size for the given symbol."""
        if price is None:
            return 0

        info = self._get_exchange_info()
        bin_symbol = self._client.to_binance_symbol(symbol)
        symbol_info = info.get(bin_symbol, {})
        tick_size = symbol_info.get("tick_size", 1e-8)

        if tick_size <= 0:
            return price

        precision = 0
        tick_str = f"{tick_size:.10f}".rstrip("0")
        if "." in tick_str:
            precision = len(tick_str.split(".")[1])

        return round(price // tick_size * tick_size, precision)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def execute_signal(
        self,
        signal: Dict[str, Any],
        account_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a trading signal.

        Returns:
            {
                'success': bool,
                'action': str,           # 'OPEN_LONG', 'OPEN_SHORT', 'CLOSE', 'ERROR'
                'order': dict | None,    # ccxt order response
                'error': str | None,
                'retries': int,
            }
        """
        signal_type = signal.get("type", "")

        if signal_type in ("LONG", "SHORT"):
            return self._open_position(signal_type, signal)
        elif signal_type in ("CLOSE_LONG", "CLOSE_SHORT"):
            return self._close_position(signal)
        else:
            return {
                "success": False,
                "action": "ERROR",
                "order": None,
                "error": f"Unknown signal type: {signal_type}",
                "retries": 0,
            }

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def _open_position(
        self,
        direction: str,
        signal: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Open a new position (LONG or SHORT).

        Steps:
          1. Set leverage
          2. Set margin mode (isolated)
          3. Round quantity
          4. Place order with retry
        """
        symbol = signal["symbol"]
        quantity = signal["quantity"]
        price = signal.get("price")
        leverage = signal.get("leverage", 3)
        margin_mode = signal.get("margin_mode", "isolated")

        side = "buy" if direction == "LONG" else "sell"
        action = f"OPEN_{direction}"

        # 1. Set leverage
        for attempt in range(self._max_retries):
            try:
                self._client.set_leverage(symbol, leverage)
                break
            except Exception as e:
                logger.warning(
                    "set_leverage attempt %d/%d failed: %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt == self._max_retries - 1:
                    return {
                        "success": False,
                        "action": action,
                        "order": None,
                        "error": f"set_leverage failed after {self._max_retries} attempts: {e}",
                        "retries": attempt + 1,
                    }
                time.sleep(self._retry_delay)

        # 2. Set margin mode
        try:
            self._client.set_margin_mode(symbol, margin_mode)
        except Exception as e:
            # Margin mode setting can fail if already set; not fatal
            logger.debug("set_margin_mode (non-fatal): %s", e)

        # 3. Round quantity & price
        qty = self._round_quantity(symbol, quantity)
        order_price = self._round_price(symbol, price) if price else None
        order_type = "limit" if order_price else "market"

        # 4. Place order with retry
        last_error = None
        for attempt in range(self._max_retries):
            try:
                order = self._client.place_order(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=qty,
                    price=order_price,
                    reduce_only=False,
                )
                return {
                    "success": True,
                    "action": action,
                    "order": order,
                    "error": None,
                    "retries": attempt,
                }
            except Exception as e:
                last_error = e
                logger.warning(
                    "place_order attempt %d/%d failed: %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay)

        return {
            "success": False,
            "action": action,
            "order": None,
            "error": f"place_order failed after {self._max_retries} attempts: {last_error}",
            "retries": self._max_retries,
        }

    def _close_position(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Close an existing position with reduce_only=True.

        Signal type: 'CLOSE_LONG' -> sell, 'CLOSE_SHORT' -> buy
        """
        signal_type = signal["type"]
        symbol = signal["symbol"]
        quantity = signal["quantity"]
        price = signal.get("price")

        side = "sell" if signal_type == "CLOSE_LONG" else "buy"
        action = "CLOSE"

        qty = self._round_quantity(symbol, quantity)
        order_price = self._round_price(symbol, price) if price else None
        order_type = "limit" if order_price else "market"

        last_error = None
        for attempt in range(self._max_retries):
            try:
                order = self._client.place_order(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=qty,
                    price=order_price,
                    reduce_only=True,
                )
                return {
                    "success": True,
                    "action": action,
                    "order": order,
                    "error": None,
                    "retries": attempt,
                }
            except Exception as e:
                last_error = e
                logger.warning(
                    "close_position attempt %d/%d failed: %s",
                    attempt + 1, self._max_retries, e,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_delay)

        return {
            "success": False,
            "action": action,
            "order": None,
            "error": f"close_position failed after {self._max_retries} attempts: {last_error}",
            "retries": self._max_retries,
        }
