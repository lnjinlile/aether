"""LightGBM-based Alpha model for directional prediction.

Provides training, prediction, and signal generation on top of LightGBM's
LGBMClassifier with the hyperparameter profile specified for alpha mining.
"""

import os
import joblib

import numpy as np
import pandas as pd

from lightgbm import LGBMClassifier


class AlphaModel:
    """LightGBM classifier for binary directional prediction.

    Predicts whether the next bar will be UP (probability > 0.5) or DOWN.
    Signals are thresholded with a configurable confidence margin.
    """

    def __init__(
        self,
        n_estimators: int = 200,
        max_depth: int = 3,
        learning_rate: float = 0.03,
        num_leaves: int = 15,
        min_child_samples: int = 50,
        subsample: float = 0.8,
        colsample_bytree: float = 0.8,
        reg_alpha: float = 0.1,
        reg_lambda: float = 0.1,
        class_weight: str = "balanced",
        random_state: int = 42,
    ):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.num_leaves = num_leaves
        self.min_child_samples = min_child_samples
        self.subsample = subsample
        self.colsample_bytree = colsample_bytree
        self.reg_alpha = reg_alpha
        self.reg_lambda = reg_lambda
        self.class_weight = class_weight
        self.random_state = random_state
        self.model: LGBMClassifier | None = None
        self._feature_names: list | None = None

    def train(self, X: pd.DataFrame, y: pd.Series,
              X_val: pd.DataFrame = None, y_val: pd.Series = None) -> float:
        """Train the LightGBM model.

        Args:
            X: Feature DataFrame.
            y: Binary target Series (1=up, 0=down).
            X_val: Optional validation features for early stopping.
            y_val: Optional validation target for early stopping.

        Returns:
            Training accuracy as a float between 0 and 1.
        """
        self._feature_names = list(X.columns)
        fit_params = dict(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            min_child_samples=self.min_child_samples,
            subsample=self.subsample,
            colsample_bytree=self.colsample_bytree,
            reg_alpha=self.reg_alpha,
            reg_lambda=self.reg_lambda,
            class_weight=self.class_weight,
            random_state=self.random_state,
            verbose=-1,
            predict_disable_shape_check=True,
        )
        self.model = LGBMClassifier(**fit_params)

        if X_val is not None and y_val is not None:
            self.model.fit(X, y, eval_set=[(X_val, y_val)],
                          eval_metric='logloss')
        else:
            self.model.fit(X, y)
        train_acc = self.model.score(X, y)
        return train_acc

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Return probability of UP (class 1) for each row.

        Args:
            X: Feature DataFrame with same columns as training data.

        Returns:
            1D numpy array of probabilities (0-1) that next bar is UP.
        """
        if self.model is None:
            raise RuntimeError("Model not trained yet. Call train() first.")
        return self.model.predict_proba(X)[:, 1]

    def predict_signal(self, X: pd.DataFrame) -> np.ndarray:
        """Convert probabilities to trading signals.

        Signal convention:
            1  = LONG  (prob > 0.55)
           -1  = SHORT (prob < 0.45)
            0  = HOLD  (otherwise)

        Args:
            X: Feature DataFrame (typically single row for live trading).

        Returns:
            1D numpy array of integers in {-1, 0, 1}.
        """
        prob = self.predict(X)
        signals = np.zeros(len(prob), dtype=int)
        signals[prob > 0.55] = 1
        signals[prob < 0.45] = -1
        return signals

    def save(self, path: str):
        """Save the trained model to disk.

        Args:
            path: File path (e.g. 'ml_alpha/model.pkl').
        """
        if self.model is None:
            raise RuntimeError("No model to save. Train first.")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "feature_names": self._feature_names,
                "params": {
                    "n_estimators": self.n_estimators,
                    "max_depth": self.max_depth,
                    "learning_rate": self.learning_rate,
                    "class_weight": self.class_weight,
                    "random_state": self.random_state,
                },
            },
            path,
        )

    def load(self, path: str):
        """Load a trained model from disk.

        Args:
            path: File path to a saved model.
        """
        data = joblib.load(path)
        self.model = data["model"]
        self._feature_names = data["feature_names"]
        params = data.get("params", {})
        self.n_estimators = params.get("n_estimators", self.n_estimators)
        self.max_depth = params.get("max_depth", self.max_depth)
        self.learning_rate = params.get("learning_rate", self.learning_rate)
        self.class_weight = params.get("class_weight", self.class_weight)
        self.random_state = params.get("random_state", self.random_state)

    def get_feature_importance(self) -> list:
        """Return top 10 features with their importance scores.

        Returns:
            List of (feature_name, importance_score) tuples, sorted
            descending by importance.
        """
        if self.model is None:
            raise RuntimeError("Model not trained yet.")
        importances = self.model.feature_importances_
        names = self._feature_names or [f"f{i}" for i in range(len(importances))]
        ranked = sorted(zip(names, importances), key=lambda x: x[1], reverse=True)
        return ranked[:10]
