"""Feature engineering for ML-based Alpha strategy mining.

Computes 20+ features from OHLCV data and produces binary classification
targets (next-bar return direction).
"""

import numpy as np
import pandas as pd


class FeatureEngineer:
    """Engineer features for ML-based alpha signal generation.

    Takes an OHLCV DataFrame and produces a feature matrix X and target
    vector y for binary classification (next bar up=1, down=0).
    """

    def __init__(self):
        pass

    def build_features(self, df: pd.DataFrame, oracle_df: pd.DataFrame = None) -> tuple:
        """Build feature matrix and target from OHLCV DataFrame.

        Args:
            df: DataFrame with columns: open, high, low, close, volume.
                Must be sorted chronologically.
            oracle_df: Optional DataFrame with oracle features (OI/funding/orderbook),
                       must share index with df.

        Returns:
            (X, y) tuple where X is the feature DataFrame and y is the
            binary target Series (1=up, 0=down).
        """
        df = df.copy()
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        volume = df["volume"].astype(float)

        features = pd.DataFrame(index=df.index)

        # ── Returns ──────────────────────────────────────────────
        features["ret_1"] = np.log(close / close.shift(1))
        features["ret_5"] = np.log(close / close.shift(5))
        features["ret_10"] = np.log(close / close.shift(10))
        features["ret_20"] = np.log(close / close.shift(20))

        # ── Volatility ───────────────────────────────────────────
        features["vol_20"] = features["ret_1"].rolling(20).std()

        # ── Volume features ──────────────────────────────────────
        vol_sma_20 = volume.rolling(20).mean()
        features["vol_ratio"] = volume / vol_sma_20.replace(0, np.nan)
        features["vol_trend"] = volume.rolling(5).mean() / volume.rolling(20).mean().replace(0, np.nan)

        # ── Price position ───────────────────────────────────────
        hl_range = high - low
        features["price_position"] = np.where(
            hl_range > 0, (close - low) / hl_range, 0.5
        )
        features["dist_sma_20"] = (close - close.rolling(20).mean()) / close.rolling(20).std().replace(0, np.nan)
        features["dist_sma_50"] = (close - close.rolling(50).mean()) / close.rolling(50).std().replace(0, np.nan)

        # ── Momentum ─────────────────────────────────────────────
        features["rsi_14"] = self._rsi(close, period=14)
        features["macd_hist"] = self._macd_hist(close)
        features["roc_10"] = close.pct_change(10) * 100  # Rate of Change

        # ── Volatility regime ────────────────────────────────────
        features["atr_ratio"] = self._atr(df, period=14) / close

        # ── Additional features ──────────────────────────────────
        # price acceleration (change of returns)
        features["ret_accel"] = features["ret_1"] - features["ret_1"].shift(5)

        # Bollinger Band position
        sma_20 = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        features["bb_position"] = (close - sma_20) / (2 * bb_std).replace(0, np.nan)

        # Volume-price trend (correlation proxy)
        features["vpt"] = (features["ret_1"].rolling(5).mean() *
                           features["vol_ratio"].rolling(5).mean())

        # High-low range ratio
        features["hl_ratio"] = hl_range / close.shift(1)

        # ── Oracle features (OI / Funding / Orderbook) ──────────
        if oracle_df is not None and not oracle_df.empty:
            common_idx = features.index.intersection(oracle_df.index)
            if len(common_idx) > 0:
                oracle_cols = [c for c in oracle_df.columns
                              if c not in features.columns
                              and not c.startswith(("open_interest", "best_bid", "best_ask", "mid_price", "timestamp"))]
                for col in oracle_cols:
                    features[col] = oracle_df[col].reindex(features.index)

        # ── Target: next-bar return direction ────────────────────
        future_ret = np.log(close.shift(-1) / close)
        y = (future_ret > 0).astype(int)

        # ── Clean up NaN ─────────────────────────────────────────
        features = features.ffill().bfill()
        # Fill remaining NaN (e.g. sparse oracle features) with 0 as neutral
        features = features.fillna(0)
        # Drop any rows still with NaN (edge of rolling windows)
        mask = features.notna().all(axis=1) & y.notna()
        X = features[mask]
        y = y[mask]

        return X, y

    # ── Indicator helpers ────────────────────────────────────────

    @staticmethod
    def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
        """Compute Relative Strength Index."""
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(span=period, adjust=False).mean()
        avg_loss = loss.ewm(span=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _macd_hist(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
        """Compute MACD histogram."""
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        return macd_line - signal_line

    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Compute Average True Range."""
        high, low, close = df["high"], df["low"], df["close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.ewm(span=period, adjust=False).mean()
