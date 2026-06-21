"""
SQLite storage layer for market data.
Handles persistence of klines and trades into a local SQLite database.
"""

import os
import sqlite3
import pandas as pd


class MarketStorage:
    """
    SQLite-backed storage for klines and trade data.

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
        return sqlite3.connect(self.db_path)

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
            df.to_sql("klines", conn, if_exists="append", index=False)
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
