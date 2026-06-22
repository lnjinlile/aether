"""
SQLite storage layer for market data and trade logging.
Handles persistence of klines, trades, and trade logs into a local SQLite database.
"""

import os
import sqlite3
import time
from typing import Dict, List, Optional

import pandas as pd


class MarketStorage:
    """
    SQLite-backed storage for klines, trade data, and trade logging.

    Database location: <project_root>/data/market.db
    """

    def __init__(self, db_path: str = None):
        """
        Initialize storage with optional custom db_path.

        Args:
            db_path: Path to SQLite database file. If None, defaults to
                     PROJECT_ROOT/data/market.db (uses settings).
        """
        if db_path is None:
            from config.settings import get_config
            cfg = get_config()
            db_path = cfg.db_path

        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        """Create and return a new SQLite connection."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=10000")  # 10s timeout for concurrent access
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        """Create tables if they do not exist."""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS klines (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    open_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    quote_volume REAL NOT NULL,
                    trades_count INTEGER NOT NULL,
                    PRIMARY KEY (symbol, timeframe, open_time)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    symbol TEXT NOT NULL,
                    trade_id INTEGER NOT NULL,
                    price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    time INTEGER NOT NULL,
                    is_buyer_maker INTEGER NOT NULL,
                    PRIMARY KEY (symbol, trade_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG', 'SHORT')),
                    entry_time REAL NOT NULL,
                    exit_time REAL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    quantity REAL NOT NULL,
                    pnl REAL DEFAULT 0.0,
                    pnl_pct REAL DEFAULT 0.0,
                    fee REAL DEFAULT 0.0,
                    strategy_name TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_log_symbol
                ON trades_log(symbol)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_log_status
                ON trades_log(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_log_strategy
                ON trades_log(strategy_name)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orderbook (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    best_bid REAL, best_ask REAL,
                    bid_volume_5 REAL, ask_volume_5 REAL,
                    spread_pct REAL,
                    imbalance REAL,
                    created_at REAL DEFAULT (strftime('%s','now'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS funding_rates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    funding_time REAL NOT NULL,
                    funding_rate REAL NOT NULL,
                    mark_price REAL,
                    UNIQUE(symbol, funding_time)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # Klines
    # ──────────────────────────────────────────────

    def save_klines(self, df: pd.DataFrame, symbol: str, timeframe: str):
        """
        Save kline data to the database.

        Expected DataFrame columns:
            open_time, open, high, low, close, volume, quote_volume, trades_count

        Args:
            df: DataFrame with kline data.
            symbol: Trading symbol (e.g. 'BTC/USDT').
            timeframe: Kline interval (e.g. '1h', '1m').
        """
        if df.empty:
            return

        df = df.copy()
        df["symbol"] = symbol
        df["timeframe"] = timeframe

        conn = self._get_conn()
        try:
            # Use INSERT OR REPLACE to handle duplicate (symbol, timeframe, open_time)
            rows = df.to_dict(orient="records")
            conn.executemany(
                """INSERT OR REPLACE INTO klines
                   (symbol, timeframe, open_time, open, high, low, close, volume, quote_volume, trades_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(r["symbol"], r["timeframe"], r["open_time"],
                  r["open"], r["high"], r["low"], r["close"],
                  r["volume"], r.get("quote_volume", 0), r.get("trades_count", 0))
                 for r in rows]
            )
            conn.commit()

            # Auto-prune old klines (skip when keep_days >= actual data age to avoid
            # pointless index scan — e.g. pipeline collects 365d and keep_days=365)
            if timeframe == "1m":
                keep_days = 90
                self._prune_old_klines_conn(conn, symbol, timeframe, keep_days)
            # All other timeframes (15m/1h/4h/1d): keep 365d, pipeline only
            # collects 365d → no pruning needed. Skip to save index scan.
        finally:
            conn.close()

    def load_klines(
        self,
        symbol: str,
        timeframe: str,
        start: int = None,
        end: int = None
    ) -> pd.DataFrame:
        """
        Load kline data from the database.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            timeframe: Kline interval (e.g. '1h').
            start: Start timestamp in milliseconds (inclusive).
            end: End timestamp in milliseconds (exclusive).

        Returns:
            DataFrame with kline data sorted by open_time.
        """
        conn = self._get_conn()
        try:
            query = """
                SELECT open_time, open, high, low, close, volume, quote_volume, trades_count
                FROM klines
                WHERE symbol = ? AND timeframe = ?
            """
            params = [symbol, timeframe]

            if start is not None:
                query += " AND open_time >= ?"
                params.append(start)
            if end is not None:
                query += " AND open_time < ?"
                params.append(end)

            query += " ORDER BY open_time ASC"
            df = pd.read_sql_query(query, conn, params=params)
            return df
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # Trades
    # ──────────────────────────────────────────────

    def save_trades(self, trades_list: list):
        """
        Save trade records to the database.

        Args:
            trades_list: List of dicts, each with keys:
                symbol, trade_id, price, quantity, time, is_buyer_maker
        """
        if not trades_list:
            return

        df = pd.DataFrame(trades_list)
        conn = self._get_conn()
        try:
            df.to_sql("trades", conn, if_exists="append", index=False)
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # Trade Logging (trades_log table)
    # ──────────────────────────────────────────────

    def log_trade(self, trade_dict: dict) -> int:
        """
        Log a new trade entry (open position).

        Args:
            trade_dict: dict with keys:
                symbol, side, entry_price, quantity, strategy_name (required)
                entry_time, reason, fee, pnl, pnl_pct (optional)
                status is set to 'OPEN' automatically.

        Returns:
            The id of the newly inserted trade log row.
        """
        required = ("symbol", "side", "entry_price", "quantity", "strategy_name")
        for key in required:
            if key not in trade_dict:
                raise ValueError(f"Missing required field: {key}")

        conn = self._get_conn()
        try:
            entry_time = trade_dict.get("entry_time", time.time())
            reason = trade_dict.get("reason", "")
            fee = trade_dict.get("fee", 0.0)
            pnl = trade_dict.get("pnl", 0.0)
            pnl_pct = trade_dict.get("pnl_pct", 0.0)

            cursor = conn.execute("""
                INSERT INTO trades_log
                    (symbol, side, entry_time, entry_price, quantity,
                     strategy_name, reason, fee, pnl, pnl_pct, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
            """, (
                trade_dict["symbol"],
                trade_dict["side"],
                entry_time,
                trade_dict["entry_price"],
                trade_dict["quantity"],
                trade_dict["strategy_name"],
                reason,
                fee,
                pnl,
                pnl_pct,
            ))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def update_trade_close(
        self, trade_id: int, exit_price: float,
        exit_time: float = None, pnl: float = None,
        pnl_pct: float = None, fee: float = None
    ):
        """
        Mark a trade as CLOSED with exit details.

        Args:
            trade_id: The id of the trade to close.
            exit_price: The price at which the position was closed.
            exit_time: Timestamp of exit (defaults to now).
            pnl: Profit/loss in quote currency.
            pnl_pct: Profit/loss as percentage.
            fee: Total fee for this trade.
        """
        if exit_time is None:
            exit_time = time.time()

        conn = self._get_conn()
        try:
            updates = ["exit_price = ?", "exit_time = ?", "status = 'CLOSED'"]
            params = [exit_price, exit_time]

            if pnl is not None:
                updates.append("pnl = ?")
                params.append(pnl)
            if pnl_pct is not None:
                updates.append("pnl_pct = ?")
                params.append(pnl_pct)
            if fee is not None:
                updates.append("fee = ?")
                params.append(fee)

            params.append(trade_id)
            conn.execute(
                f"UPDATE trades_log SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def get_trade_history(
        self, symbol: str = None, limit: int = 50
    ) -> List[Dict]:
        """
        Retrieve trade history, optionally filtered by symbol.

        Args:
            symbol: Filter by symbol (e.g. 'BTCUSDT'). None for all.
            limit: Maximum number of rows to return (default 50).

        Returns:
            List of trade dicts, ordered by id DESC.
        """
        conn = self._get_conn()
        try:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM trades_log WHERE symbol = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trades_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_open_trades(self) -> List[Dict]:
        """
        Retrieve all currently open trades.

        Returns:
            List of trade dicts with status='OPEN'.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM trades_log WHERE status = 'OPEN' ORDER BY id DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # Orderbook
    # ──────────────────────────────────────────────

    def save_orderbook(self, symbol: str, data: dict):
        """
        Save a single order book snapshot with derived metrics.

        Args:
            symbol: Trading symbol (e.g. 'BTC/USDT').
            data: dict from collector.fetch_orderbook() with keys:
                  bids, asks, timestamp.
        """
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        ts = data.get("timestamp", time.time() * 1000)

        best_bid = float(bids[0][0]) if bids else None
        best_ask = float(asks[0][0]) if asks else None

        # Top-5 volumes
        bid_vol_5 = sum(float(b[1]) for b in bids[:5]) if bids else 0.0
        ask_vol_5 = sum(float(a[1]) for a in asks[:5]) if asks else 0.0

        # Spread percentage
        if best_bid and best_ask:
            spread_pct = round((best_ask - best_bid) / best_bid * 100, 6)
        else:
            spread_pct = None

        # Imbalance: bid_vol / (bid_vol + ask_vol)
        total_vol = bid_vol_5 + ask_vol_5
        imbalance = round(bid_vol_5 / total_vol, 6) if total_vol > 0 else None

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO orderbook
                   (symbol, timestamp, best_bid, best_ask,
                    bid_volume_5, ask_volume_5, spread_pct, imbalance)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, ts, best_bid, best_ask,
                 bid_vol_5, ask_vol_5, spread_pct, imbalance),
            )
            conn.commit()
        finally:
            conn.close()

    def get_latest_orderbook(self, symbol: str) -> Optional[Dict]:
        """
        Get the most recent order book snapshot for a symbol.

        Args:
            symbol: Trading symbol.

        Returns:
            Dict with orderbook fields or None if no data.
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM orderbook
                   WHERE symbol = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # Funding Rates
    # ──────────────────────────────────────────────

    def save_funding_rates(self, symbol: str, rates_list: list):
        """
        Save funding rate records. Duplicates are skipped via UNIQUE constraint.

        Args:
            symbol: Trading symbol.
            rates_list: List of dicts from collector.fetch_funding_rate(),
                        each with fundingTime, fundingRate, markPrice.
        """
        if not rates_list:
            return

        conn = self._get_conn()
        try:
            # Batch insert with executemany (was per-row loop — ~10x faster)
            rows = []
            for r in rates_list:
                try:
                    rows.append((
                        symbol,
                        r["fundingTime"],
                        r["fundingRate"],
                        r["markPrice"],
                    ))
                except Exception:
                    pass  # skip malformed records
            if rows:
                conn.executemany(
                    """INSERT OR IGNORE INTO funding_rates
                       (symbol, funding_time, funding_rate, mark_price)
                       VALUES (?, ?, ?, ?)""",
                    rows,
                )
            conn.commit()
        finally:
            conn.close()

    def get_funding_history(
        self, symbol: str, limit: int = 100
    ) -> List[Dict]:
        """
        Get funding rate history for a symbol.

        Args:
            symbol: Trading symbol.
            limit: Maximum number of records to return.

        Returns:
            List of funding rate dicts, newest first.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM funding_rates
                   WHERE symbol = ?
                   ORDER BY funding_time DESC LIMIT ?""",
                (symbol, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # Database Maintenance
    # ──────────────────────────────────────────────

    def vacuum(self):
        """Reclaim unused space in the SQLite database."""
        conn = self._get_conn()
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    def get_db_stats(self) -> Dict:
        """
        Get database statistics including table sizes and row counts.

        Returns:
            dict with keys: db_size_bytes, db_size_mb, tables (dict of table->rows).
        """
        conn = self._get_conn()
        try:
            # Database file size
            db_size_bytes = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            db_size_mb = round(db_size_bytes / (1024 * 1024), 2)

            # Row counts per table
            tables = {}
            for table in ("klines", "trades", "trades_log"):
                row = conn.execute(
                    f"SELECT COUNT(*) as cnt FROM {table}"
                ).fetchone()
                tables[table] = row["cnt"] if row else 0

            return {
                "db_size_bytes": db_size_bytes,
                "db_size_mb": db_size_mb,
                "tables": tables,
            }
        finally:
            conn.close()

    def prune_old_klines(
        self, symbol: str, timeframe: str, keep_days: int = 90
    ) -> int:
        """
        Delete klines older than keep_days.

        Args:
            symbol: Trading symbol.
            timeframe: Kline interval.
            keep_days: Number of days of data to retain.

        Returns:
            Number of rows deleted.
        """
        conn = self._get_conn()
        try:
            return self._prune_old_klines_conn(conn, symbol, timeframe, keep_days)
        finally:
            conn.close()

    def _prune_old_klines_conn(
        self, conn: sqlite3.Connection,
        symbol: str, timeframe: str, keep_days: int
    ) -> int:
        """Internal: prune old klines using an existing connection."""
        cutoff_ms = int((time.time() - keep_days * 86400) * 1000)
        cursor = conn.execute(
            "DELETE FROM klines WHERE symbol = ? AND timeframe = ? AND open_time < ?",
            (symbol, timeframe, cutoff_ms),
        )
        conn.commit()
        return cursor.rowcount
