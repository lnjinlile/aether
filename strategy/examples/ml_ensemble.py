"""
ML Ensemble Strategy — LightGBM + XGBoost + RandomForest voting.

Features:
- 20+ engineered features (returns, volatility, volume, momentum, cross-timeframe)
- Voting ensemble: soft-vote (classification) or weighted-average (regression)
- Online learning: retrain every N bars (default 24)
- Dynamic position sizing based on model confidence
- ATR-based dynamic stop loss / take profit

Dependencies:
- lightgbm (already installed)
- xgboost (pip install xgboost)
- scikit-learn (already installed)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from ..base import BaseStrategy, Signal, SignalType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame, lookback: int = 100,
                      oracle_df: pd.DataFrame = None) -> pd.DataFrame:
    """Compute 20+ features from OHLCV data, optionally enriched with oracle features.

    Args:
        df: OHLCV DataFrame
        lookback: Minimum bars for rolling calculations
        oracle_df: Optional DataFrame with oracle features (OI/funding/orderbook),
                   must share index with df.

    Returns a DataFrame of features aligned with df.index.
    """
    feats = pd.DataFrame(index=df.index)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"] if "volume" in df.columns else pd.Series(1.0, index=df.index)

    # --- Returns features ---
    feats["log_return_1"] = np.log(close / close.shift(1))
    feats["log_return_3"] = np.log(close / close.shift(3))
    feats["log_return_5"] = np.log(close / close.shift(5))
    feats["log_return_10"] = np.log(close / close.shift(10))
    feats["log_return_20"] = np.log(close / close.shift(20))

    # --- Volatility features ---
    feats["volatility_5"] = feats["log_return_1"].rolling(5).std()
    feats["volatility_10"] = feats["log_return_1"].rolling(10).std()
    feats["volatility_20"] = feats["log_return_1"].rolling(20).std()
    feats["hilo_pct"] = (high - low) / close * 100
    feats["atr_pct_14"] = (
        (high - low).rolling(14).mean() / close * 100
    )

    # --- Volume features ---
    feats["volume_ratio_5"] = volume / volume.rolling(5).mean()
    feats["volume_ratio_20"] = volume / volume.rolling(20).mean()
    feats["volume_ma5"] = volume.rolling(5).mean()
    feats["volume_ma20"] = volume.rolling(20).mean()
    feats["volume_trend"] = feats["volume_ma5"] / feats["volume_ma20"]

    # --- Momentum / trend features ---
    feats["price_ma5"] = close.rolling(5).mean()
    feats["price_ma10"] = close.rolling(10).mean()
    feats["price_ma20"] = close.rolling(20).mean()
    feats["price_ma50"] = close.rolling(50).mean()
    feats["ma5_div_ma20"] = feats["price_ma5"] / feats["price_ma20"] - 1.0
    feats["ma10_div_ma50"] = feats["price_ma10"] / feats["price_ma50"] - 1.0
    feats["price_div_ma20"] = close / feats["price_ma20"] - 1.0
    feats["price_div_ma50"] = close / feats["price_ma50"] - 1.0

    # RSI-like
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=14, adjust=False).mean()
    avg_loss = loss.ewm(span=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    feats["rsi_14"] = 100 - (100 / (1 + rs))

    # MACD-like
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    feats["macd"] = ema12 - ema26
    feats["macd_signal"] = feats["macd"].ewm(span=9, adjust=False).mean()
    feats["macd_hist"] = feats["macd"] - feats["macd_signal"]

    # --- Cross-timeframe proxy features (using 1h data via different lags) ---
    feats["high_low_range_pct"] = (high - low) / close * 100
    feats["close_position"] = (close - low) / (high - low).replace(0, 1)
    feats["close_position_ma"] = feats["close_position"].rolling(10).mean()

    # --- Skew / kurt (distribution features) ---
    feats["skew_20"] = feats["log_return_1"].rolling(20).skew()
    feats["kurt_20"] = feats["log_return_1"].rolling(20).kurt()

    # --- Oracle features (OI / Funding / Orderbook) ---
    if oracle_df is not None and not oracle_df.empty:
        common_idx = feats.index.intersection(oracle_df.index)
        if len(common_idx) > 0:
            oracle_cols = [c for c in oracle_df.columns
                          if c not in feats.columns
                          and not c.startswith(("open_interest", "best_bid", "best_ask", "mid_price", "timestamp"))]
            for col in oracle_cols:
                feats[col] = oracle_df[col].reindex(feats.index)
            logger.debug("Merged %d oracle feature columns", len(oracle_cols))

    # Drop NaN-only columns, forward fill any remaining NaN
    feats = feats.replace([np.inf, -np.inf], np.nan)
    feats = feats.ffill().fillna(0.0)

    return feats


def build_labels(df: pd.DataFrame, horizon: int = 5, mode: str = "direction") -> np.ndarray:
    """Build target labels for supervised learning.

    Args:
        df: OHLCV DataFrame
        horizon: Number of bars ahead to predict
        mode: 'direction' (1=up, -1=down, 0=flat) or 'regression' (log return)

    Returns:
        np.array of labels, aligned with df index (last `horizon` bars are NaN)
    """
    close = df["close"].values.astype(float)
    n = len(close)
    labels = np.full(n, np.nan)

    future_close = np.roll(close, -horizon)
    future_close[-horizon:] = np.nan  # last horizon bars have no label

    if mode == "direction":
        pct_change = (future_close - close) / close
        # 1 = up (>0.3%), -1 = down (<-0.3%), 0 = flat
        labels[pct_change > 0.003] = 1
        labels[pct_change < -0.003] = -1
        labels[(pct_change >= -0.003) & (pct_change <= 0.003)] = 0
    else:  # regression
        labels = np.log(future_close / close)

    labels[-horizon:] = np.nan
    return labels


# ---------------------------------------------------------------------------
# ML Ensemble Strategy
# ---------------------------------------------------------------------------

class MLEnsembleStrategy(BaseStrategy):
    """ML Ensemble: LightGBM + XGBoost + RandomForest voting.

    Parameters
    ----------
    symbols : list[str]
        Trading symbols
    timeframes : list[str]
        Kline timeframes
    retrain_every : int
        Number of bars between model retraining (online learning)
    prediction_horizon : int
        Number of bars ahead to predict
    min_train_samples : int
        Minimum samples before first training
    confidence_threshold : float
        Minimum ensemble confidence to enter a trade (0.5-1.0)
    atr_period : int
        ATR period for dynamic stop/take-profit
    atr_sl_mult : float
        ATR multiplier for stop loss
    atr_tp_mult : float
        ATR multiplier for take profit
    cooldown_bars : int
        Minimum bars between trades
    model_type : str
        'classifier' or 'regressor'
    """

    def __init__(
        self,
        name: str = "MLEnsemble",
        symbols: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        retrain_every: int = 24,
        prediction_horizon: int = 5,
        min_train_samples: int = 200,
        confidence_threshold: float = 0.60,
        atr_period: int = 14,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 3.0,
        cooldown_bars: int = 5,
        model_type: str = "classifier",
        leverage: int = 3,
    ):
        super().__init__(
            name=name,
            symbols=symbols or ["BTC/USDT"],
            timeframes=timeframes or ["1h"],
        )
        self.params = {
            "retrain_every": retrain_every,
            "prediction_horizon": prediction_horizon,
            "min_train_samples": min_train_samples,
            "confidence_threshold": confidence_threshold,
            "atr_period": atr_period,
            "atr_sl_mult": atr_sl_mult,
            "atr_tp_mult": atr_tp_mult,
            "cooldown_bars": cooldown_bars,
            "model_type": model_type,
            "leverage": leverage,
        }

        self._models: Dict[str, Tuple] = {}  # symbol -> (lgb, xgb, rf)
        self._last_prediction: Dict[str, dict] = {}
        self._bars_since_retrain: Dict[str, int] = {}
        self._bars_since_last_trade = cooldown_bars + 1

        # Store latest prediction confidence/count for each symbol
        self._latest_conf: Dict[str, float] = {}
        self._latest_dir: Dict[str, int] = {}
        self._feature_cols: Optional[List[str]] = None

    # ------------------------------------------------------------------
    # Model management
    # ------------------------------------------------------------------

    def _ensure_models(self, symbol: str, X: np.ndarray, y: np.ndarray):
        """Train or retrain all three models."""
        if symbol not in self._models:
            self._models[symbol] = self._build_models()
        self._fit_models(symbol, X, y)

    def _build_models(self):
        """Create LightGBM, XGBoost, and RandomForest instances."""
        from lightgbm import LGBMClassifier, LGBMRegressor
        from xgboost import XGBClassifier, XGBRegressor
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

        if self.params["model_type"] == "classifier":
            lgb = LGBMClassifier(
                n_estimators=100,
                max_depth=6,
                learning_rate=0.05,
                num_leaves=31,
                verbosity=-1,
                random_state=42,
                force_col_wise=True,
                predict_disable_shape_check=True,
            )
            xgb = XGBClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.05,
                verbosity=0,
                random_state=42,
                eval_metric="mlogloss",
            )
            rf = RandomForestClassifier(
                n_estimators=100,
                max_depth=8,
                random_state=42,
                n_jobs=-1,
            )
        else:
            lgb = LGBMRegressor(
                n_estimators=100,
                max_depth=6,
                learning_rate=0.05,
                num_leaves=31,
                verbosity=-1,
                random_state=42,
                force_col_wise=True,
                predict_disable_shape_check=True,
            )
            xgb = XGBRegressor(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.05,
                verbosity=0,
                random_state=42,
            )
            rf = RandomForestRegressor(
                n_estimators=100,
                max_depth=8,
                random_state=42,
                n_jobs=-1,
            )
        return (lgb, xgb, rf)

    # Label mapping: our labels (-1, 0, 1) -> sklearn labels (0, 1, 2)
    _LABEL_MAP = {-1: 0, 0: 1, 1: 2}
    _LABEL_INV = {0: -1, 1: 0, 2: 1}

    def _remap_labels(self, y: np.ndarray) -> np.ndarray:
        """Remap labels from [-1, 0, 1] to [0, 1, 2] for sklearn classifiers."""
        y_remapped = y.copy().astype(int)
        y_remapped += 1  # -1->0, 0->1, 1->2
        return y_remapped

    def _fit_models(self, symbol: str, X: np.ndarray, y: np.ndarray):
        """Fit all three models on the training data."""
        lgb, xgb, rf = self._models.get(
            symbol, self._build_models()
        )
        try:
            if self.params["model_type"] == "classifier":
                y_mapped = self._remap_labels(y)
            else:
                y_mapped = y
            lgb.fit(X, y_mapped)
            xgb.fit(X, y_mapped)
            rf.fit(X, y_mapped)
        except Exception as e:
            logger.warning(f"MLEnsemble fit error for {symbol}: {e}")
            return
        self._models[symbol] = (lgb, xgb, rf)

    def _predict(self, symbol: str, X: np.ndarray) -> Tuple[int, float]:
        """Ensemble prediction: returns (direction, confidence)."""
        models = self._models.get(symbol)
        if models is None:
            return 0, 0.0

        lgb, xgb, rf = models

        try:
            if self.params["model_type"] == "classifier":
                # Soft voting: average class probabilities
                prob_lgb = lgb.predict_proba(X)
                prob_xgb = xgb.predict_proba(X)
                prob_rf = rf.predict_proba(X)

                # Average probabilities
                avg_prob = (prob_lgb + prob_xgb + prob_rf) / 3.0
                pred_class = int(np.argmax(avg_prob, axis=1)[0])

                # Remap from sklearn label (0,1,2) to our direction (-1,0,1)
                direction = pred_class - 1  # 0->-1, 1->0, 2->1
                confidence = float(np.max(avg_prob))
            else:
                # Regression: average predictions
                pred_lgb = lgb.predict(X).ravel()
                pred_xgb = xgb.predict(X).ravel()
                pred_rf = rf.predict(X).ravel()
                avg_pred = float(np.mean([pred_lgb[0], pred_xgb[0], pred_rf[0]]))

                # Convert regression to direction
                if avg_pred > 0.002:
                    direction = 1
                elif avg_pred < -0.002:
                    direction = -1
                else:
                    direction = 0

                # Confidence from agreement
                signs = [np.sign(pred_lgb[0]), np.sign(pred_xgb[0]), np.sign(pred_rf[0])]
                agreement = sum(1 for s in signs if s == np.sign(avg_pred)) / 3.0
                confidence = agreement

        except Exception as e:
            logger.warning(f"MLEnsemble predict error for {symbol}: {e}")
            return 0, 0.0

        return direction, confidence

    def _compute_atr(self, key: tuple) -> float:
        """Compute ATR value from stored data."""
        df = self._data.get(key)
        if df is None or len(df) < 2:
            return 0.0
        atr_p = self.params["atr_period"]
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift(1)).abs()
        low_close = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.ewm(span=atr_p, adjust=False).mean()
        return float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.0

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def _preprocess(self, symbol: str, timeframe: str, df: pd.DataFrame):
        """Compute features and indicators when data is fed."""
        key = (symbol, timeframe)

        # Try to load oracle features for enrichment
        oracle_df = None
        try:
            from ml_alpha.oracle_features import merge_oracle_features
            bin_sym = symbol.replace("/", "")  # BTC/USDT -> BTCUSDT
            enriched = merge_oracle_features(df, bin_sym)
            oracle_df = enriched[[c for c in enriched.columns if c not in df.columns]]
        except Exception:
            pass  # Oracle features are optional

        # Compute features
        X = compute_features(df, oracle_df=oracle_df)
        if self._feature_cols is None:
            self._feature_cols = list(X.columns)

        # Compute labels for training
        y = build_labels(df, horizon=self.params["prediction_horizon"],
                         mode="direction" if self.params["model_type"] == "classifier" else "regression")

        # Store indicators (features + labels + ATR)
        ind = X.copy()
        ind["label"] = y
        # ATR
        atr_p = self.params["atr_period"]
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift(1)).abs()
        low_close = (df["low"] - df["close"].shift(1)).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        ind["atr"] = tr.ewm(span=atr_p, adjust=False).mean()
        ind["close"] = df["close"]

        self._indicators[key] = ind

        # Trigger retrain if enough data
        self._maybe_retrain(symbol, key)

    def _maybe_retrain(self, symbol: str, key: tuple):
        """Check if retraining is needed and do it."""
        p = self.params
        bars_since = self._bars_since_retrain.get(symbol, 999)
        if bars_since < p["retrain_every"]:
            self._bars_since_retrain[symbol] = bars_since + 1
            return

        # Time to retrain
        ind = self._indicators.get(key)
        if ind is None:
            return

        # Get valid training samples
        valid = ind.dropna(subset=["label"])
        if len(valid) < p["min_train_samples"]:
            self._bars_since_retrain[symbol] = bars_since + 1
            return

        # Use last N samples (avoid lookahead - only use data up to current bar)
        # But we can safely use all historical data (not the future labels)
        train_data = valid.iloc[:-p["prediction_horizon"]]  # exclude bars with no future
        if len(train_data) < p["min_train_samples"]:
            self._bars_since_retrain[symbol] = bars_since + 1
            return

        X_train = train_data[self._feature_cols].values.astype(float)
        y_train = train_data["label"].values.astype(float if p["model_type"] == "regression" else int)

        self._ensure_models(symbol, X_train, y_train)
        self._bars_since_retrain[symbol] = 0
        logger.debug(f"MLEnsemble retrained for {symbol} on {len(train_data)} samples")

    def generate_signal(self, symbol: str) -> Signal:
        """Generate trading signal based on ensemble prediction."""
        timeframe = self.timeframes[0]
        key = (symbol, timeframe)
        ind = self._indicators.get(key)
        df = self._data.get(key)
        p = self.params

        if ind is None or df is None or len(df) < p["min_train_samples"]:
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason="Insufficient data for ML model", strategy_name=self.name,
            )

        self._bars_since_last_trade += 1
        current_price = float(df["close"].iloc[-1])
        atr = float(ind["atr"].iloc[-1]) if not pd.isna(ind["atr"].iloc[-1]) else 0.0

        # Check if we have trained models
        if symbol not in self._models:
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason="Model not yet trained", strategy_name=self.name,
            )

        # Get latest feature row for prediction
        X_latest = ind[self._feature_cols].iloc[-1:].values.astype(float)
        if np.isnan(X_latest).any():
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason="NaN in features", strategy_name=self.name,
            )

        direction, confidence = self._predict(symbol, X_latest)
        self._latest_conf[symbol] = confidence
        self._latest_dir[symbol] = direction

        has_pos = self.has_position(symbol)

        # --- Position management ---
        if has_pos:
            pos = self._positions[symbol]
            entry = pos["entry_price"]

            # Exit on signal reversal
            if pos["side"] == "LONG" and direction == -1:
                return Signal(
                    type=SignalType.CLOSE_LONG, symbol=symbol, price=current_price,
                    reason=f"ML ensemble reversed (conf={confidence:.2f})",
                    strategy_name=self.name, confidence=confidence,
                    timestamp=df.index[-1],
                )
            elif pos["side"] == "SHORT" and direction == 1:
                return Signal(
                    type=SignalType.CLOSE_SHORT, symbol=symbol, price=current_price,
                    reason=f"ML ensemble reversed (conf={confidence:.2f})",
                    strategy_name=self.name, confidence=confidence,
                    timestamp=df.index[-1],
                )

            # Exit on confidence drop
            if confidence < 0.40:
                sig_type = SignalType.CLOSE_LONG if pos["side"] == "LONG" else SignalType.CLOSE_SHORT
                return Signal(
                    type=sig_type, symbol=symbol, price=current_price,
                    reason=f"Confidence dropped to {confidence:.2f}",
                    strategy_name=self.name, confidence=confidence,
                    timestamp=df.index[-1],
                )

            # ATR-based stop loss / take profit
            if atr > 0:
                atr_sl = entry - p["atr_sl_mult"] * atr
                atr_tp = entry + p["atr_tp_mult"] * atr
                if pos["side"] == "LONG":
                    if current_price <= atr_sl:
                        return Signal(
                            type=SignalType.CLOSE_LONG, symbol=symbol, price=current_price,
                            reason=f"ATR stop loss (SL={atr_sl:.2f})",
                            strategy_name=self.name, confidence=confidence,
                            timestamp=df.index[-1],
                        )
                    if current_price >= atr_tp:
                        return Signal(
                            type=SignalType.CLOSE_LONG, symbol=symbol, price=current_price,
                            reason=f"ATR take profit (TP={atr_tp:.2f})",
                            strategy_name=self.name, confidence=confidence,
                            timestamp=df.index[-1],
                        )
                else:  # SHORT
                    atr_sl = entry + p["atr_sl_mult"] * atr
                    atr_tp = entry - p["atr_tp_mult"] * atr
                    if current_price >= atr_sl:
                        return Signal(
                            type=SignalType.CLOSE_SHORT, symbol=symbol, price=current_price,
                            reason=f"ATR stop loss (SL={atr_sl:.2f})",
                            strategy_name=self.name, confidence=confidence,
                            timestamp=df.index[-1],
                        )
                    if current_price <= atr_tp:
                        return Signal(
                            type=SignalType.CLOSE_SHORT, symbol=symbol, price=current_price,
                            reason=f"ATR take profit (TP={atr_tp:.2f})",
                            strategy_name=self.name, confidence=confidence,
                            timestamp=df.index[-1],
                        )

            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason=f"Holding (dir={direction}, conf={confidence:.2f})",
                strategy_name=self.name, confidence=confidence,
                timestamp=df.index[-1],
            )

        # --- Entry logic ---
        if direction == 0:
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason=f"No clear direction (conf={confidence:.2f})",
                strategy_name=self.name, confidence=confidence,
                timestamp=df.index[-1],
            )

        if confidence < p["confidence_threshold"]:
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason=f"Confidence {confidence:.2f} below threshold {p['confidence_threshold']}",
                strategy_name=self.name, confidence=confidence,
                timestamp=df.index[-1],
            )

        if self._bars_since_last_trade <= p["cooldown_bars"]:
            return Signal(
                type=SignalType.HOLD, symbol=symbol,
                reason="Cooldown", strategy_name=self.name,
                timestamp=df.index[-1],
            )

        # Dynamic position size: scale with confidence
        base_qty = 0.001
        qty = base_qty * (confidence / 0.5)  # scale up for higher confidence

        if atr > 0:
            if direction == 1:
                sl_price = current_price - p["atr_sl_mult"] * atr
                tp_price = current_price + p["atr_tp_mult"] * atr
            else:
                sl_price = current_price + p["atr_sl_mult"] * atr
                tp_price = current_price - p["atr_tp_mult"] * atr
        else:
            sl_price = float("nan")
            tp_price = float("nan")

        sig_type = SignalType.LONG if direction == 1 else SignalType.SHORT
        self._bars_since_last_trade = 0

        return Signal(
            type=sig_type,
            symbol=symbol,
            price=current_price,
            quantity=round(qty, 6),
            stop_loss=round(sl_price, 2) if not np.isnan(sl_price) else float("nan"),
            take_profit=round(tp_price, 2) if not np.isnan(tp_price) else float("nan"),
            reason=f"ML ensemble (dir={direction}, conf={confidence:.2f}, "
                   f"models: LGB+XGB+RF)",
            confidence=confidence,
            leverage=p["leverage"],
            strategy_name=self.name,
            timestamp=df.index[-1],
        )

    def get_required_data(self) -> dict:
        return {
            "symbols": self.symbols,
            "timeframes": self.timeframes,
            "lookback_bars": max(self.params["min_train_samples"], 300),
        }
