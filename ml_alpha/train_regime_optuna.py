#!/usr/bin/env python3
"""PERF-052: Optuna hyperparameter tuning for MTF Regime Classifier.

Tunes LGBMClassifier hyperparameters on the 72-feature MTF regime dataset.
Goal: beat current test accuracy of 66.87% (target >70%).

Usage:
    cd /home/rinnen/binance_quant
    python3 ml_alpha/train_regime_optuna.py [--trials N] [--timeout SEC]
"""
import sys, os, json, warnings, argparse, time as _time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import joblib
from datetime import datetime, timezone
from lightgbm import LGBMClassifier, early_stopping
import optuna
from optuna.samplers import TPESampler

from data.storage import MarketStorage
from config.settings import get_config
from regime_monitor import build_regime_features_mtf

warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')
optuna.logging.set_verbosity(optuna.logging.WARNING)

HORIZON = 6
THRESHOLD = 0.01
SYMBOL = 'ETH/USDT'
TF = '1h'
MODEL_OUTPUT = 'ml_alpha/regime_model_mtf.pkl'
TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
RANDOM_SEED = 42


def build_target(df, horizon=HORIZON, threshold=THRESHOLD):
    """Build binary target: 1 if |future return| > threshold, else 0."""
    close = df['close'].astype(float).values
    target = np.zeros(len(df), dtype=int)
    for i in range(len(df) - horizon):
        future_ret = (close[i + horizon] - close[i]) / close[i]
        if abs(future_ret) > threshold:
            target[i] = 1
    return target


def load_and_prepare():
    """Load data, build features and target, return train/val/test splits."""
    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    df = storage.load_klines(SYMBOL, TF)
    if df is None or len(df) < 600:
        raise RuntimeError(f"Insufficient data: {len(df) if df is not None else 0} bars")

    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)

    feats = build_regime_features_mtf(df)
    target = build_target(df)

    valid_len = len(target) - HORIZON
    feats = feats.iloc[:valid_len]
    target = target[:valid_len]

    mask = ~feats.isna().any(axis=1)
    feats = feats[mask].copy()
    target = target[mask]

    n = len(feats)
    train_end = int(n * TRAIN_RATIO)
    val_end = int(n * (TRAIN_RATIO + VAL_RATIO))

    X_train, y_train = feats.iloc[:train_end], target[:train_end]
    X_val, y_val = feats.iloc[train_end:val_end], target[train_end:val_end]
    X_test, y_test = feats.iloc[val_end:], target[val_end:]

    return X_train, y_train, X_val, y_val, X_test, y_test, feats.columns


def objective(trial, X_train, y_train, X_val, y_val):
    """Optuna objective — minimize 1 - validation accuracy."""
    params = {
        'n_estimators': trial.suggest_int('n_estimators', 100, 500, step=50),
        'max_depth': trial.suggest_int('max_depth', 2, 8),
        'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.15, log=True),
        'num_leaves': trial.suggest_int('num_leaves', 8, 127),
        'min_child_samples': trial.suggest_int('min_child_samples', 10, 100, step=10),
        'subsample': trial.suggest_float('subsample', 0.5, 1.0),
        'colsample_bytree': trial.suggest_float('colsample_bytree', 0.5, 1.0),
        'reg_alpha': trial.suggest_float('reg_alpha', 1e-3, 10.0, log=True),
        'reg_lambda': trial.suggest_float('reg_lambda', 1e-3, 10.0, log=True),
        'class_weight': 'balanced',
        'random_state': RANDOM_SEED,
        'verbose': -1,
    }

    # Optional: try min_data_in_leaf instead of min_child_samples
    if trial.suggest_categorical('use_min_data', [True, False]):
        del params['min_child_samples']
        params['min_data_in_leaf'] = trial.suggest_int('min_data_in_leaf', 10, 200, step=10)

    model = LGBMClassifier(**params)

    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[early_stopping(50, verbose=False)],
    )

    val_acc = model.score(X_val, y_val)
    return 1.0 - val_acc  # minimize error


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=100, help='Optuna trials (default: 100)')
    ap.add_argument('--timeout', type=int, default=600, help='Max seconds (default: 600)')
    args = ap.parse_args()

    print("=" * 65)
    print("  PERF-052: Optuna Hyperparameter Tuning — MTF Regime Model")
    print("=" * 65)

    # ── Load data ──
    t0 = _time.time()
    print("\n[1/3] Loading data and building features...")
    X_train, y_train, X_val, y_val, X_test, y_test, feature_names = load_and_prepare()
    n_tr, n_up = len(y_train), (y_train == 1).sum()
    n_val = len(y_val)
    print(f"      Train: {n_tr} rows ({n_tr - n_up} RANGING / {n_up} TRENDING, "
          f"{n_up/n_tr:.1%} trending)")
    print(f"      Val:   {n_val} rows")
    print(f"      Test:  {len(y_test)} rows")
    print(f"      Features: {len(feature_names)}")

    # ── Baseline ──
    print("\n[2/3] Baseline (current params)...")
    base_model = LGBMClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.03, num_leaves=15,
        min_child_samples=50, subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.1, reg_lambda=0.1,
        class_weight='balanced', random_state=RANDOM_SEED, verbose=-1,
    )
    base_model.fit(X_train, y_train)
    base_train = base_model.score(X_train, y_train)
    base_test = base_model.score(X_test, y_test)
    print(f"      Current model: Train={base_train:.4f}  Test={base_test:.4f}")

    # ── Optuna study ──
    print(f"\n[3/3] Optuna tuning ({args.trials} trials, {args.timeout}s timeout)...")
    sampler = TPESampler(seed=RANDOM_SEED)
    study = optuna.create_study(
        direction='minimize',
        sampler=sampler,
        pruner=optuna.pruners.MedianPruner(n_startup_trials=10),
    )

    def obj(trial):
        return objective(trial, X_train, y_train, X_val, y_val)

    study.optimize(obj, n_trials=args.trials, timeout=args.timeout, show_progress_bar=True)

    # ── Best model ──
    best_params = study.best_params
    # Remove meta params
    use_min_data = best_params.pop('use_min_data', False)
    if use_min_data:
        best_params['min_data_in_leaf'] = best_params.pop('min_data_in_leaf')
        best_params.pop('min_child_samples', None)

    # Add fixed params
    best_params['class_weight'] = 'balanced'
    best_params['random_state'] = RANDOM_SEED
    best_params['verbose'] = -1

    best_model = LGBMClassifier(**best_params)
    best_model.fit(X_train, y_train, eval_set=[(X_val, y_val)],
                   callbacks=[early_stopping(50, verbose=False)])

    best_train = best_model.score(X_train, y_train)
    best_val = best_model.score(X_val, y_val)
    best_test = best_model.score(X_test, y_test)

    improvement = (best_test - base_test) * 100

    print(f"\n{'=' * 65}")
    print(f"  Optuna Tuning Complete")
    print(f"{'=' * 65}")
    print(f"  Best trial: #{study.best_trial.number}")
    print(f"  Best val accuracy: {1 - study.best_value:.4f}")
    print(f"\n  Performance:")
    print(f"    Train: {best_train:.4f}")
    print(f"    Val:   {best_val:.4f}")
    print(f"    Test:  {best_test:.4f}  ({improvement:+.1f}% vs baseline {base_test:.4f})")
    print(f"    Gap:   {best_train - best_test:.4f}")
    print(f"\n  Best params:")
    for k, v in sorted(best_params.items()):
        if k not in ('class_weight', 'random_state', 'verbose'):
            print(f"    {k}: {v}")

    # Top features
    importances = best_model.feature_importances_
    ranked = sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 10 Features:")
    for feat, score in ranked[:10]:
        print(f"    {feat:32s} {score:.4f}")

    # ── Save if improved ──
    if best_test > base_test:
        print(f"\n  ✅ IMPROVED (+{improvement:+.1f}%) — saving model...")
        model_data = {
            'model': best_model,
            'feature_names': list(feature_names),
            'horizon': HORIZON,
            'threshold': THRESHOLD,
            'symbol': SYMBOL,
            'train_acc': best_train,
            'test_acc': best_test,
            'val_acc': best_val,
            'feature_count': len(feature_names),
            'tuned_with': 'optuna',
            'tuned_params': {k: v for k, v in best_params.items()
                            if k not in ('class_weight', 'random_state', 'verbose')},
            'trained_at': datetime.now(timezone.utc).isoformat(),
        }
        os.makedirs(os.path.dirname(MODEL_OUTPUT), exist_ok=True)
        joblib.dump(model_data, MODEL_OUTPUT)
        print(f"      Saved to: {MODEL_OUTPUT}")
    else:
        print(f"\n  ⚠️  No improvement ({improvement:+.1f}%) — keeping current model.")

    elapsed = _time.time() - t0
    print(f"\n  Total time: {elapsed:.1f}s")
    print(f"  {'=' * 65}")


if __name__ == '__main__':
    main()
