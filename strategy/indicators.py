"""
Shared technical indicators for strategy modules.

All indicators are pure functions — no external state, no database access.
Designed to work with pandas Series/DataFrame and be usable in both
live trading (per-bar) and backtesting (vectorized) contexts.
"""

import pandas as pd
import numpy as np


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI (Relative Strength Index) using Wilder's smoothing.

    ⚠️  CANONICAL METHOD — this is the single authoritative RSI implementation
    for the entire Aether system. ALL consumers (strategy engine, backtests,
    signal generators, ML features, patrol diagnostics) MUST use Wilder's EWM
    smoothing (alpha=1/period). 

    DO NOT use SMA-based RSI (rolling(window=period).mean()) — it produces
    values that diverge by 8-12 points from Wilder's method, causing false
    signal/no-signal discrepancies between system components.

    Args:
        close: Series of closing prices
        period: RSI lookback period (default 14)

    Returns:
        Series of RSI values (0-100). NaN for first <period> bars.
    """
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[avg_loss == 0] = 100.0
    rsi[avg_gain == 0] = 0.0
    return rsi


def compute_stoch_rsi(
    close: pd.Series,
    rsi_period: int = 14,
    stoch_period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> pd.DataFrame:
    """Compute Stochastic RSI.

    Returns DataFrame with columns: stoch_rsi, k, d
    """
    rsi = compute_rsi(close, rsi_period)
    rsi_min = rsi.rolling(window=stoch_period).min()
    rsi_max = rsi.rolling(window=stoch_period).max()
    stoch_rsi = (rsi - rsi_min) / (rsi_max - rsi_min).replace(0, np.nan)
    k = stoch_rsi.rolling(window=smooth_k).mean()
    d = k.rolling(window=smooth_d).mean()
    return pd.DataFrame({"stoch_rsi": stoch_rsi, "k": k, "d": d}, index=close.index)


def compute_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Compute Average True Range (ATR) using Wilder's smoothing."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr


def compute_donchian(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> pd.DataFrame:
    """Compute Donchian Channel (rolling high/low).

    Returns DataFrame with columns: upper, lower, middle
    """
    upper = high.rolling(window=period).max()
    lower = low.rolling(window=period).min()
    middle = (upper + lower) / 2
    return pd.DataFrame({"upper": upper, "lower": lower, "middle": middle}, index=high.index)


def compute_bollinger_bands(
    close: pd.Series,
    period: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """Compute Bollinger Bands.

    Returns DataFrame with columns: middle, upper, lower, bandwidth, percent_b
    """
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle
    percent_b = (close - lower) / (upper - lower)
    return pd.DataFrame(
        {"middle": middle, "upper": upper, "lower": lower,
         "bandwidth": bandwidth, "percent_b": percent_b},
        index=close.index,
    )


def compute_ema(close: pd.Series, period: int) -> pd.Series:
    """Compute Exponential Moving Average."""
    return close.ewm(span=period, adjust=False).mean()


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """Compute MACD (Moving Average Convergence Divergence).

    Returns DataFrame with columns: macd, signal, histogram
    """
    ema_fast = compute_ema(close, fast)
    ema_slow = compute_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "histogram": histogram},
        index=close.index,
    )
