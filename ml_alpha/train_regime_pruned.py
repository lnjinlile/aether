#!/usr/bin/env python3
"""PERF-055: Feature-Pruned Regime Model Training.

Removes 34 zero-importance features from the 120-feature MTF regime model,
then retrains with Optuna to eliminate dead weight and improve generalization.

Usage:
    cd /home/rinnen/binance_quant
    python3 ml_alpha/train_regime_pruned.py [--trials N] [--timeout SEC]
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

# 34 zero-importance features identified from current model (69.02% acc)
ZERO_IMP_FEATURES = {
    # 1h timeframe
    'adx_14_norm', 'autocorr_10', 'close_pos_20', 'close_pos_50',
    'close_vs_ma_50', 'kurtosis_20', 'ma_disp_20_100', 'roc_10',
    'rsi_14_norm', 'rsi_div_20', 'vol_price_corr_10', 'vol_ratio',
    # 4h timeframe
    '4h_adx_28_norm', '4h_close_pos_20', '4h_close_vs_ma_50',
    '4h_hl_range_20', '4h_ma_disp_10_50', '4h_rsi_14_norm',
    '4h_vol_price_corr_10', '4h_vol_price_corr_20', '4h_vol_trend',
    # 1d timeframe
    '1d_adx_14', '1d_adx_14_norm', '1d_adx_28', '1d_adx_28_norm',
    '1d_autocorr_10', '1d_close_vs_ma_20', '1d_hl_range_10',
    '1d_hl_range_20', '1d_kurtosis_20', '1d_roc_5', '1d_roc_20',
    '1d_vol_10_norm', '1d_vol_price_corr_10',
}


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

    # Prune zero-importance features
    drop_cols = [c for c in ZERO_IMP_FEATURES if c in feats.columns]
    print(f"      Pruning {len(drop_cols)} zero-importance features...")
    for c in drop_cols:
        print(f"        - {c}")
    feats_pruned = feats.drop(columns=drop_cols)

    remaining = [c for c in feats.columns if c not in ZERO_IMP_FEATURES]
    print(f"      Retained {len(remaining)} features (from {len(feats.columns)})")

    n = len(feats_pruned)
    train_end = int(n * TRAIN_RATIO)
    val_end = int(n * (TRAIN_RATIO + VAL_RATIO))

    X_train = feats_pruned.iloc[:train_end]
    y_train = target[:train_end]
    X_val = feats_pruned.iloc[train_end:val_end]
    y_val = target[train_end:val_end]
    X_test = feats_pruned.iloc[val_end:]
    y_test = target[val_end:]

    return X_train, y_train, X_val, y_val, X_test, y_test, remaining


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
    return 1.0 - val_acc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=50, help='Optuna trials (default: 50)')
    ap.add_argument('--timeout', type=int, default=300, help='Max seconds (default: 300)')
    args = ap.parse_args()

    print("=" * 65)
    print("  PERF-055: Feature-Pruned Regime Model — Training (86 features)")
    print("=" * 65)

    # ── Load data ──
    t0 = _time.time()
    print("\n[1/4] Loading data and building features...")
    X_train, y_train, X_val, y_val, X_test, y_test, feature_names = load_and_prepare()
    n_tr, n_up = len(y_train), (y_train == 1).sum()
    print(f"      Train: {n_tr} rows ({n_tr - n_up} RANGING / {n_up} TRENDING, "
          f"{n_up/n_tr:.1%} trending)")
    print(f"      Val:   {len(y_val)} rows")
    print(f"      Test:  {len(y_test)} rows")
    print(f"      Features: {len(feature_names)} (pruned from 120)")

    # ── Baseline with current (120-ft) model ──
    print("\n[2/4] Baseline — loading current 120-feature model...")
    current = joblib.load(MODEL_OUTPUT)
    print(f"      Current: Train={current['train_acc']:.4f}  Test={current['test_acc']:.4f}  "
          f"Features={current['feature_count']}")

    # ── Optuna tuning on pruned features ──
    print(f"\n[3/4] Optuna tuning ({args.trials} trials, {args.timeout}s timeout)...")
    sampler = TPESampler(seed=RANDOM_SEED)
    study = optuna.create_study(
        direction='minimize',
        sampler=sampler,
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5),
    )

    def obj(trial):
        return objective(trial, X_train, y_train, X_val, y_val)

    study.optimize(obj, n_trials=args.trials, timeout=args.timeout, show_progress_bar=True)

    # ── Best model ──
    best_params = study.best_params
    use_min_data = best_params.pop('use_min_data', False)
    if use_min_data:
        best_params['min_data_in_leaf'] = best_params.pop('min_data_in_leaf')
        best_params.pop('min_child_samples', None)

    best_params['class_weight'] = 'balanced'
    best_params['random_state'] = RANDOM_SEED
    best_params['verbose'] = -1

    best_model = LGBMClassifier(**best_params)
    best_model.fit(X_train, y_train, eval_set=[(X_val, y_val)],
                   callbacks=[early_stopping(50, verbose=False)])

    best_train = best_model.score(X_train, y_train)
    best_val = best_model.score(X_val, y_val)
    best_test = best_model.score(X_test, y_test)

    improvement = (best_test - current['test_acc']) * 100

    print(f"\n{'=' * 65}")
    print(f"  PERF-055: Feature-Pruned Regime Model — Complete")
    print(f"{'=' * 65}")
    print(f"  Best trial: #{study.best_trial.number}")
    print(f"  Best val accuracy: {1 - study.best_value:.4f}")
    print(f"\n  Performance (86 features, pruned from 120):")
    print(f"    Train: {best_train:.4f}")
    print(f"    Val:   {best_val:.4f}")
    print(f"    Test:  {best_test:.4f}  ({improvement:+.1f}% vs current {current['test_acc']:.4f})")
    print(f"    Gap:   {best_train - best_test:.4f}")
    print(f"\n  Best params:")
    for k, v in sorted(best_params.items()):
        if k not in ('class_weight', 'random_state', 'verbose'):
            print(f"    {k}: {v}")

    # Top features
    importances = best_model.feature_importances_
    ranked = sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True)
    print(f"\n  Top 10 Features (86-ft model):")
    for feat, score in ranked[:10]:
        print(f"    {feat:32s} {score:.4f}")

    # Zero-importance count in new model
    zeros_new = sum(1 for imp in importances if imp == 0)
    print(f"\n  Zero-importance features: {zeros_new}/{len(feature_names)} "
          f"(was 34/120 = 28.3%)")

    # ── Save ──
    print(f"\n[4/4] Saving model...")

    # Always save (feature pruning is a structural improvement)
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
        'tuned_with': 'optuna_pruned',
        'tuned_params': {k: v for k, v in best_params.items()
                        if k not in ('class_weight', 'random_state', 'verbose')},
        'pruned_from': 120,
        'pruned_zero_imp_count': len(ZERO_IMP_FEATURES),
        'trained_at': datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(os.path.dirname(MODEL_OUTPUT), exist_ok=True)
    # Backup current model
    backup_path = MODEL_OUTPUT.replace('.pkl', f'_prev_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")}.pkl')
    if os.path.exists(MODEL_OUTPUT):
        joblib.dump(current, backup_path)
        print(f"      Backed up current model to: {backup_path}")
    joblib.dump(model_data, MODEL_OUTPUT)
    print(f"      Saved pruned model to: {MODEL_OUTPUT}")

    elapsed = _time.time() - t0
    print(f"\n  Total time: {elapsed:.1f}s")
    print(f"  {'=' * 65}")

    # ── Verdict ──
    print(f"\n  PERF-055 VERDICT:")
    print(f"    Before: 120 features, test_acc={current['test_acc']:.4f}")
    print(f"    After:  {len(feature_names)} features, test_acc={best_test:.4f}")
    print(f"    Delta:  {improvement:+.1f}% accuracy")
    print(f"    Pruned: {len(ZERO_IMP_FEATURES)} zero-importance features removed")


if __name__ == '__main__':
    main()
