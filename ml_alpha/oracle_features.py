"""
Oracle Features — On-chain / market microstructure features for ML models.

Loads orderbook snapshots, funding rates, and open interest from market.db
and computes derived features that can be merged into ML feature matrices.

Features:
- OI change rate (1h, 4h, 24h)
- Funding rate level + extreme flag (top/bottom decile)
- Order book imbalance (bid/ask ratio at top 5 levels)
- Spread width (basis points)
"""

import os
import logging
from typing import Optional, Dict

import numpy as np
import pandas as pd

# Centralized DB access with WAL + busy_timeout
from data.db import get_market_db

logger = logging.getLogger(__name__)

# Default DB path relative to project root
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "market.db")


def load_orderbook_features(symbol: str, db_path: str = None) -> Optional[pd.DataFrame]:
    """Load orderbook snapshots for a symbol and compute derived features.

    Args:
        symbol: e.g. 'BTCUSDT' or 'ETHUSDT' (Binance symbol format)
        db_path: Path to SQLite DB (default: data/market.db)

    Returns:
        DataFrame indexed by timestamp with columns:
        - best_bid, best_ask, spread_pct, imbalance, bid_vol_5, ask_vol_5
        Returns None if no data.
    """
    db_path = db_path or _DEFAULT_DB
    if not os.path.exists(db_path):
        return None

    conn = get_market_db(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, best_bid, best_ask, spread_pct, imbalance, bid_vol_5, ask_vol_5 "
            "FROM orderbook_snapshots WHERE symbol = ? ORDER BY timestamp ASC",
            conn, params=(symbol,)
        )
    finally:
        conn.close()

    if df.empty:
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)

    # Derived features
    df["mid_price"] = (df["best_bid"] + df["best_ask"]) / 2.0
    df["imbalance_smoothed"] = df["imbalance"].rolling(6, min_periods=2).mean()
    df["imbalance_delta"] = df["imbalance"] - df["imbalance_smoothed"]
    df["spread_bps"] = df["spread_pct"] * 100  # convert to basis points

    return df


def load_funding_features(symbol: str, db_path: str = None) -> Optional[pd.DataFrame]:
    """Load funding rate history and compute features.

    Args:
        symbol: e.g. 'BTCUSDT' or 'ETHUSDT' (Binance symbol format)
        db_path: Path to SQLite DB

    Returns:
        DataFrame indexed by funding_time with columns:
        - funding_rate, funding_rate_pct
        - funding_extreme (1 if in top/bottom 10% of lookback, else 0)
        - funding_zscore
        Returns None if no data.
    """
    db_path = db_path or _DEFAULT_DB
    if not os.path.exists(db_path):
        return None

    conn = get_market_db(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT funding_time, funding_rate FROM funding_rates "
            "WHERE symbol = ? ORDER BY funding_time ASC",
            conn, params=(symbol,)
        )
    finally:
        conn.close()

    if df.empty:
        return None

    df["funding_time"] = pd.to_datetime(df["funding_time"], unit="ms", utc=True)
    df.set_index("funding_time", inplace=True)
    df.sort_index(inplace=True)

    df["funding_rate_pct"] = df["funding_rate"] * 100.0  # as percentage

    # Extreme flag: top/bottom 10% of rolling window
    window = min(48, len(df))  # up to 48 periods (8h of 10-min intervals)
    if window >= 5:
        df["funding_pctile"] = df["funding_rate"].rolling(window, min_periods=5).rank(pct=True)
        df["funding_extreme"] = ((df["funding_pctile"] >= 0.9) | (df["funding_pctile"] <= 0.1)).astype(int)
    else:
        df["funding_extreme"] = 0

    # Z-score of funding rate
    if len(df) >= 5:
        df["funding_zscore"] = (
            (df["funding_rate"] - df["funding_rate"].rolling(window, min_periods=5).mean())
            / df["funding_rate"].rolling(window, min_periods=5).std().replace(0, np.nan)
        )
    else:
        df["funding_zscore"] = 0.0

    return df


def load_oi_features(symbol: str, db_path: str = None) -> Optional[pd.DataFrame]:
    """Load open interest history and compute derived features.

    Args:
        symbol: e.g. 'BTCUSDT' or 'ETHUSDT'
        db_path: Path to SQLite DB

    Returns:
        DataFrame indexed by timestamp with columns:
        - open_interest
        - oi_change_1h, oi_change_4h, oi_change_24h (pct change)
        - oi_change_5m (short-term flow)
        Returns None if no data.
    """
    db_path = db_path or _DEFAULT_DB
    if not os.path.exists(db_path):
        return None

    conn = get_market_db(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT timestamp, open_interest FROM open_interest "
            "WHERE symbol = ? ORDER BY timestamp ASC",
            conn, params=(symbol,)
        )
    finally:
        conn.close()

    if df.empty:
        return None

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)

    # OI change rates at different horizons
    df["oi_change_1"] = df["open_interest"].pct_change(1)
    df["oi_change_3"] = df["open_interest"].pct_change(3)
    df["oi_change_12"] = df["open_interest"].pct_change(12)   # ~1h at 5min intervals
    df["oi_change_48"] = df["open_interest"].pct_change(48)   # ~4h at 5min intervals
    df["oi_change_288"] = df["open_interest"].pct_change(288) # ~24h at 5min intervals

    # OI acceleration (change of change)
    if len(df) >= 3:
        df["oi_accel"] = df["oi_change_1"].diff(2)

    # OI / price divergence proxy (if we had price, we'd compute correlation)
    # For now, just mark extreme OI moves
    if len(df) >= 10:
        oi_chg_std = df["oi_change_1"].rolling(20, min_periods=5).std()
        df["oi_surge"] = (abs(df["oi_change_1"]) > 2 * oi_chg_std).astype(int)
    else:
        df["oi_surge"] = 0

    return df


def load_orderflow_features(symbol: str, db_path: str = None) -> Optional[pd.DataFrame]:
    """Load order flow (trade-level) features for a symbol.

    Reads the order_flow table which contains 1-minute aggregated trade windows
    with pre-computed microstructure features.

    Args:
        symbol: e.g. 'BTCUSDT' or 'ETHUSDT' (Binance symbol format)
        db_path: Path to SQLite DB

    Returns:
        DataFrame indexed by window_start with columns:
        - volume_imbalance, trade_count_imbalance, aggressiveness_ratio
        - large_trade_ratio, entropy_trade_size, entropy_buy_sell
        - avg_trade_size, std_trade_size, total_volume
        - of_imbalance_smoothed, of_imbalance_delta, of_aggressiveness_smoothed
        Returns None if no data.
    """
    db_path = db_path or _DEFAULT_DB
    if not os.path.exists(db_path):
        return None

    # order_flow table uses "BTC/USDT" format; convert if needed (PERF-089)
    if "/" not in symbol:
        of_symbol = symbol[:3] + "/" + symbol[3:]
    else:
        of_symbol = symbol

    conn = get_market_db(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT window_start, buy_volume, sell_volume, buy_count, sell_count, "
            "total_trades, total_volume, vwap, volume_imbalance, "
            "trade_count_imbalance, aggressiveness_ratio, "
            "large_trade_count, large_trade_volume, "
            "entropy_trade_size, entropy_buy_sell, "
            "avg_trade_size, std_trade_size "
            "FROM order_flow WHERE symbol = ? ORDER BY window_start ASC",
            conn, params=(of_symbol,)
        )
    finally:
        conn.close()

    if df.empty:
        return None

    df["window_start"] = pd.to_datetime(df["window_start"], unit="ms", utc=True)
    df.set_index("window_start", inplace=True)
    df.sort_index(inplace=True)

    # Derived features
    # Large trade proportion
    total_vol = df["total_volume"].replace(0, np.nan)
    df["large_trade_ratio"] = df["large_trade_volume"] / total_vol

    # Smoothed imbalance (6-period rolling, ~6min)
    df["of_imbalance_smoothed"] = df["volume_imbalance"].rolling(6, min_periods=2).mean()
    df["of_imbalance_delta"] = df["volume_imbalance"] - df["of_imbalance_smoothed"]

    # Smoothed aggressiveness
    df["of_aggressiveness_smoothed"] = df["aggressiveness_ratio"].rolling(6, min_periods=2).mean()

    # Volume surge detection (2-sigma)
    if len(df) >= 10:
        vol_mean = df["total_volume"].rolling(20, min_periods=5).mean()
        vol_std = df["total_volume"].rolling(20, min_periods=5).std().replace(0, np.nan)
        df["of_volume_surge"] = ((df["total_volume"] - vol_mean) > 2 * vol_std).astype(int)
    else:
        df["of_volume_surge"] = 0

    # Net taker volume proxy (buy - sell volume)
    df["of_net_taker_vol"] = df["buy_volume"] - df["sell_volume"]

    return df


def _normalize_index(df: pd.DataFrame, target_idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Normalize a DataFrame's DatetimeIndex to match the target index dtype."""
    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
    # Match target timezone
    target_tz = getattr(target_idx, 'tz', None)
    if target_tz is not None and df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if df.index.tz is not None and target_tz is None:
        df.index = df.index.tz_localize(None)
    elif df.index.tz is not None and target_tz is not None:
        if str(df.index.tz) != str(target_tz):
            df.index = df.index.tz_convert(target_tz)
    # Match the exact dtype of target (e.g. datetime64[ms, UTC])
    target_dtype = str(target_idx.dtype)
    if 'ms' in target_dtype:
        if target_tz is not None:
            df.index = df.index.tz_convert(target_tz) if df.index.tz else df.index.tz_localize(target_tz)
            df.index = df.index.tz_localize(None).astype('datetime64[ms]')
            df.index = df.index.tz_localize(target_tz)
        else:
            df.index = df.index.tz_localize(None).astype('datetime64[ms]')
    elif 'ns' in target_dtype:
        if target_tz is not None:
            df.index = df.index.tz_convert(target_tz) if df.index.tz else df.index.tz_localize(target_tz)
            # already ns, just ensure correct tz
        else:
            df.index = df.index.tz_localize(None)
    else:
        df.index = pd.to_datetime(df.index)
    return df


def _safe_reindex(oracle_df: pd.DataFrame, col: str, 
                  target_idx: pd.DatetimeIndex) -> pd.Series:
    """Safely reindex an oracle column to a kline index using forward fill.
    
    Uses merge_asof for robust timestamp alignment even when oracle
    timestamps have sub-second precision and klines are at exact intervals.
    """
    if oracle_df is None or oracle_df.empty or col not in oracle_df.columns:
        return pd.Series(np.nan, index=target_idx)
    sub = oracle_df[[col]].copy()
    sub = _normalize_index(sub, target_idx)
    sub = sub.sort_index()
    
    # Build target DataFrame
    target_df = pd.DataFrame(index=target_idx.sort_values())
    target_df['_order'] = range(len(target_df))
    
    # merge_asof: for each kline timestamp, find last oracle value ≤ that timestamp
    merged = pd.merge_asof(
        target_df, sub,
        left_index=True, right_index=True,
        direction='backward'
    )
    merged = merged.sort_values('_order')
    result = merged[col]
    result.index = target_idx
    return result


def merge_oracle_features(
    kline_df: pd.DataFrame,
    symbol: str,
    db_path: str = None,
) -> pd.DataFrame:
    """Merge oracle (orderbook, funding, OI) features into a kline-aligned DataFrame.

    Takes a kline OHLCV DataFrame and returns a new DataFrame with additional
    columns for oracle-derived features, forward-filled to align with kline timestamps.

    Args:
        kline_df: OHLCV DataFrame with DatetimeIndex
        symbol: Binance symbol format (e.g. 'BTCUSDT')
        db_path: DB path

    Returns:
        DataFrame with the same index as kline_df, containing original columns
        plus oracle feature columns. Missing values are forward-filled then
        filled with 0.
    """
    result = kline_df.copy()
    target_idx = pd.DatetimeIndex(kline_df.index)

    # ── Orderbook features ──
    ob_df = load_orderbook_features(symbol, db_path)
    if ob_df is not None and not ob_df.empty:
        ob_cols = ["imbalance", "imbalance_smoothed", "imbalance_delta",
                    "spread_bps", "spread_pct"]
        for col in ob_cols:
            if col in ob_df.columns:
                result[f"ob_{col}"] = _safe_reindex(ob_df, col, target_idx)
        logger.debug("Merged orderbook features for %s: %d rows", symbol, len(ob_df))
    else:
        logger.debug("No orderbook data for %s", symbol)

    # ── Funding rate features ──
    fund_df = load_funding_features(symbol, db_path)
    if fund_df is not None and not fund_df.empty:
        fund_cols = ["funding_rate", "funding_rate_pct", "funding_extreme", "funding_zscore"]
        for col in fund_cols:
            if col in fund_df.columns:
                result[f"fund_{col}"] = _safe_reindex(fund_df, col, target_idx)
        logger.debug("Merged funding features for %s: %d rows", symbol, len(fund_df))
    else:
        logger.debug("No funding data for %s", symbol)

    # ── Open Interest features ──
    oi_df = load_oi_features(symbol, db_path)
    if oi_df is not None and not oi_df.empty:
        oi_cols = ["oi_change_1", "oi_change_3", "oi_change_12",
                    "oi_change_48", "oi_change_288", "oi_surge"]
        for col in oi_cols:
            if col in oi_df.columns:
                result[f"oi_{col}"] = _safe_reindex(oi_df, col, target_idx)
        logger.debug("Merged OI features for %s: %d rows", symbol, len(oi_df))
    else:
        logger.debug("No OI data for %s", symbol)

    # ── Order Flow features (PERF-089) ──
    of_df = load_orderflow_features(symbol, db_path)
    if of_df is not None and not of_df.empty:
        of_cols = ["volume_imbalance", "trade_count_imbalance",
                    "aggressiveness_ratio", "large_trade_ratio",
                    "entropy_trade_size", "entropy_buy_sell",
                    "avg_trade_size", "std_trade_size",
                    "of_imbalance_smoothed", "of_imbalance_delta",
                    "of_aggressiveness_smoothed", "of_volume_surge",
                    "of_net_taker_vol"]
        for col in of_cols:
            if col in of_df.columns:
                result[f"of_{col}"] = _safe_reindex(of_df, col, target_idx)
        logger.debug("Merged order flow features for %s: %d rows", symbol, len(of_df))
    else:
        logger.debug("No order flow data for %s", symbol)

    return result


def get_oracle_feature_names() -> list:
    """Return the list of oracle feature column names that may be added."""
    return [
        # Orderbook
        "ob_imbalance", "ob_imbalance_smoothed", "ob_imbalance_delta",
        "ob_spread_bps", "ob_spread_pct",
        # Funding
        "fund_funding_rate", "fund_funding_rate_pct",
        "fund_funding_extreme", "fund_funding_zscore",
        # Open Interest
        "oi_oi_change_1", "oi_oi_change_3", "oi_oi_change_12",
        "oi_oi_change_48", "oi_oi_change_288", "oi_oi_surge",
        # Order Flow (PERF-089)
        "of_volume_imbalance", "of_trade_count_imbalance",
        "of_aggressiveness_ratio", "of_large_trade_ratio",
        "of_entropy_trade_size", "of_entropy_buy_sell",
        "of_avg_trade_size", "of_std_trade_size",
        "of_of_imbalance_smoothed", "of_of_imbalance_delta",
        "of_of_aggressiveness_smoothed", "of_of_volume_surge",
        "of_of_net_taker_vol",
    ]
