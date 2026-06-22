"""
Binance Futures API Client using CCXT.

Connects to Binance Futures testnet or mainnet depending on configuration.
Handles symbol format conversion (BTCUSDT <-> BTC/USDT) internally.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import ccxt

logger = logging.getLogger(__name__)

# Testnet base URL for USDT-M futures
TESTNET_BASE_URL = "https://testnet.binancefuture.com"

# Version-specific API paths on testnet
# Private endpoints (except account) use v1; account uses v2
_TESTNET_API_PATHS = {
    "fapiPublic": "/fapi/v1",
    "fapiPrivate": "/fapi/v1",       # orders, leverage, margin use v1
    "fapiPublicV2": "/fapi/v2",
    "fapiPrivateV2": "/fapi/v2",     # account endpoint uses v2 (via REST fallback)
    "fapiPublicV3": "/fapi/v3",
    "fapiPrivateV3": "/fapi/v3",
    "fapiData": "/fapi/v1",
    "dapiPublic": "/dapi/v1",
    "dapiPrivate": "/dapi/v1",
    "dapiPrivateV2": "/dapi/v2",
    "dapiData": "/dapi/v1",
}


class BinanceFuturesClient:
    """Thin wrapper around ccxt.binanceusdm for Binance USDT-M Futures.

    All methods target the USDT-M futures market. Symbol conversion between
    ccxt format (BTC/USDT) and Binance native (BTCUSDT) is handled
    transparently.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet

        exchange_params: Dict[str, Any] = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                # Skip calls that testnet doesn't support
                "fetchCurrencies": False,
                "fetchLeverageBrackets": False,
            },
        }

        self._exchange = ccxt.binanceusdm(exchange_params)

        if self.testnet:
            self._configure_testnet()
            # Testnet doesn't support leverage bracket endpoints.
            # Monkey-patch to return empty data instead of erroring.
            self._exchange.fetch_leverage_brackets = lambda *a, **kw: {}
            self._exchange.fetch_leverage_tiers = lambda *a, **kw: {}
            # Sync time to avoid timestamp skew on testnet
            self._sync_time()

        # Populated lazily
        self._exchange_info: Optional[Dict] = None

        # Time offset for testnet clock drift compensation (ms)
        self._time_offset: int = 0
        self._markets: Optional[Dict] = None

    def _configure_testnet(self) -> None:
        """Override exchange URLs to point to Binance Futures testnet."""
        api_urls = self._exchange.urls.get("api", {})
        for key in list(api_urls.keys()):
            if key in _TESTNET_API_PATHS:
                api_urls[key] = TESTNET_BASE_URL + _TESTNET_API_PATHS[key]

    # ------------------------------------------------------------------
    # Symbol helpers
    # ------------------------------------------------------------------

    @staticmethod
    def to_ccxt_symbol(symbol: str) -> str:
        """Convert 'BTCUSDT' -> 'BTC/USDT'.  Pass-through if already slash-form."""
        if "/" in symbol:
            return symbol
        # Binance perps always end in USDT for USDT-M
        if symbol.endswith("USDT"):
            return symbol[:-4] + "/USDT"
        return symbol

    @staticmethod
    def to_binance_symbol(symbol: str) -> str:
        """Convert 'BTC/USDT' or 'BTC/USDT:USDT' -> 'BTCUSDT'.
        Pass-through if no slash and no colon suffix."""
        # Strip ccxt settlement suffix (":USDT")
        if ":" in symbol:
            symbol = symbol.split(":")[0]
        return symbol.replace("/", "")

    # ------------------------------------------------------------------
    # Market data (public)
    # ------------------------------------------------------------------

    def get_exchange_info(self) -> Dict[str, Any]:
        """Return symbol filters suitable for rounding quantities and prices.

        Returns a dict keyed by BINANCE symbol (e.g. 'BTCUSDT') with:
            min_qty, step_size, tick_size, min_notional
        """
        if self._exchange_info is not None:
            return self._exchange_info

        self._markets = self._exchange.load_markets()

        info: Dict[str, Any] = {}
        for mkt_symbol, mkt in self._markets.items():
            if mkt.get("type") != "swap":
                continue
            # Handle ccxt market keys like "BTC/USDT:USDT" -> "BTCUSDT"
            clean_symbol = mkt_symbol.split(":")[0]
            bin_symbol = self.to_binance_symbol(clean_symbol)
            limits = mkt.get("limits", {})
            precision = mkt.get("precision", {})
            amount = limits.get("amount", {})
            price_limits = limits.get("price", {})
            cost = limits.get("cost", {})

            info[bin_symbol] = {
                "min_qty": amount.get("min", 0),
                "step_size": precision.get("amount", 1e-8),
                "tick_size": precision.get("price", 1e-8),
                "min_notional": cost.get("min", 0),
            }

        self._exchange_info = info
        return info

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get current ticker for a symbol.  Returns ccxt ticker dict.

        PERF-020: On testnet, use REST /fapi/v1/ticker/price to avoid ccxt hang.
        """
        if self.testnet:
            return self._get_ticker_via_rest(symbol)
        ccxt_symbol = self.to_ccxt_symbol(symbol)
        return self._exchange.fetch_ticker(ccxt_symbol)

    def _get_ticker_via_rest(self, symbol: str) -> Dict[str, Any]:
        """Get ticker via public REST on testnet (ccxt hangs).  PERF-020"""
        import urllib.request, json as _json
        native = symbol.replace("/", "").replace(":USDT", "")
        url = f"{TESTNET_BASE_URL}/fapi/v1/ticker/price?symbol={native}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = _json.loads(resp.read())
            return {"last": float(data.get("price", 0)), "symbol": symbol}
        except Exception:
            return {"last": 0, "symbol": symbol}

    # ------------------------------------------------------------------
    # Account (private)
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """Return account info: balances and positions.

        PERF-002: On testnet, skip ccxt (always times out) — go straight to REST.
        """
        if self.testnet:
            acct = self._get_account_via_rest()
            return {
                "balances": {"USDT": {"free": acct["available"], "total": acct["balance"]}},
                "positions": acct["positions"],
            }
        try:
            balance_info = self._exchange.fetch_balance()
        except Exception:
            balance_info = {}
        try:
            positions = self._exchange.fetch_positions()
        except Exception:
            positions = []
        return {
            "balances": balance_info,
            "positions": positions,
        }

    def get_balance(self) -> Dict[str, Any]:
        """Return a simplified balance snapshot.

        Uses direct REST API when ccxt fails or returns zero balance on testnet.
        PERF-002: On testnet, skip ccxt (always times out) — go straight to REST.
        """
        if self.testnet:
            # ccxt fetch_balance() always times out on testnet. Skip it.
            return self._get_balance_via_rest()
        try:
            result = self._get_balance_via_ccxt()
            if result.get("balance", 0) == 0 and result.get("available", 0) == 0:
                return self._get_balance_via_rest()
            return result
        except Exception:
            return self._get_balance_via_rest()

    def _get_balance_via_ccxt(self) -> Dict[str, Any]:
        """Get balance using ccxt (single API call, no position chaining)."""
        balance = self._exchange.fetch_balance()
        total = float(balance.get("total", {}).get("USDT", 0))
        free = float(balance.get("free", {}).get("USDT", 0))
        # Note: unrealized_pnl not available from balance endpoint alone;
        # caller should merge with positions data if needed.
        return {
            "balance": total,
            "available": free,
            "unrealized_pnl": 0.0,
        }

    def _get_account_via_rest(self, timeout: int = 15) -> Dict[str, Any]:
        """Single REST call to /fapi/v2/account — returns both balance AND positions.
        
        Avoids duplicate API calls when both balance and positions are needed.
        Cached for 30s to reduce rate-limit pressure from multiple consumers.
        """
        import time
        now = time.time()
        if hasattr(self, '_account_cache') and (now - self._account_cache_ts) < 30:
            return self._account_cache

        data = self._rest_get("/fapi/v2/account", timeout=timeout)

        # Parse balance — v2 returns top-level fields, NOT an "assets" array
        total_balance = float(data.get("totalWalletBalance", 0))
        available_balance = float(data.get("availableBalance", 0))
        unrealized_pnl = float(data.get("totalUnrealizedProfit", 0))

        # Parse positions — v2 does NOT include markPrice or liquidationPrice
        positions = []
        for p in data.get("positions", []):
            if abs(float(p.get("positionAmt", 0))) > 0:
                pos_amt = float(p["positionAmt"])
                entry = float(p.get("entryPrice", 0))
                leverage = int(float(p.get("leverage", 20)))
                notional = abs(float(p.get("notional", 0)))
                # Estimate mark from notional/quantity
                mark_est = notional / abs(pos_amt) if abs(pos_amt) > 0 else entry
                # Estimate liquidation for isolated LONG: entry * (1 - 1/leverage + mmr)
                # mmr ~0.4% for BTC lowest tier
                mmr = 0.004
                liq_est = entry * (1 - 1.0 / max(leverage, 1) + mmr) if pos_amt > 0 else entry * (1 + 1.0 / max(leverage, 1) - mmr)
                positions.append({
                    "symbol": p.get("symbol", ""),
                    "side": "LONG" if pos_amt > 0 else "SHORT",
                    "quantity": abs(pos_amt),
                    "entry_price": entry,
                    "mark_price": round(mark_est, 2),
                    "liquidation_price": round(liq_est, 2),
                    "unrealized_pnl": float(p.get("unrealizedProfit", 0)),
                    "leverage": leverage,
                    "notional": notional,
                })

        self._account_cache = {
            "balance": total_balance,
            "available": available_balance,
            "unrealized_pnl": unrealized_pnl,
            "positions": positions,
        }
        self._account_cache_ts = now
        return self._account_cache

    def _get_balance_via_rest(self) -> Dict[str, Any]:
        """Get balance from cached account data (no extra API call)."""
        acct = self._get_account_via_rest()
        return {
            "balance": acct["balance"],
            "available": acct["available"],
            "unrealized_pnl": acct["unrealized_pnl"],
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return list of open positions.

        On testnet, uses /fapi/v2/positionRisk as the authoritative source
        because ccxt fetch_positions() returns markPrice=0, liquidationPrice=0,
        and leverage=1 (all bogus). v2/positionRisk returns correct values
        for all three fields in a single call.
        """
        if self.testnet:
            try:
                return self._get_positions_via_risk()
            except Exception:
                pass

        # Mainnet / fallback: ccxt with REST merge
        positions = []
        rest_acct = None
        try:
            rest_acct = self._get_account_via_rest()
        except Exception:
            pass

        try:
            positions = self._get_positions_via_ccxt()
        except Exception:
            pass

        if not positions and rest_acct:
            positions = self._get_positions_via_rest()

        # Merge: override ccxt leverage with REST v2/account leverage (authoritative)
        if positions and rest_acct:
            def _norm(s):
                return s.replace(":USDT", "").replace("/", "").upper()
            rest_positions = {_norm(p["symbol"]): p for p in rest_acct["positions"]}
            for p in positions:
                sym = _norm(p.get("symbol", ""))
                rest_p = rest_positions.get(sym)
                if rest_p:
                    p["leverage"] = rest_p.get("leverage", p["leverage"])
                    # Also fix bogus mark/liquidation from ccxt on testnet
                    if p.get("mark_price", 0) == 0 and rest_p.get("mark_price", 0) > 0:
                        p["mark_price"] = rest_p["mark_price"]
                    if p.get("liquidation_price", 0) == 0 and rest_p.get("liquidation_price", 0) > 0:
                        p["liquidation_price"] = rest_p["liquidation_price"]

        return positions

    def _get_positions_via_ccxt(self) -> List[Dict[str, Any]]:
        """Get positions using ccxt."""
        raw = self._exchange.fetch_positions()
        positions = []
        for p in raw:
            if float(p.get("contracts", 0) or 0) != 0:
                positions.append(self._normalize_position(p))
        return positions

    def _get_positions_via_rest(self) -> List[Dict[str, Any]]:
        """Get positions from cached account data (no extra API call)."""
        acct = self._get_account_via_rest()
        # Convert from normalized format back to ccxt-compatible format
        positions = []
        for p in acct["positions"]:
            qty = p["quantity"]
            positions.append({
                "symbol": p["symbol"],
                "side": p["side"].lower(),
                "contracts": qty,
                "positionAmt": qty,  # alias for Guardian scripts
                "amount": qty,       # alias
                "entry_price": p["entry_price"],
                "mark_price": p["mark_price"],
                "unrealized_pnl": p["unrealized_pnl"],
                "liquidation_price": p["liquidation_price"],
                "leverage": p["leverage"],
                "margin_mode": "isolated",
                "notional": qty * p["mark_price"],
            })
        return positions

    def _get_positions_via_risk(self, timeout: int = 15) -> List[Dict[str, Any]]:
        """Get positions via /fapi/v2/positionRisk — authoritative on testnet.

        Unlike ccxt fetch_positions() and /fapi/v2/account, this endpoint
        returns accurate markPrice, liquidationPrice, AND leverage in one call.
        On testnet this is the ONLY endpoint that returns correct values for
        all three fields.
        """
        raw = self._rest_get("/fapi/v2/positionRisk", timeout=timeout)
        positions = []
        for p in raw:
            pos_amt = float(p.get("positionAmt", 0))
            if abs(pos_amt) < 0.0001:
                continue
            mark = float(p.get("markPrice", 0))
            positions.append({
                "symbol": p.get("symbol", ""),
                "side": "long" if pos_amt > 0 else "short",
                "contracts": abs(pos_amt),
                "positionAmt": pos_amt,
                "amount": abs(pos_amt),
                "entry_price": float(p.get("entryPrice", 0)),
                "mark_price": mark,
                "markPrice": mark,  # alias
                "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                "unrealizedPnl": float(p.get("unRealizedProfit", 0)),  # alias
                "liquidation_price": float(p.get("liquidationPrice", 0)),
                "liquidationPrice": float(p.get("liquidationPrice", 0)),  # alias
                "leverage": int(float(p.get("leverage", 20))),
                "margin_mode": p.get("marginType", "isolated").lower(),
                "marginMode": p.get("marginType", "isolated").lower(),  # alias
                "notional": abs(pos_amt) * mark,
            })
        return positions

    def _normalize_position(self, p: Dict) -> Dict[str, Any]:
        """Normalize ccxt position format to standard format."""
        contracts = float(p.get("contracts", 0) or 0)
        return {
            "symbol": p.get("symbol", ""),
            "side": p.get("side", ""),
            "contracts": contracts,
            "positionAmt": contracts,  # alias for Guardian scripts
            "amount": contracts,       # alias
            "entry_price": float(p.get("entryPrice", 0) or 0),
            "mark_price": float(p.get("markPrice", 0) or 0),
            "unrealized_pnl": float(p.get("unrealizedPnl", 0) or 0),
            "liquidation_price": float(p.get("liquidationPrice", 0) or 0),
            "leverage": int(p.get("leverage", 1) or 1),
            "margin_mode": p.get("marginMode", "isolated"),
            "notional": float(p.get("notional", 0) or 0),
        }

    # ------------------------------------------------------------------
    # Account settings
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """Set leverage for a symbol. Uses REST fallback if ccxt fails."""
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            return self._exchange.set_leverage(leverage, ccxt_symbol)
        except Exception:
            return self._rest_post("/fapi/v1/leverage", {
                "symbol": self.to_binance_symbol(symbol),
                "leverage": leverage,
            })

    def set_margin_mode(self, symbol: str, mode: str = "isolated") -> Dict:
        """Set margin mode ('isolated' or 'cross') for a symbol."""
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            return self._exchange.set_margin_mode(mode, ccxt_symbol)
        except Exception:
            return self._rest_post("/fapi/v1/marginType", {
                "symbol": self.to_binance_symbol(symbol),
                "marginType": mode.upper(),
            })

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "market",
        quantity: float = 0,
        price: Optional[float] = None,
        reduce_only: bool = False,
        **kwargs,
    ) -> Dict[str, Any]:
        """Place an order. Uses REST fallback if ccxt fails."""
        try:
            return self._place_order_via_ccxt(symbol, side, order_type, quantity, price, reduce_only, **kwargs)
        except Exception:
            return self._place_order_via_rest(symbol, side, order_type, quantity, price, reduce_only)

    def _place_order_via_ccxt(self, symbol, side, order_type, quantity, price, reduce_only, **kwargs):
        ccxt_symbol = self.to_ccxt_symbol(symbol)
        params: Dict[str, Any] = dict(kwargs) if kwargs else {}
        if reduce_only:
            params["reduceOnly"] = True
        return self._exchange.create_order(ccxt_symbol, order_type, side, quantity, price, params)

    def _place_order_via_rest(self, symbol, side, order_type, quantity, price, reduce_only):
        params = {
            "symbol": self.to_binance_symbol(symbol),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
        }
        # STOP/STOP_MARKET orders MUST use ccxt algo endpoint on testnet;
        # REST /fapi/v1/order with type=STOP_MARKET returns -4120 on testnet.
        # Block early to prevent silent fallback to MARKET order.
        if order_type.upper() in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            raise RuntimeError(
                f"STOP/STOP_MARKET orders cannot use REST fallback on testnet "
                f"(Binance error -4120). Use client.place_sl_order() instead."
            )
        if price and order_type.upper() == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._rest_post("/fapi/v1/order", params)

    def place_sl_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        working_type: str = "MARK_PRICE",
    ) -> Dict[str, Any]:
        """Place a STOP loss order via ccxt algo endpoint.

        On testnet, STOP/STOP_MARKET orders MUST go through the algo order
        endpoint, which ccxt handles automatically. Direct REST calls to
        /fapi/v1/order with type=STOP_MARKET fail with -4120 on testnet.

        Args:
            symbol: e.g. "BTCUSDT"
            side: "BUY" or "SELL"
            quantity: contract quantity
            stop_price: trigger price
            working_type: "MARK_PRICE" or "CONTRACT_PRICE"
        """
        ccxt_symbol = self.to_ccxt_symbol(symbol)
        params = {
            "stopPrice": stop_price,
            "reduceOnly": True,
            "workingType": working_type,
        }
        # Use STOP type (not STOP_MARKET) — ccxt routes this to the algo
        # endpoint automatically. We set a limit price 0.1% below trigger
        # so it acts like a market stop.
        limit_price = round(stop_price * 0.999, 1) if side.upper() == "SELL" else round(stop_price * 1.001, 1)
        return self._exchange.create_order(
            ccxt_symbol, "STOP", side.lower(), quantity, limit_price, params
        )

    def cancel_order(self, symbol: str, order_id) -> Dict:
        """Cancel a single order by ID.

        Tries three strategies in order:
        1. ccxt (handles both standard and algo orders)
        2. REST standard endpoint (/fapi/v1/order)
        3. REST algo endpoint (/fapi/v1/algoOrder) — for STOP/TAKE_PROFIT
           orders that only exist as conditional algo orders on testnet.
        """
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            return self._exchange.cancel_order(order_id, ccxt_symbol)
        except Exception:
            pass

        bin_symbol = self.to_binance_symbol(symbol)

        # Try standard cancel first
        result = self._rest_delete("/fapi/v1/order", {
            "symbol": bin_symbol,
            "orderId": order_id,
        })
        code = result.get("code", 0) if isinstance(result, dict) else 0
        if code == 0:
            return result

        # Standard cancel failed — try algo cancel
        # STOP/STOP_MARKET orders on testnet are conditional algo orders
        # and must be cancelled via the algo endpoint.
        return self._rest_delete("/fapi/v1/algoOrder", {
            "symbol": bin_symbol,
            "algoId": order_id,
        })

    def cancel_all_orders(self, symbol: str) -> List[Dict]:
        """Cancel all open orders for a symbol, including algo orders."""
        results = []
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            results.extend(self._exchange.cancel_all_orders(ccxt_symbol) or [])
        except Exception:
            pass

        bin_symbol = self.to_binance_symbol(symbol)

        # Cancel standard orders
        std_result = self._rest_delete("/fapi/v1/allOpenOrders", {
            "symbol": bin_symbol,
        })
        if isinstance(std_result, list):
            results.extend(std_result)

        # Cancel algo orders (STOP/TAKE_PROFIT on testnet)
        algo_result = self._rest_delete("/fapi/v1/allAlgoOrders", {
            "symbol": bin_symbol,
        })
        if isinstance(algo_result, list):
            results.extend(algo_result)

        return results

    def get_live_snapshot(self):
        """Return (balance, positions, open_orders, tickers) in minimal API calls.

        PERF-010: Parallelizes 4 independent REST calls via ThreadPoolExecutor.
        Each call has 5s timeout — parallel execution reduces 18s→~5s on testnet.
        Each step is independently fault-tolerant — one timeout won't kill the rest.
        """
        results = {}

        def _fetch_balance():
            try:
                acct = self._get_account_via_rest(timeout=5)
                return {"balance": acct["balance"], "available": acct["available"],
                        "unrealized_pnl": acct["unrealized_pnl"]}
            except Exception:
                return {"balance": 0, "available": 0, "unrealized_pnl": 0.0}

        def _fetch_positions():
            try:
                if self.testnet:
                    return self._get_positions_via_risk(timeout=5)
                return []
            except Exception:
                return []

        def _fetch_orders():
            try:
                return self.get_open_orders(timeout=5)
            except Exception:
                return []

        def _fetch_tickers():
            tickers = {}
            try:
                raw = self._rest_get("/fapi/v1/ticker/bookTicker", timeout=5)
                if isinstance(raw, list):
                    for t in raw:
                        sym = t.get("symbol", "")
                        if sym == "BTCUSDT":
                            tickers["BTC/USDT"] = float(t.get("bidPrice", 0))
                        elif sym == "ETHUSDT":
                            tickers["ETH/USDT"] = float(t.get("bidPrice", 0))
            except Exception:
                pass
            return tickers

        # PERF-016: ThreadPoolExecutor is fragile during interpreter shutdown.
        # When atexit handlers fire (e.g. ccxt cleanup), submit() raises
        # RuntimeError("cannot schedule new futures after interpreter shutdown").
        # Catch at both submit() and shutdown() levels; fall back to sequential.
        try:
            pool = ThreadPoolExecutor(max_workers=4)
        except RuntimeError:
            results["balance"] = _fetch_balance()
            results["positions"] = _fetch_positions()
            results["orders"] = _fetch_orders()
            results["tickers"] = _fetch_tickers()
        else:
            try:
                try:
                    futures = {
                        pool.submit(_fetch_balance): "balance",
                        pool.submit(_fetch_positions): "positions",
                        pool.submit(_fetch_orders): "orders",
                        pool.submit(_fetch_tickers): "tickers",
                    }
                except RuntimeError:
                    # Interpreter shutting down mid-submit – fall back to sequential
                    futures = None
                if futures:
                    for future in as_completed(futures):
                        key = futures[future]
                        try:
                            results[key] = future.result()
                        except Exception:
                            results[key] = [] if key in ("positions", "orders") else (
                                {} if key == "tickers" else {"balance": 0, "available": 0, "unrealized_pnl": 0.0})
                else:
                    results["balance"] = _fetch_balance()
                    results["positions"] = _fetch_positions()
                    results["orders"] = _fetch_orders()
                    results["tickers"] = _fetch_tickers()
            finally:
                try:
                    pool.shutdown(wait=False)
                except RuntimeError:
                    pass  # interpreter shutting down

        bal = results.get("balance", {"balance": 0, "available": 0, "unrealized_pnl": 0.0})
        positions = results.get("positions", [])
        orders = results.get("orders", [])
        tickers = results.get("tickers", {})

        # Fallback: individual ticker calls for any missing symbols (mainnet only)
        if not self.testnet:
            for sym in ["BTC/USDT", "ETH/USDT"]:
                if sym not in tickers:
                    try:
                        tickers[sym] = self.get_ticker(sym).get("last", 0)
                    except Exception:
                        tickers[sym] = 0

        return bal, positions, orders, tickers

    def get_open_orders(self, symbol: Optional[str] = None, timeout: int = 15) -> List[Dict]:
        """Get all open orders, including algo orders (STOP/TAKE_PROFIT).

        Merges ccxt standard orders + REST algo orders (testnet-safe).
        Falls back to REST openOrders when ccxt returns empty (common on testnet).
        PERF-003: On testnet, skip ccxt fetch_open_orders (always times out).
        PERF-010: Parallelizes openOrders + openAlgoOrders REST calls on testnet.
        """
        orders = []
        # Standard orders via ccxt (skip on testnet — always times out)
        if not self.testnet:
            try:
                ccxt_symbol = self.to_ccxt_symbol(symbol) if symbol else None
                ccxt_orders = self._exchange.fetch_open_orders(ccxt_symbol) or []
                orders.extend(ccxt_orders)
            except Exception:
                pass

        # On testnet: parallelize openOrders + openAlgoOrders REST calls
        if self.testnet and len(orders) == 0:
            params = {}
            if symbol:
                params["symbol"] = self.to_binance_symbol(symbol)

            def _fetch_open():
                try:
                    raw = self._rest_get("/fapi/v1/openOrders", params, timeout=timeout)
                    return raw if isinstance(raw, list) else []
                except Exception:
                    return []

            def _fetch_algos():
                try:
                    raw = self._rest_get("/fapi/v1/openAlgoOrders", params, timeout=timeout)
                    return raw if isinstance(raw, list) else []
                except Exception:
                    return []

            # PERF-016: Guard ThreadPoolExecutor against interpreter-shutdown RuntimeError.
            try:
                pool = ThreadPoolExecutor(max_workers=2)
            except RuntimeError:
                # Fallback: sequential execution
                try:
                    orders.extend(_fetch_open())
                except Exception:
                    pass
                try:
                    orders.extend(_fetch_algos())
                except Exception:
                    pass
            else:
                try:
                    try:
                        f_open = pool.submit(_fetch_open)
                        f_algos = pool.submit(_fetch_algos)
                    except RuntimeError:
                        # Interpreter shutting down – sequential fallback
                        try:
                            orders.extend(_fetch_open())
                        except Exception:
                            pass
                        try:
                            orders.extend(_fetch_algos())
                        except Exception:
                            pass
                        f_open = f_algos = None
                    if f_open is not None and f_algos is not None:
                        try:
                            orders.extend(f_open.result())
                        except Exception:
                            pass
                        try:
                            orders.extend(f_algos.result())
                        except Exception:
                            pass
                finally:
                    try:
                        pool.shutdown(wait=False)
                    except RuntimeError:
                        pass  # interpreter shutting down
        else:
            # Fallback: REST /fapi/v1/openOrders (ccxt may return empty on testnet)
            ccxt_count = len(orders)
            if ccxt_count == 0:
                try:
                    params = {}
                    if symbol:
                        params["symbol"] = self.to_binance_symbol(symbol)
                    rest_orders = self._rest_get("/fapi/v1/openOrders", params, timeout=timeout)
                    if isinstance(rest_orders, list):
                        orders.extend(rest_orders)
                except Exception:
                    pass

            # Algo orders (STOP/TAKE_PROFIT on testnet) — REST-only, ccxt doesn't cover these
            try:
                params = {}
                if symbol:
                    params["symbol"] = self.to_binance_symbol(symbol)
                algos = self._rest_get("/fapi/v1/openAlgoOrders", params, timeout=timeout)
                if isinstance(algos, list):
                    orders.extend(algos)
            except Exception:
                pass

        return orders

    def get_order(self, symbol: str, order_id) -> Dict:
        """Fetch a single order by ID."""
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            return self._exchange.fetch_order(order_id, ccxt_symbol)
        except Exception:
            return self._rest_get("/fapi/v1/order", {
                "symbol": self.to_binance_symbol(symbol),
                "orderId": order_id,
            })

    # ------------------------------------------------------------------
    # REST helpers (for testnet fallback)
    # ------------------------------------------------------------------

    def _rest_base(self) -> str:
        return "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"

    def _sync_time(self) -> None:
        """Fetch server time and compute offset to compensate for clock drift."""
        import time as _time
        import requests as req
        try:
            resp = req.get(f"{self._rest_base()}/fapi/v1/time", timeout=5)
            server_time = resp.json().get("serverTime", 0)
            if server_time:
                local_time = int(_time.time() * 1000)
                self._time_offset = server_time - local_time
                logger.debug("Time synced: offset=%dms (server=%d local=%d)",
                             self._time_offset, server_time, local_time)
        except Exception:
            pass  # keep existing offset if sync fails

    def _signed_request(self, method: str, endpoint: str, params: dict, retries: int = 3, timeout: int = 15) -> dict:
        import hmac, hashlib, time, requests
        from urllib.parse import urlencode
        params = {k: v for k, v in params.items() if v is not None}
        
        for attempt in range(retries):
            params["timestamp"] = int(time.time() * 1000) + self._time_offset
            query = urlencode(params)
            signature = hmac.new(
                self.api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            url = f"{self._rest_base()}{endpoint}?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": self.api_key}
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=timeout)
            elif method == "POST":
                resp = requests.post(url, headers=headers, timeout=timeout)
            elif method == "DELETE":
                resp = requests.delete(url, headers=headers, timeout=15)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            data = resp.json()
            code = data.get("code", 0) if isinstance(data, dict) else 0
            
            # Rate limit or IP ban — wait and retry
            if code in (-1003, -1015):
                wait = 2 ** attempt  # 1s, 2s, 4s
                logger.warning("REST %s %s → %s (attempt %d/%d, waiting %ds)",
                               method, endpoint, data.get("msg", code), attempt + 1, retries, wait)
                time.sleep(wait)
                continue
            
            # Timestamp skew — sync time and retry
            if code == -1021:
                logger.warning("REST %s %s → timestamp skew, syncing time", method, endpoint)
                self._sync_time()
                continue
            
            return data
        
        # All retries exhausted
        logger.error("REST %s %s → failed after %d attempts", method, endpoint, retries)
        return {"code": -1003, "msg": "Rate limited after retries"}

    def _rest_get(self, endpoint: str, params: dict = None, timeout: int = 15) -> dict:
        return self._signed_request("GET", endpoint, params or {}, timeout=timeout)

    def _rest_post(self, endpoint: str, params: dict, timeout: int = 15) -> dict:
        return self._signed_request("POST", endpoint, params, timeout=timeout)

    def _rest_delete(self, endpoint: str, params: dict) -> dict:
        return self._signed_request("DELETE", endpoint, params)
