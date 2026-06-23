"""
Shared database connection helper for Aether system.

Provides a single entry point for SQLite connections to market.db,
ensuring consistent PRAGMA settings (WAL mode, busy_timeout) across
all modules. Eliminates copy-paste of sqlite3.connect() + PRAGMA setup.

Usage:
    from data.db import get_market_db
    with get_market_db() as db:
        db.execute("SELECT ...")

Or for multi-statement work:
    db = get_market_db()
    try:
        ...
    finally:
        db.close()
"""

import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

# Resolve project root relative to this file
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "market.db")


def get_market_db_path() -> str:
    """Return the default market.db path."""
    return _DEFAULT_DB_PATH


def _open_conn(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with standard PRAGMA settings."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")  # 10s timeout for concurrent access
    conn.row_factory = sqlite3.Row
    return conn


def get_market_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Get a configured SQLite connection to market.db.

    Args:
        db_path: Path to the database. Defaults to PROJECT_ROOT/data/market.db.

    Returns:
        sqlite3.Connection with WAL mode, busy_timeout=10s, and Row factory.
    """
    path = db_path or _DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return _open_conn(path)


@contextmanager
def market_db(db_path: Optional[str] = None):
    """Context manager for market.db connections. Auto-closes on exit.

    Usage:
        with market_db() as db:
            rows = db.execute("SELECT ...").fetchall()
    """
    conn = get_market_db(db_path)
    try:
        yield conn
    finally:
        conn.close()


def wal_checkpoint(db_path: Optional[str] = None, truncate: bool = True) -> tuple:
    """Run WAL checkpoint on market.db, optionally truncating the WAL file.

    Args:
        db_path: Database path (defaults to market.db).
        truncate: If True, use TRUNCATE mode to reset WAL to zero bytes.

    Returns:
        (busy_before, busy_after) page counts.
    """
    path = db_path or _DEFAULT_DB_PATH
    conn = _open_conn(path)
    try:
        before = conn.execute("PRAGMA wal_checkpoint").fetchone()
        if truncate:
            after = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        else:
            after = before
        return before[0] if before else 0, after[0] if after else 0
    finally:
        conn.close()


def cleanup_old_data(db_path: Optional[str] = None, retention_days: int = 30) -> dict:
    """Remove stale auxiliary data and reclaim disk space.

    Cleans orderbook_snapshots, open_interest, and order_flow older than
    *retention_days*.  Preserves klines, funding_rates, trades_log
    (historical reference data).

    Args:
        db_path: Database path (defaults to market.db).
        retention_days: Max age in days for auxiliary data (default 30).

    Returns:
        Dict with {table: rows_deleted} stats and vacuum info.
    """
    import time
    path = db_path or _DEFAULT_DB_PATH
    cutoff = time.time() - retention_days * 86400.0

    conn = _open_conn(path)
    stats = {}
    try:
        # orderbook_snapshots (timestamp is REAL = Unix seconds)
        cur = conn.execute(
            "DELETE FROM orderbook_snapshots WHERE timestamp < ?", (cutoff,)
        )
        stats["orderbook_snapshots"] = cur.rowcount

        # open_interest (timestamp is REAL = Unix seconds)
        cur = conn.execute(
            "DELETE FROM open_interest WHERE timestamp < ?", (cutoff,)
        )
        stats["open_interest"] = cur.rowcount

        # order_flow (window_start is INTEGER = Unix ms)
        cur = conn.execute(
            "DELETE FROM order_flow WHERE window_start < ?", (int(cutoff * 1000),)
        )
        stats["order_flow"] = cur.rowcount

        conn.commit()

        # Vacuum if fragmentation is significant
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        frag_pct = freelist / max(page_count, 1) * 100
        if frag_pct > 20:
            conn.execute("PRAGMA vacuum")
            stats["vacuum"] = f"triggered at {frag_pct:.1f}% fragmentation"
        else:
            stats["vacuum"] = f"skipped ({frag_pct:.1f}% fragmentation)"

        # Truncate WAL
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    return stats
