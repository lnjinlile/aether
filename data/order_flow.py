"""
Order Flow data pipeline — collects trade-level data and computes
order flow metrics for volatility/magnitude prediction.

Based on arXiv:2512.15720 — "Order Flow Entropy → Predict Magnitude"
Core insight: trade direction is unpredictable, but trade *intensity*
(volume imbalance, entropy, aggressiveness) predicts volatility magnitude.

Stores aggregated features in a new 'order_flow' table in market.db.
"""

import os
import sys
import time
import sqlite3
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from scipy.stats import entropy as scipy_entropy

logger = logging.getLogger("order_flow")


class OrderFlowCollector:
    """
    Collects recent trades from Binance Futures via ccxt and computes
    order flow features at configurable aggregation windows.
    """

    # ── Binance trade classification ──────────────────────────
    # Binance's isBuyerMaker=True means the trade was initated by the seller
    # (buyer was passive / using a limit order), so it's a SELL-pressure trade.
    # isBuyerMaker=False means the buyer initiated (market buy), so BUY-pressure.

    def __init__(
        self,
        api_key: str = None,
        api_secret: str = None,
        testnet: bool = None,
        db_path: str = None,
    ):
        from config.settings import get_config
        cfg = get_config()

        self.api_key = api_key or cfg.api_key
        self.api_secret = api_secret or cfg.api_secret
        self.testnet = testnet if testnet is not None else cfg.testnet
        self.db_path = db_path or cfg.db_path

        import ccxt
        self.exchange = ccxt.binanceusdm({
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "fetchCurrencies": False,       # prevents unnecessary spot API call
                "fetchLeverageBrackets": False, # disables bracket fetch
            },
        })

        if self.testnet:
            base = "https://testnet.binancefuture.com"
            api = self.exchange.urls.get("api", {})
            for key in list(api.keys()):
                if key.startswith("fapi"):
                    api[key] = base + "/fapi/v1"
            self.exchange.fetch_leverage_brackets = lambda *a, **kw: {}
            self.exchange.fetch_leverage_tiers = lambda *a, **kw: {}

        self._init_db()

    def _get_conn(self):
        """Get a SQLite connection via centralized db module."""
        from data.db import get_market_db
        conn = get_market_db(self.db_path)
        # order_flow needs Row factory explicitly (ensured by get_market_db)
        conn.row_factory = sqlite3.Row  # already set by get_market_db, explicit for clarity
        return conn

    def _init_db(self):
        """Create order_flow table if it doesn't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_flow (
                    symbol TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    window_sec INTEGER NOT NULL,
                    buy_volume REAL NOT NULL DEFAULT 0,
                    sell_volume REAL NOT NULL DEFAULT 0,
                    buy_count INTEGER NOT NULL DEFAULT 0,
                    sell_count INTEGER NOT NULL DEFAULT 0,
                    total_trades INTEGER NOT NULL DEFAULT 0,
                    total_volume REAL NOT NULL DEFAULT 0,
                    vwap REAL,
                    volume_imbalance REAL,
                    trade_count_imbalance REAL,
                    aggressiveness_ratio REAL,
                    large_trade_count INTEGER NOT NULL DEFAULT 0,
                    large_trade_volume REAL NOT NULL DEFAULT 0,
                    entropy_trade_size REAL,
                    entropy_buy_sell REAL,
                    avg_trade_size REAL,
                    std_trade_size REAL,
                    PRIMARY KEY (symbol, window_start, window_sec)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_order_flow_sym_time
                ON order_flow(symbol, window_start)
            """)
            conn.commit()
        finally:
            conn.close()

    def fetch_recent_trades(
        self, symbol: str, limit: int = 1000
    ) -> pd.DataFrame:
        """
        Fetch recent individual trades from Binance Futures.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            limit: Maximum number of trades to fetch (max 1000 for Binance).

        Returns:
            DataFrame with columns: id, price, amount, timestamp, is_buyer_maker
        """
        raw = self.exchange.fetch_trades(symbol, limit=limit)
        if not raw:
            return pd.DataFrame(columns=["id", "price", "amount", "timestamp", "is_buyer_maker"])

        df = pd.DataFrame(raw)
        df = df[["id", "price", "amount", "timestamp", "side", "takerOrMaker"]].copy()
        df["is_buyer_maker"] = df["side"].apply(lambda s: 1 if s == "sell" else 0)
        del df["side"]
        del df["takerOrMaker"]
        return df

    def classify_trade(self, is_buyer_maker: bool) -> str:
        """
        Classify a trade as 'buy' or 'sell' based on Binance's isBuyerMaker flag.

        isBuyerMaker=True  → seller initiated → SELL pressure
        isBuyerMaker=False → buyer initiated  → BUY pressure
        """
        return "sell" if is_buyer_maker else "buy"

    def compute_window_features(
        self, trades: pd.DataFrame, window_sec: int
    ) -> List[Dict]:
        """
        Aggregate trades into fixed windows and compute order flow features.

        Args:
            trades: DataFrame with columns [id, price, amount, timestamp, is_buyer_maker].
            window_sec: Aggregation window in seconds.

        Returns:
            List of dicts, one per window, with order flow features.
        """
        if trades.empty:
            return []

        trades = trades.copy()
        trades["timestamp_ms"] = trades["timestamp"]
        trades["window_start"] = (trades["timestamp_ms"] // (window_sec * 1000)) * (window_sec * 1000)

        # Classify each trade: buy vs sell
        trades["direction"] = trades["is_buyer_maker"].apply(
            lambda x: "sell" if x else "buy"
        )
        trades["notional"] = trades["price"] * trades["amount"]

        features = []
        for window_start, group in trades.groupby("window_start"):
            buy_mask = group["direction"] == "buy"
            sell_mask = group["direction"] == "sell"

            buy_vol = group.loc[buy_mask, "amount"].sum()
            sell_vol = group.loc[sell_mask, "amount"].sum()
            buy_cnt = buy_mask.sum()
            sell_cnt = sell_mask.sum()
            total_vol = buy_vol + sell_vol
            total_cnt = len(group)

            # VWAP
            if total_vol > 0:
                vwap = (group["price"] * group["amount"]).sum() / total_vol
            else:
                vwap = None

            # Volume imbalance: +1 = all buys, -1 = all sells
            volume_imbalance = ((buy_vol - sell_vol) / total_vol) if total_vol > 0 else 0.0

            # Trade count imbalance
            trade_count_imbalance = ((buy_cnt - sell_cnt) / total_cnt) if total_cnt > 0 else 0.0

            # Aggressiveness ratio: buy-initiated volume / total volume
            aggressiveness_ratio = (buy_vol / total_vol) if total_vol > 0 else 0.5

            # Large trade detection (top decile by notional in this window)
            if total_cnt >= 10:
                threshold = group["notional"].quantile(0.9)
                large = group[group["notional"] >= threshold]
                large_cnt = len(large)
                large_vol = large["amount"].sum()
            else:
                large_cnt = 0
                large_vol = 0.0

            # Entropy of trade sizes (distributional complexity)
            if total_cnt >= 5:
                # Discretize trade sizes into 10 bins for entropy calc
                sizes = group["notional"].values
                bins = min(10, total_cnt // 2)
                if bins >= 2:
                    hist, _ = np.histogram(sizes, bins=bins, density=True)
                    hist = hist[hist > 0]
                    entropy_trade_size = scipy_entropy(hist) if len(hist) > 1 else 0.0
                else:
                    entropy_trade_size = 0.0
            else:
                entropy_trade_size = 0.0

            # Entropy of buy/sell split
            if total_cnt >= 4:
                p_buy = buy_cnt / total_cnt
                p_sell = sell_cnt / total_cnt
                probs = [p for p in [p_buy, p_sell] if p > 0]
                entropy_buy_sell = scipy_entropy(probs) if len(probs) > 1 else 0.0
            else:
                entropy_buy_sell = 0.0

            # Trade size stats
            avg_trade_size = group["notional"].mean()
            std_trade_size = group["notional"].std() if total_cnt > 1 else 0.0

            features.append({
                "symbol": str(group["symbol"].iloc[0]) if "symbol" in group.columns else "UNKNOWN",
                "window_start": int(window_start),
                "window_sec": window_sec,
                "buy_volume": float(buy_vol),
                "sell_volume": float(sell_vol),
                "buy_count": int(buy_cnt),
                "sell_count": int(sell_cnt),
                "total_trades": int(total_cnt),
                "total_volume": float(total_vol),
                "vwap": float(vwap) if vwap is not None else None,
                "volume_imbalance": float(volume_imbalance),
                "trade_count_imbalance": float(trade_count_imbalance),
                "aggressiveness_ratio": float(aggressiveness_ratio),
                "large_trade_count": int(large_cnt),
                "large_trade_volume": float(large_vol),
                "entropy_trade_size": float(entropy_trade_size),
                "entropy_buy_sell": float(entropy_buy_sell),
                "avg_trade_size": float(avg_trade_size),
                "std_trade_size": float(std_trade_size),
            })

        return features

    def save_features(self, features: List[Dict]):
        """Persist order flow features to the database."""
        if not features:
            return

        conn = self._get_conn()
        try:
            conn.executemany("""
                INSERT OR REPLACE INTO order_flow
                (symbol, window_start, window_sec, buy_volume, sell_volume,
                 buy_count, sell_count, total_trades, total_volume, vwap,
                 volume_imbalance, trade_count_imbalance, aggressiveness_ratio,
                 large_trade_count, large_trade_volume,
                 entropy_trade_size, entropy_buy_sell,
                 avg_trade_size, std_trade_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                (
                    f["symbol"], f["window_start"], f["window_sec"],
                    f["buy_volume"], f["sell_volume"],
                    f["buy_count"], f["sell_count"],
                    f["total_trades"], f["total_volume"], f["vwap"],
                    f["volume_imbalance"], f["trade_count_imbalance"],
                    f["aggressiveness_ratio"],
                    f["large_trade_count"], f["large_trade_volume"],
                    f["entropy_trade_size"], f["entropy_buy_sell"],
                    f["avg_trade_size"], f["std_trade_size"],
                )
                for f in features
            ])
            conn.commit()
        finally:
            conn.close()

    def load_features(
        self,
        symbol: str,
        window_sec: int = 60,
        start_ms: int = None,
        end_ms: int = None,
    ) -> pd.DataFrame:
        """Load order flow features for a symbol."""
        conn = self._get_conn()
        try:
            query = """
                SELECT * FROM order_flow
                WHERE symbol = ? AND window_sec = ?
            """
            params = [symbol, window_sec]

            if start_ms is not None:
                query += " AND window_start >= ?"
                params.append(start_ms)
            if end_ms is not None:
                query += " AND window_start < ?"
                params.append(end_ms)

            query += " ORDER BY window_start ASC"
            return pd.read_sql_query(query, conn, params=params)
        finally:
            conn.close()

    def collect_and_store(
        self,
        symbols: List[str],
        window_sec: int = 60,
        trade_limit: int = 1000,
    ) -> Dict[str, int]:
        """
        Main entry point: fetch trades, compute features, store.

        Args:
            symbols: List of symbols to collect (e.g. ['BTC/USDT', 'ETH/USDT']).
            window_sec: Aggregation window in seconds.
            trade_limit: Max trades to fetch per symbol.

        Returns:
            Dict mapping symbol → number of windows stored.
        """
        results = {}
        for sym in symbols:
            try:
                trades = self.fetch_recent_trades(sym, limit=trade_limit)
                if not trades.empty:
                    trades["symbol"] = sym
                    features = self.compute_window_features(trades, window_sec)
                    self.save_features(features)
                    results[sym] = len(features)
                    logger.info("Order flow: %s → %d windows stored", sym, len(features))
                else:
                    results[sym] = 0
                    logger.warning("Order flow: %s → no trades returned", sym)
            except Exception as e:
                logger.error("Order flow error for %s: %s", sym, e)
                results[sym] = -1
        return results

    def get_latest_signal(self, symbol: str, window_sec: int = 60) -> Optional[Dict]:
        """
        Get the most recent order flow window as a signal dict.

        Useful for strategies to incorporate order flow data in real-time.

        Returns:
            dict with keys: volume_imbalance, aggressiveness_ratio,
                           entropy_trade_size, large_trade_count, etc.
            None if no data exists.
        """
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT * FROM order_flow
                WHERE symbol = ? AND window_sec = ?
                ORDER BY window_start DESC
                LIMIT 1
            """, (symbol, window_sec)).fetchone()
            if row is None:
                return None
            return {
                "window_start": row["window_start"],
                "volume_imbalance": row["volume_imbalance"],
                "trade_count_imbalance": row["trade_count_imbalance"],
                "aggressiveness_ratio": row["aggressiveness_ratio"],
                "large_trade_count": row["large_trade_count"],
                "large_trade_volume": row["large_trade_volume"],
                "entropy_trade_size": row["entropy_trade_size"],
                "entropy_buy_sell": row["entropy_buy_sell"],
                "avg_trade_size": row["avg_trade_size"],
                "std_trade_size": row["std_trade_size"],
                "total_volume": row["total_volume"],
                "total_trades": row["total_trades"],
                "vwap": row["vwap"],
            }
        finally:
            conn.close()

    def get_volatility_signal(self, symbol: str, lookback_windows: int = 10, window_sec: int = 60) -> Dict:
        """
        Compute a volatility/magnitude signal from recent order flow.

        Based on arXiv:2512.15720 — order flow entropy predicts volatility magnitude.
        High entropy + high imbalance → likely large move ahead.

        Args:
            symbol: Trading symbol.
            lookback_windows: Number of recent windows to average over.
            window_sec: Aggregation window size to query.

        Returns:
            dict with 'magnitude_score' (0-1) and supporting metrics.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute("""
                SELECT entropy_trade_size, volume_imbalance, aggressiveness_ratio,
                       large_trade_volume, total_volume, avg_trade_size, std_trade_size
                FROM order_flow
                WHERE symbol = ? AND window_sec = ?
                ORDER BY window_start DESC
                LIMIT ?
            """, (symbol, window_sec, lookback_windows)).fetchall()

            if not rows:
                return {"magnitude_score": 0.0, "status": "no_data"}

            entropies = [r["entropy_trade_size"] for r in rows]
            imbalances = [abs(r["volume_imbalance"]) for r in rows]
            aggressions = [r["aggressiveness_ratio"] for r in rows]
            large_vol_ratio = [
                r["large_trade_volume"] / max(r["total_volume"], 1e-8) for r in rows
            ]

            avg_entropy = np.mean(entropies)
            avg_imbalance = np.mean(imbalances)
            avg_aggression = np.mean(aggressions)
            avg_large_ratio = np.mean(large_vol_ratio)

            # Magnitude score: weighted combination
            # Entropy-weighted: high entropy means more complex order flow → more uncertainty → bigger moves
            # Imbalance: directional conviction
            # Large trades: smart money indicator
            score = (
                0.35 * min(avg_entropy / 2.5, 1.0) +
                0.30 * avg_imbalance +
                0.20 * abs(avg_aggression - 0.5) * 2 +  # deviation from 50/50
                0.15 * avg_large_ratio
            )

            return {
                "magnitude_score": round(min(score, 1.0), 4),
                "avg_entropy": round(avg_entropy, 4),
                "avg_imbalance": round(avg_imbalance, 4),
                "avg_aggression_ratio": round(avg_aggression, 4),
                "avg_large_trade_ratio": round(avg_large_ratio, 4),
                "windows_used": len(rows),
                "status": "ok",
            }
        finally:
            conn.close()


# ── Standalone runner for integration with pipeline ──────────

def run_order_flow_pipeline():
    """
    One-shot collection of order flow data. Designed to be called
    from pipeline.py or a cron-like scheduler.

    Uses the same symbols as the main pipeline.
    """
    from config.settings import get_config
    cfg = get_config()

    collector = OrderFlowCollector(
        api_key=cfg.api_key,
        api_secret=cfg.api_secret,
        testnet=cfg.testnet,
    )

    symbols = cfg.symbols
    results = collector.collect_and_store(
        symbols=symbols,
        window_sec=60,
        trade_limit=1000,
    )
    logger.info("Order flow pipeline complete: %s", results)
    return results


if __name__ == "__main__":
    INTERVAL = 60  # collect order flow every 60s (trades are high-frequency)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [OF] %(message)s")
    logger.info("Order flow pipeline started — interval %ds", INTERVAL)
    while True:
        try:
            run_order_flow_pipeline()
            logger.info("Cycle complete")
        except Exception as e:
            logger.error("Cycle error: %s", e)
        time.sleep(INTERVAL)
