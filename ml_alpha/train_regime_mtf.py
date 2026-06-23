#!/usr/bin/env python3
"""Train Multi-Timeframe (MTF) Regime Classifier.

Uses build_regime_features_mtf() to produce 72 features (24 each × 1h/4h/1d),
trains a LightGBM binary classifier predicting TRENDING vs RANGING 6h ahead,
and saves the model to ml_alpha/regime_model_mtf.pkl.

Usage:
    cd /home/rinnen/binance_quant
    source venv/bin/activate
    python3 ml_alpha/train_regime_mtf.py
"""

import sys, os, json, warnings
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import joblib
from datetime import datetime, timezone
from lightgbm import LGBMClassifier

from data.storage import MarketStorage
from config.settings import get_config
from regime_monitor import build_regime_features_mtf

warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

HORIZON = 6       # hours ahead
THRESHOLD = 0.01   # 1% move threshold
SYMBOL = 'ETH/USDT'
TF = '1h'
MODEL_OUTPUT = 'ml_alpha/regime_model_mtf.pkl'

# Train/val/test split ratios
TRAIN_RATIO = 0.70
VAL_RATIO = 0.15  # test = remaining 15%


def build_target(df, horizon=HORIZON, threshold=THRESHOLD):
    """Build binary target: 1 if future return exceeds threshold, else 0."""
    close = df['close'].astype(float).values
    target = np.zeros(len(df), dtype=int)
    for i in range(len(df) - horizon):
        future_ret = (close[i + horizon] - close[i]) / close[i]
        if future_ret > threshold:
            target[i] = 1  # TRENDING up
        elif future_ret < -threshold:
            target[i] = 1  # TRENDING down (absolute move > threshold)
        else:
            target[i] = 0  # RANGING
    # Last 'horizon' rows have no future data — drop them later
    return target


def main():
    print("=" * 60)
    print("  MTF Regime Classifier — Training (120 features)")
    print("=" * 60)

    # ── 1. Load data ──────────────────────────────────────────
    print("\n[1/4] Loading ETH/USDT 1h data...")
    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    df = storage.load_klines(SYMBOL, TF)
    if df is None or len(df) < 600:
        print(f"ERROR: Insufficient data ({len(df) if df is not None else 0} bars). Need ≥600.")
        sys.exit(1)

    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)
    print(f"      Loaded {len(df)} bars: {df.index[0]} → {df.index[-1]}")

    # ── 2. Build features & target ────────────────────────────
    print("\n[2/4] Building MTF features (1h + 4h + 1d)...")
    feats = build_regime_features_mtf(df)
    target = build_target(df)

    # Align: drop last HORIZON rows (no future target) and any NaN features
    valid_len = len(target) - HORIZON
    feats = feats.iloc[:valid_len]
    target = target[:valid_len]

    # Drop rows where target or features are NaN
    mask = ~feats.isna().any(axis=1)
    feats = feats[mask].copy()
    target = target[mask]

    print(f"      Feature matrix: {feats.shape[0]} rows × {feats.shape[1]} cols (40/tf × 3 = 120)")
    n_up = (target == 1).sum()
    n_down = (target == 0).sum()
    print(f"      Class balance: TRENDING={n_up} ({n_up/len(target):.1%}), "
          f"RANGING={n_down} ({n_down/len(target):.1%})")

    # ── 3. Train/val/test split ───────────────────────────────
    n = len(feats)
    train_end = int(n * TRAIN_RATIO)
    val_end = int(n * (TRAIN_RATIO + VAL_RATIO))

    X_train, y_train = feats.iloc[:train_end], target[:train_end]
    X_val, y_val = feats.iloc[train_end:val_end], target[train_end:val_end]
    X_test, y_test = feats.iloc[val_end:], target[val_end:]
    print(f"\n[3/4] Split: {len(X_train)} train / {len(X_val)} val / {len(X_test)} test")

    # ── 4. Train LightGBM ────────────────────────────────────
    print("\n[4/4] Training LightGBM classifier...")
    # Same params as original regime model (from joblib inspection)
    model = LGBMClassifier(
        n_estimators=200,
        max_depth=3,
        learning_rate=0.03,
        num_leaves=15,
        min_child_samples=50,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.1,
        class_weight='balanced',
        random_state=42,
        verbose=-1,
    )

    model.fit(X_train, y_train, eval_set=[(X_val, y_val)])

    train_acc = model.score(X_train, y_train)
    val_acc = model.score(X_val, y_val)
    test_acc = model.score(X_test, y_test)

    print(f"      Train accuracy: {train_acc:.4f}")
    print(f"      Val accuracy:   {val_acc:.4f}")
    print(f"      Test accuracy:  {test_acc:.4f}")
    gap = train_acc - test_acc
    if gap > 0.15:
        print(f"      ⚠️  Overfitting gap: {gap:.1%} (train-test)")
    else:
        print(f"      Generalization gap: {gap:.1%} (acceptable)")

    # Top feature importance
    importances = model.feature_importances_
    ranked = sorted(zip(feats.columns, importances), key=lambda x: x[1], reverse=True)
    print(f"\n      Top 15 Features:")
    for feat, score in ranked[:15]:
        print(f"        {feat:30s}  {score:.4f}")

    # ── 5. Save model ─────────────────────────────────────────
    model_data = {
        'model': model,
        'feature_names': list(feats.columns),
        'horizon': HORIZON,
        'threshold': THRESHOLD,
        'symbol': SYMBOL,
        'train_acc': train_acc,
        'test_acc': test_acc,
        'val_acc': val_acc,
        'feature_count': len(feats.columns),
        'trained_at': datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(os.path.dirname(MODEL_OUTPUT), exist_ok=True)
    joblib.dump(model_data, MODEL_OUTPUT)

    print(f"\n      Model saved to: {MODEL_OUTPUT}")
    print(f"      Size: {os.path.getsize(MODEL_OUTPUT) / 1024:.1f} KB")

    # ── 6. Summary ─────────────────────────────────────────────
    improvement = (test_acc - 0.6591) * 100  # vs baseline 1h-only model
    print(f"\n{'=' * 60}")
    print(f"  Training Complete — MTF Regime Model")
    print(f"{'=' * 60}")
    print(f"  Features:  {len(feats.columns)} (24 × 3 timeframes)")
    print(f"  Test acc:  {test_acc:.4f} ({improvement:+.1f}% vs 1h-only baseline)")
    print(f"  Train/Val/Test gap: {train_acc - test_acc:.3f}")
    if test_acc > 0.6591:
        print(f"  ✅ MTF features IMPROVE accuracy (+{improvement:.1f}%)")
    else:
        print(f"  ⚠️  MTF features did NOT improve accuracy ({improvement:.1f}%)")
    print(f"  {'=' * 60}")


if __name__ == '__main__':
    main()
