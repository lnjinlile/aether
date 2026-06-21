"""
Binance Futures API Client using CCXT.

Connects to Binance Futures testnet or mainnet depending on configuration.
Handles symbol format conversion (BTCUSDT <-> BTC/USDT) internally.
"""

import logging
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

        # Populated lazily
        self._exchange_info: Optional[Dict] = None
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
        """Get current ticker for a symbol.  Returns ccxt ticker dict."""
        ccxt_symbol = self.to_ccxt_symbol(symbol)
        return self._exchange.fetch_ticker(ccxt_symbol)

    # ------------------------------------------------------------------
    # Account (private)
    # ------------------------------------------------------------------

    def get_account(self) -> Dict[str, Any]:
        """Return account info: balances and positions."""
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

        Uses direct REST API when ccxt fails on testnet.
        """
        try:
            return self._get_balance_via_ccxt()
        except Exception:
            return self._get_balance_via_rest()

    def _get_balance_via_ccxt(self) -> Dict[str, Any]:
        """Get balance using ccxt."""
        balance = self._exchange.fetch_balance()
        total = float(balance.get("total", {}).get("USDT", 0))
        free = float(balance.get("free", {}).get("USDT", 0))
        positions = self.get_positions()
        unrealized_pnl = sum(
            float(p.get("unrealizedPnl", 0) or 0) for p in positions
        )
        return {
            "balance": total + unrealized_pnl if total else unrealized_pnl,
            "available": free,
            "unrealized_pnl": unrealized_pnl,
        }

    def _get_balance_via_rest(self) -> Dict[str, Any]:
        """Get balance using direct REST API call (fallback for testnet)."""
        import hmac
        import hashlib
        import time
        import requests
        from urllib.parse import urlencode

        base = "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        endpoint = "/fapi/v2/account"
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp}
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{base}{endpoint}?{query}&signature={signature}"
        resp = requests.get(url, headers={"X-MBX-APIKEY": self.api_key}, timeout=15)
        data = resp.json()

        total_balance = 0.0
        available_balance = 0.0
        unrealized_pnl = 0.0
        for asset in data.get("assets", []):
            if asset.get("asset") == "USDT":
                total_balance = float(asset.get("walletBalance", 0))
                available_balance = float(asset.get("availableBalance", 0))
                unrealized_pnl = float(asset.get("unrealizedProfit", 0))
                break

        return {
            "balance": total_balance,
            "available": available_balance,
            "unrealized_pnl": unrealized_pnl,
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return list of open positions. Uses REST fallback on testnet."""
        try:
            return self._get_positions_via_ccxt()
        except Exception:
            return self._get_positions_via_rest()

    def _get_positions_via_ccxt(self) -> List[Dict[str, Any]]:
        """Get positions using ccxt."""
        raw = self._exchange.fetch_positions()
        positions = []
        for p in raw:
            if float(p.get("contracts", 0) or 0) != 0:
                positions.append(self._normalize_position(p))
        return positions

    def _get_positions_via_rest(self) -> List[Dict[str, Any]]:
        """Get positions using direct REST API call."""
        import hmac
        import hashlib
        import time
        import requests
        from urllib.parse import urlencode

        base = "https://testnet.binancefuture.com" if self.testnet else "https://fapi.binance.com"
        endpoint = "/fapi/v2/account"
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp}
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{base}{endpoint}?{query}&signature={signature}"
        resp = requests.get(url, headers={"X-MBX-APIKEY": self.api_key}, timeout=15)
        data = resp.json()

        positions = []
        for p in data.get("positions", []):
            if abs(float(p.get("positionAmt", 0))) > 0:
                pos_amt = float(p["positionAmt"])
                positions.append({
                    "symbol": p.get("symbol", ""),
                    "side": "long" if pos_amt > 0 else "short",
                    "contracts": abs(pos_amt),
                    "entry_price": float(p.get("entryPrice", 0)),
                    "mark_price": float(p.get("markPrice", 0)),
                    "unrealized_pnl": float(p.get("unrealizedProfit", 0)),
                    "liquidation_price": float(p.get("liquidationPrice", 0)),
                    "leverage": int(p.get("leverage", 1) or 1),
                    "margin_mode": p.get("marginType", "isolated"),
                    "notional": abs(pos_amt) * float(p.get("markPrice", 0)),
                })
        return positions

    def _normalize_position(self, p: Dict) -> Dict[str, Any]:
        """Normalize ccxt position format to standard format."""
        return {
            "symbol": p.get("symbol", ""),
            "side": p.get("side", ""),
            "contracts": float(p.get("contracts", 0) or 0),
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
        params: Dict[str, Any] = {}
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
        if price and order_type.upper() == "LIMIT":
            params["price"] = price
            params["timeInForce"] = "GTC"
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._rest_post("/fapi/v1/order", params)

    def cancel_order(self, symbol: str, order_id) -> Dict:
        """Cancel a single order by ID."""
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            return self._exchange.cancel_order(order_id, ccxt_symbol)
        except Exception:
            return self._rest_delete("/fapi/v1/order", {
                "symbol": self.to_binance_symbol(symbol),
                "orderId": order_id,
            })

    def cancel_all_orders(self, symbol: str) -> List[Dict]:
        """Cancel all open orders for a symbol."""
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol)
            return self._exchange.cancel_all_orders(ccxt_symbol)
        except Exception:
            return self._rest_delete("/fapi/v1/allOpenOrders", {
                "symbol": self.to_binance_symbol(symbol),
            })

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get all open orders."""
        try:
            ccxt_symbol = self.to_ccxt_symbol(symbol) if symbol else None
            return self._exchange.fetch_open_orders(ccxt_symbol)
        except Exception:
            params = {}
            if symbol:
                params["symbol"] = self.to_binance_symbol(symbol)
            return self._rest_get("/fapi/v1/openOrders", params)

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

    def _signed_request(self, method: str, endpoint: str, params: dict) -> dict:
        import hmac, hashlib, time, requests
        from urllib.parse import urlencode
        params = {k: v for k, v in params.items() if v is not None}
        params["timestamp"] = int(time.time() * 1000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{self._rest_base()}{endpoint}?{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": self.api_key}
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=15)
        elif method == "POST":
            resp = requests.post(url, headers=headers, timeout=15)
        elif method == "DELETE":
            resp = requests.delete(url, headers=headers, timeout=15)
        else:
            raise ValueError(f"Unsupported method: {method}")
        return resp.json()

    def _rest_get(self, endpoint: str, params: dict = None) -> dict:
        return self._signed_request("GET", endpoint, params or {})

    def _rest_post(self, endpoint: str, params: dict) -> dict:
        return self._signed_request("POST", endpoint, params)

    def _rest_delete(self, endpoint: str, params: dict) -> dict:
        return self._signed_request("DELETE", endpoint, params)
