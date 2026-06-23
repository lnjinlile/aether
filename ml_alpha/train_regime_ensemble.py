#!/usr/bin/env python3
"""PERF-056: Stacked Ensemble Regime Model.

Combines LightGBM + XGBoost + RandomForest via LogisticRegression meta-learner.
Goal: push regime classification accuracy past 70% (current: 69.02%).

Usage:
    cd /home/rinnen/binance_quant
    python3 ml_alpha/train_regime_ensemble.py [--trials N] [--timeout SEC]
"""
import sys, os, json, warnings, argparse, time as _time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import joblib
from datetime import datetime, timezone
from lightgbm import LGBMClassifier, early_stopping
from xgboost import XGBClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from data.storage import MarketStorage
from config.settings import get_config
from regime_monitor import build_regime_features_mtf

warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

HORIZON = 6
THRESHOLD = 0.01
SYMBOL = 'ETH/USDT'
TF = '1h'
ENSEMBLE_OUTPUT = 'ml_alpha/regime_model_ensemble.pkl'
CURRENT_MODEL = 'ml_alpha/regime_model_mtf.pkl'
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

    return X_train, y_train, X_val, y_val, X_test, y_test, list(feats.columns)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=30, help='LightGBM Optuna trials (default: 30)')
    ap.add_argument('--timeout', type=int, default=300, help='Max seconds (default: 300)')
    ap.add_argument('--fast', action='store_true', help='Skip Optuna, use fixed params')
    args = ap.parse_args()

    print("=" * 65)
    print("  PERF-056: Stacked Ensemble Regime Model")
    print("=" * 65)

    # ── Load data ──
    t0 = _time.time()
    print("\n[1/5] Loading data and building features...")
    X_train, y_train, X_val, y_val, X_test, y_test, feature_names = load_and_prepare()
    n_tr, n_up = len(y_train), (y_train == 1).sum()
    print(f"      Train: {n_tr} rows ({n_tr - n_up} RANGING / {n_up} TRENDING, "
          f"{n_up/n_tr:.1%} trending)")
    print(f"      Val:   {len(y_val)} rows")
    print(f"      Test:  {len(y_test)} rows")
    print(f"      Features: {len(feature_names)}")

    # ── Baseline ──
    print("\n[2/5] Baseline — current MTF model...")
    current = joblib.load(CURRENT_MODEL)
    # Current model predicts on test
    current_model = current.get('model')
    if current_model is None:
        print("      WARN: No model object in pkl, re-evaluating...")
        # Try to use the saved params
    else:
        cur_test = current_model.score(X_test, y_test)
        cur_train = current.get('train_acc', 0)
        print(f"      Current MTF: Train={current.get('train_acc', 0):.4f}  "
              f"Test={current.get('test_acc', cur_test):.4f}  "
              f"Features={current.get('feature_count', '?')}")

    # ── Train base models ──
    print(f"\n[3/5] Training base models...")

    # LightGBM (tuned or fast)
    if args.fast:
        lgb_params = {
            'n_estimators': 300, 'max_depth': 3, 'learning_rate': 0.05,
            'num_leaves': 31, 'class_weight': 'balanced',
            'random_state': RANDOM_SEED, 'verbose': -1,
        }
    else:
        import optuna
        from optuna.samplers import TPESampler
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        def lgb_objective(trial):
            params = {
                'n_estimators': trial.suggest_int('n_estimators', 100, 500, step=50),
                'max_depth': trial.suggest_int('max_depth', 2, 6),
                'learning_rate': trial.suggest_float('learning_rate', 0.02, 0.15, log=True),
                'num_leaves': trial.suggest_int('num_leaves', 8, 63),
                'subsample': trial.suggest_float('subsample', 0.6, 1.0),
                'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
                'class_weight': 'balanced',
                'random_state': RANDOM_SEED,
                'verbose': -1,
            }
            model = LGBMClassifier(**params)
            model.fit(X_train, y_train,
                      eval_set=[(X_val, y_val)],
                      callbacks=[early_stopping(30, verbose=False)])
            return 1.0 - model.score(X_val, y_val)

        sampler = TPESampler(seed=RANDOM_SEED)
        study = optuna.create_study(direction='minimize', sampler=sampler)
        study.optimize(lgb_objective, n_trials=args.trials, timeout=args.timeout,
                       show_progress_bar=False)
        lgb_params = {k: v for k, v in study.best_params.items()}
        lgb_params['class_weight'] = 'balanced'
        lgb_params['random_state'] = RANDOM_SEED
        lgb_params['verbose'] = -1

    lgb = LGBMClassifier(**lgb_params)
    lgb.fit(X_train, y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[early_stopping(30, verbose=False)])
    lgb_train = lgb.score(X_train, y_train)
    lgb_val = lgb.score(X_val, y_val)
    lgb_test = lgb.score(X_test, y_test)
    print(f"      LGBM:    Train={lgb_train:.4f}  Val={lgb_val:.4f}  Test={lgb_test:.4f}")

    # XGBoost
    xgb = XGBClassifier(
        n_estimators=300, max_depth=3, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=RANDOM_SEED, verbosity=0, eval_metric='logloss',
    )
    xgb.fit(X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False)
    xgb_train = xgb.score(X_train, y_train)
    xgb_val = xgb.score(X_val, y_val)
    xgb_test = xgb.score(X_test, y_test)
    print(f"      XGBoost: Train={xgb_train:.4f}  Val={xgb_val:.4f}  Test={xgb_test:.4f}")

    # RandomForest
    rf = RandomForestClassifier(
        n_estimators=300, max_depth=8, min_samples_leaf=20,
        class_weight='balanced', random_state=RANDOM_SEED, n_jobs=-1,
    )
    rf.fit(X_train, y_train)
    rf_train = rf.score(X_train, y_train)
    rf_val = rf.score(X_val, y_val)
    rf_test = rf.score(X_test, y_test)
    print(f"      RF:      Train={rf_train:.4f}  Val={rf_val:.4f}  Test={rf_test:.4f}")

    # ── Meta-learner ──
    print("\n[4/5] Training meta-learner (LogisticRegression)...")
    # Build meta-features: base model predictions on val set
    meta_train = np.column_stack([
        lgb.predict_proba(X_train)[:, 1],
        xgb.predict_proba(X_train)[:, 1],
        rf.predict_proba(X_train)[:, 1],
    ])
    meta_val = np.column_stack([
        lgb.predict_proba(X_val)[:, 1],
        xgb.predict_proba(X_val)[:, 1],
        rf.predict_proba(X_val)[:, 1],
    ])
    meta_test = np.column_stack([
        lgb.predict_proba(X_test)[:, 1],
        xgb.predict_proba(X_test)[:, 1],
        rf.predict_proba(X_test)[:, 1],
    ])

    scaler = StandardScaler()
    meta_train_scaled = scaler.fit_transform(meta_train)
    meta_val_scaled = scaler.transform(meta_val)
    meta_test_scaled = scaler.transform(meta_test)

    meta = LogisticRegression(max_iter=1000, random_state=RANDOM_SEED)
    meta.fit(meta_train_scaled, y_train)
    meta_train_acc = meta.score(meta_train_scaled, y_train)
    meta_val_acc = meta.score(meta_val_scaled, y_val)
    meta_test_acc = meta.score(meta_test_scaled, y_test)
    print(f"      Ensemble: Train={meta_train_acc:.4f}  Val={meta_val_acc:.4f}  "
          f"Test={meta_test_acc:.4f}")
    print(f"      Meta weights: {meta.coef_[0]}")

    # ── Simple average baseline ──
    avg_pred = (meta_test[:, 0] + meta_test[:, 1] + meta_test[:, 2]) / 3
    avg_acc = np.mean((avg_pred >= 0.5).astype(int) == y_test)
    print(f"      Simple avg: Test={avg_acc:.4f}")

    # ── Save model ──
    print("\n[5/5] Saving ensemble model...")
    ensemble = {
        "lgb_model": lgb,
        "xgb_model": xgb,
        "rf_model": rf,
        "meta_model": meta,
        "scaler": scaler,
        "feature_names": feature_names,
        "feature_count": len(feature_names),
        "base_models": {
            "LGBM": {"train": lgb_train, "val": lgb_val, "test": lgb_test},
            "XGBoost": {"train": xgb_train, "val": xgb_val, "test": xgb_test},
            "RF": {"train": rf_train, "val": rf_val, "test": rf_test},
        },
        "ensemble": {
            "train_acc": meta_train_acc,
            "val_acc": meta_val_acc,
            "test_acc": meta_test_acc,
        },
        "avg_ensemble": {"test_acc": avg_acc},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "perf_id": "PERF-056",
    }
    joblib.dump(ensemble, ENSEMBLE_OUTPUT)
    print(f"      Saved: {ENSEMBLE_OUTPUT}")

    # ── Verdict ──
    elapsed = _time.time() - t0
    current_acc = current.get('test_acc', 0)
    delta = meta_test_acc - current_acc
    print(f"\n{'='*65}")
    print(f"  PERF-056 VERDICT: {'✅ PASS' if delta > 0 else '❌ FAIL'}")
    print(f"  MTF baseline:   {current_acc:.4f}")
    print(f"  Ensemble:       {meta_test_acc:.4f}  (Δ={delta:+.4f}, {delta*100:+.2f}pp)")
    print(f"  Simple average: {avg_acc:.4f}  (Δ={avg_acc - current_acc:+.4f})")
    print(f"  Best base: LGBM={lgb_test:.4f} XGB={xgb_test:.4f} RF={rf_test:.4f}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"{'='*65}")
