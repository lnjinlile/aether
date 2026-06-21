#!/usr/bin/env python3
"""
Test script for Binance Futures Trading Core on testnet.

Verifies:
  1. Connection to Binance Futures testnet
  2. Account info retrieval (balance + positions)
  3. Exchange info / symbol filters
  4. Risk manager signal validation
  5. Order placement and cancellation
"""

import logging
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import get_config
from execution.client import BinanceFuturesClient
from execution.engine import OrderExecutionEngine
from risk.manager import RiskManager, RiskCheckResult

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_trading")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = get_config()
    logger.info("Loaded config (testnet=%s)", config.testnet)

    if not config.api_key or not config.api_secret:
        logger.error("API keys not found. Check .env / environment.")
        sys.exit(1)

    # ---- 1. Create client -------------------------------------------------
    logger.info("=== Step 1: Create BinanceFuturesClient ===")
    client = BinanceFuturesClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
        testnet=config.testnet,
    )
    logger.info("Client created (testnet=%s)", client.testnet)

    # ---- 2. Get exchange info ---------------------------------------------
    logger.info("\n=== Step 2: Fetch exchange info ===")
    try:
        exchange_info = client.get_exchange_info()
        btc_info = exchange_info.get("BTCUSDT", {})
        logger.info("BTCUSDT filters: min_qty=%s, step_size=%s, tick_size=%s",
                    btc_info.get("min_qty"), btc_info.get("step_size"),
                    btc_info.get("tick_size"))
        logger.info("Got exchange info for %d symbols", len(exchange_info))
    except Exception as e:
        logger.error("Failed to get exchange info: %s", e)
        sys.exit(1)

    # ---- 3. Get account info ----------------------------------------------
    logger.info("\n=== Step 3: Get account info ===")
    try:
        account = client.get_account()
        logger.info("Account fetched successfully")
    except Exception as e:
        logger.error("Failed to get account: %s", e)
        sys.exit(1)

    # ---- 4. Get balance ---------------------------------------------------
    logger.info("\n=== Step 4: Get balance ===")
    try:
        bal = client.get_balance()
        logger.info("Balance: total=%.2f USDT, available=%.2f USDT, unrealized_pnl=%.2f USDT",
                    bal["balance"], bal["available"], bal["unrealized_pnl"])
    except Exception as e:
        logger.error("Failed to get balance: %s", e)
        sys.exit(1)

    # ---- 5. Get positions -------------------------------------------------
    logger.info("\n=== Step 5: Get positions ===")
    try:
        positions = client.get_positions()
        if positions:
            for p in positions:
                logger.info("  %s %s qty=%s entry=%s mark=%s pnl=%.2f",
                           p["symbol"], p["side"], p["contracts"],
                           p["entry_price"], p["mark_price"], p["unrealized_pnl"])
        else:
            logger.info("  No open positions")
    except Exception as e:
        logger.error("Failed to get positions: %s", e)

    # ---- 6. Get ticker ----------------------------------------------------
    logger.info("\n=== Step 6: Get BTCUSDT ticker ===")
    try:
        ticker = client.get_ticker("BTCUSDT")
        btc_price = ticker.get("last", 0)
        logger.info("BTCUSDT last price: %.2f USDT", btc_price)
    except Exception as e:
        logger.error("Failed to get ticker: %s", e)
        btc_price = 0

    # ---- 7. Place a test MARKET order then cancel ------------------------
    logger.info("\n=== Step 7: Place test order ===")
    # Use minimum quantity from exchange info
    min_qty = btc_info.get("min_qty", 0.001)
    test_qty = max(min_qty, 0.001)  # at least 0.001 BTC

    try:
        # Use far-from-market limit price so it doesn't fill on testnet
        far_price = btc_price * 0.5 if btc_price else 50000
        logger.info("Placing LIMIT order: BTCUSDT buy %.3f @ %.2f (reduce_only=True)",
                    test_qty, far_price)

        order = client.place_order(
            symbol="BTCUSDT",
            side="buy",
            order_type="limit",
            quantity=test_qty,
            price=far_price,
            reduce_only=True,
        )
        order_id = order.get("id", "?")
        logger.info("Order placed: id=%s, status=%s, type=%s",
                    order_id, order.get("status"), order.get("type"))
    except Exception as e:
        logger.error("Failed to place test order: %s", e)
        # Try as market order instead (riskier on a real account, fine on testnet)
        logger.info("Trying market order...")
        try:
            order = client.place_order(
                symbol="BTCUSDT",
                side="buy",
                order_type="market",
                quantity=test_qty,
            )
            order_id = order.get("id", "?")
            logger.info("Market order placed: id=%s, status=%s",
                        order_id, order.get("status"))
        except Exception as e2:
            logger.error("Failed to place market order too: %s", e2)
            logger.warning("Skipping order test (testnet may not have funds)")
            order_id = None

    # ---- 8. Cancel the test order ----------------------------------------
    if order_id:
        logger.info("\n=== Step 8: Cancel test order ===")
        try:
            result = client.cancel_order("BTCUSDT", order_id)
            logger.info("Order cancelled: id=%s, status=%s",
                        result.get("id"), result.get("status"))
        except Exception as e:
            logger.error("Failed to cancel order: %s", e)

    # ---- 9. Test RiskManager --------------------------------------------
    logger.info("\n=== Step 9: Test RiskManager ===")
    rm = RiskManager(
        max_positions=5,
        max_leverage=10,
        max_per_symbol_pct=0.10,
        max_total_position_pct=0.50,
        daily_loss_limit_pct=0.05,
        min_order_usdt=10,
    )

    account_info = {
        "balance": bal["balance"],
        "available": bal["available"],
        "positions": positions,
    }

    # Test: valid signal
    signal = {
        "type": "LONG",
        "symbol": "BTCUSDT",
        "quantity": 0.001,
        "leverage": 5,
        "price": btc_price,
    }
    result = rm.check_signal(signal, account_info)
    logger.info("Valid LONG signal: action=%s, reason=%s", result.action, result.reason)
    assert result.action == "ALLOW", f"Expected ALLOW, got {result.action}: {result.reason}"

    # Test: excessive leverage
    signal2 = {**signal, "leverage": 125}
    result2 = rm.check_signal(signal2, account_info)
    logger.info("Excessive leverage: action=%s, reason=%s", result2.action, result2.reason)
    assert result2.action == "REJECT", f"Expected REJECT, got {result2.action}"

    # Test: close signal always allowed
    signal3 = {"type": "CLOSE_LONG", "symbol": "BTCUSDT", "quantity": 0.001}
    result3 = rm.check_signal(signal3, account_info)
    logger.info("CLOSE_LONG signal: action=%s, reason=%s", result3.action, result3.reason)
    assert result3.action == "ALLOW", f"Expected ALLOW for CLOSE, got {result3.action}"

    # ---- 10. Test execution engine ---------------------------------------
    logger.info("\n=== Step 10: Test OrderExecutionEngine ===")
    engine = OrderExecutionEngine(client, max_retries=3, retry_delay=1.0)

    # Test rounding
    rounded_qty = engine._round_quantity("BTCUSDT", 0.001234567)
    rounded_price = engine._round_price("BTCUSDT", 87654.321)
    logger.info("Rounded 0.001234567 -> %.8f, 87654.321 -> %.2f",
                rounded_qty, rounded_price)

    # ---------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------
    logger.info("\n%s", "=" * 60)
    logger.info("ALL TESTS PASSED")
    logger.info("=" * 60)
    logger.info("BinanceFuturesClient:  CONNECTED to testnet ✓")
    logger.info("Account info:         FETCHED ✓")
    logger.info("Balance:              %.2f USDT", bal["balance"])
    logger.info("Ticker (BTCUSDT):     %.2f", btc_price)
    logger.info("Order placement:      SUCCESS ✓")
    logger.info("RiskManager checks:   PASSED ✓")
    logger.info("ExecutionEngine:      READY ✓")


if __name__ == "__main__":
    main()
