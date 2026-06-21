#!/usr/bin/env python3
"""
Prometheus — Oracle Feature Impact Analysis (v2)
Compares ML model WITH vs WITHOUT oracle features on the overlapping data period.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

from data.storage import MarketStorage
from config.settings import get_config
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from ml_alpha.oracle_features import merge_oracle_features, get_oracle_feature_names
from backtest.engine import BacktestEngine

cfg = get_config()
storage = MarketStorage(cfg.db_path)

print("=" * 80)
print("🔥 Prometheus — Oracle Feature Impact Analysis v2")
print("=" * 80)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}\n")

# ── Load BTC 1h data ──
df = storage.load_klines('BTC/USDT', '1h')
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
df.set_index('open_time', inplace=True)
df.sort_index(inplace=True)
print(f"Kline data: {len(df)} bars, range={df.index[0]} to {df.index[-1]}")

# ── Merge oracle features ──
try:
    enriched = merge_oracle_features(df, 'BTCUSDT')
    oracle_cols = [c for c in enriched.columns if c not in df.columns]
    oracle_df = enriched[oracle_cols] if oracle_cols else None
    # Find where oracle data is non-NaN
    if oracle_df is not None:
        non_nan_mask = oracle_df.notna().any(axis=1)
        oracle_start = df.index[non_nan_mask].min() if non_nan_mask.any() else None
        oracle_end = df.index[non_nan_mask].max() if non_nan_mask.any() else None
        n_oracle = non_nan_mask.sum()
        print(f"Oracle data period: {oracle_start} to {oracle_end} ({n_oracle} bars with oracle data)")
        print(f"Oracle columns: {oracle_cols}")
        
        # Use only bars where oracle data is available
        df_subset = df.loc[non_nan_mask].copy()
        oracle_df_subset = oracle_df.loc[non_nan_mask].copy()
        print(f"Subset for analysis: {len(df_subset)} bars")
    else:
        df_subset = df
        oracle_df_subset = None
except Exception as e:
    print(f"⚠️ Oracle merge failed: {e}")
    import traceback; traceback.print_exc()
    df_subset = df
    oracle_df_subset = None

# ── Build features ──
engineer = FeatureEngineer()

# Baseline
X_base, y_base = engineer.build_features(df_subset)
print(f"\nBASELINE features: {X_base.shape[1]} cols, {X_base.shape[0]} samples")

# Enhanced
if oracle_df_subset is not None and not oracle_df_subset.empty and len(df_subset) > 50:
    X_enh, y_enh = engineer.build_features(df_subset, oracle_df=oracle_df_subset)
    # Check which oracle cols made it
    oracle_cols_in_X = [c for c in X_enh.columns if c.startswith(('ob_','fund_','oi_'))]
    print(f"ENHANCED features: {X_enh.shape[1]} cols ({len(oracle_cols_in_X)} oracle), {X_enh.shape[0]} samples")
    print(f"Oracle cols in X: {oracle_cols_in_X}")
else:
    X_enh = None
    print("ENHANCED: insufficient data for oracle features")

# ── Model comparison ──
print("\n" + "=" * 80)
print("MODEL COMPARISON")
print("=" * 80)

for label, X_full, y_full in [
    ('BASELINE (no oracle)', X_base, y_base),
    ('ORACLE-ENHANCED', X_enh, y_enh if X_enh is not None else None),
]:
    if X_full is None or len(X_full) < 100:
        print(f"\n  [{label}] ⚠️ Insufficient data ({len(X_full) if X_full is not None else 0} samples)")
        continue

    n = len(X_full)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)

    X_train = X_full.iloc[:train_end]
    y_train = y_full.iloc[:train_end]
    X_val = X_full.iloc[train_end:val_end]
    y_val = y_full.iloc[train_end:val_end]
    X_test = X_full.iloc[val_end:]
    y_test = y_full.iloc[val_end:]

    model = AlphaModel(n_estimators=100, max_depth=3, learning_rate=0.03)
    model.train(X_train, y_train, X_val, y_val)

    train_acc = model.model.score(X_train, y_train)
    val_acc = model.model.score(X_val, y_val)
    test_acc = model.model.score(X_test, y_test) if len(X_test) > 0 else 0
    gap = train_acc - test_acc

    top_features = model.get_feature_importance()

    print(f"\n  [{label}]")
    print(f"    Samples: train={len(X_train)}, val={len(X_val)}, test={len(X_test)}")
    print(f"    Accuracy: train={train_acc:.1%}, val={val_acc:.1%}, test={test_acc:.1%}, gap={gap:+.1%}")
    print(f"    Top 5 features:")
    for feat, imp in top_features[:5]:
        oracle_mark = " ★" if feat.startswith(('ob_','fund_','oi_')) else ""
        print(f"      {feat:<30s} {imp:.4f}{oracle_mark}")

# ── Feature correlation ──
if X_enh is not None and len(X_enh) > 50:
    print("\n" + "=" * 80)
    print("ORACLE FEATURE → TARGET CORRELATION")
    print("=" * 80)
    common = X_enh.index.intersection(y_enh.index)
    X_c = X_enh.loc[common]
    y_c = y_enh.loc[common]
    oracle_feat_names = [c for c in X_enh.columns if c.startswith(('ob_','fund_','oi_'))]
    if oracle_feat_names and len(X_c) > 10:
        print(f"\n  {'Feature':<35s} {'|Corr|':>10s} {'Corr':>12s} {'P-value':>10s}")
        print("  " + "-" * 70)
        from scipy import stats
        for feat in oracle_feat_names:
            if feat in X_c.columns:
                valid = X_c[feat].notna() & y_c.notna()
                if valid.sum() > 5:
                    corr = X_c.loc[valid, feat].corr(y_c.loc[valid])
                    try:
                        _, pval = stats.pearsonr(X_c.loc[valid, feat], y_c.loc[valid])
                    except:
                        pval = 1.0
                    sig = " **" if pval < 0.01 else " *" if pval < 0.05 else ""
                    print(f"  {feat:<35s} {abs(corr):9.4f}  {corr:+11.4f} {pval:9.4f}{sig}")
                else:
                    print(f"  {feat:<35s} {'N/A':>10s}")
    else:
        print("  No oracle features with valid data")

# ── Verdict ──
print("\n" + "=" * 80)
print("VERDICT")
print("=" * 80)

if X_enh is not None and len(X_enh) > 50 and oracle_cols_in_X:
    # Count oracle features in top importance
    oracle_in_top = sum(1 for f, _ in top_features if f in oracle_cols_in_X)
    print(f"  Oracle features in model: {len(oracle_cols_in_X)}")
    print(f"  Oracle features in top importance: {oracle_in_top}/{len(top_features)}")
    if oracle_in_top > 0:
        print(f"  ✅ Oracle features contribute to ML predictions — merge pipeline working")
    else:
        print(f"  ⚠️ Oracle features loaded but not in top predictors — may need more data")
    print(f"  ℹ️  Note: testnet data is sparse (10-20 rows each). Production will have richer history.")
else:
    print(f"  ⚠️ Oracle feature pipeline needs more data to evaluate (currently {len(df_subset)} bars with oracle coverage)")

elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
os.makedirs('.aether', exist_ok=True)
print(f"\n⏱️  Runtime: {elapsed:.1f}s")
