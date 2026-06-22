#!/usr/bin/env python3
"""Prometheus — Triple-Barrier ML Labeling & Retraining

Implements the triple-barrier method for labeling:
- Upper barrier (take-profit): label=1 (LONG wins)
- Lower barrier (stop-loss): label=0 (SHORT wins) 
- Time barrier (vertical): label based on sign of return

Replaces the naive next-bar binary classification with a more realistic
trading-oriented labeling scheme, then retrains and validates.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
import numpy as np
import pandas as pd
import joblib

from data.storage import MarketStorage
from config.settings import get_config
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from ml_alpha.oracle_features import merge_oracle_features, get_oracle_feature_names

cfg = get_config()
storage = MarketStorage(cfg.db_path)

print("=" * 70)
print("🔥 Prometheus — Triple-Barrier ML Validation")
print("=" * 70)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}\n")

# ── Load data ──
df = storage.load_klines('BTC/USDT', '1h')
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)
df.sort_index(inplace=True)
print(f"Data: {len(df)} bars, {(df.index[-1]-df.index[0]).days}d")

close = df['close'].astype(float)
high = df['high'].astype(float)
low = df['low'].astype(float)

# Load oracle features once for all models
oracle_df = None
try:
    enriched = merge_oracle_features(df, 'BTCUSDT')
    oracle_cols = [c for c in enriched.columns if c not in df.columns]
    if oracle_cols:
        oracle_df = enriched[oracle_cols]
        print(f"Oracle features: {len(oracle_cols)} columns loaded")
except Exception as e:
    print(f"Oracle features unavailable: {e}")

# ── 1. Triple-Barrier Labeling ──
print("\n" + "-" * 60)
print("1. Triple-Barrier Labeling")
print("-" * 60)

def triple_barrier_labels(close, high, low, tp_pct=0.02, sl_pct=0.02, max_bars=24):
    """
    Label each bar based on which barrier is hit first within max_bars.
    
    Returns:
        1 = upper barrier hit first (profitable LONG)
        0 = lower barrier hit first (profitable SHORT)
    
    Uses .iloc for position-based access since index is datetime.
    """
    n = len(close)
    labels = np.full(n, np.nan)
    
    c = close.values if hasattr(close, 'values') else np.array(close)
    h = high.values if hasattr(high, 'values') else np.array(high)
    l = low.values if hasattr(low, 'values') else np.array(low)
    
    for i in range(n - max_bars):
        entry = c[i]
        upper = entry * (1 + tp_pct)
        lower = entry * (1 - sl_pct)
        
        found = False
        for j in range(i + 1, min(i + max_bars + 1, n)):
            if h[j] >= upper:
                labels[i] = 1  # upper barrier hit → LONG signal
                found = True
                break
            elif l[j] <= lower:
                labels[i] = 0  # lower barrier hit → SHORT signal
                found = True
                break
        
        if not found:
            # Time barrier: label by return sign at max_bars
            if i + max_bars < n:
                ret = c[i + max_bars] / entry - 1
                labels[i] = 1 if ret > 0 else 0
    
    return pd.Series(labels, index=close.index)

# Test different barrier configs
configs = [
    ('Conservative', 0.015, 0.015, 24),   # 1.5% TP/SL, 24h timeout
    ('Moderate', 0.02, 0.02, 24),          # 2% TP/SL, 24h
    ('Aggressive', 0.03, 0.02, 48),        # 3% TP, 2% SL, 48h
    ('Wide', 0.04, 0.02, 72),              # 4% TP, 2% SL, 72h
]

results = []

for name, tp, sl, max_bars in configs:
    y = triple_barrier_labels(close, high, low, tp, sl, max_bars)
    y_clean = y.dropna()
    
    # Class balance
    bal = y_clean.mean()
    n_up = int(y_clean.sum())
    n_total = len(y_clean)
    
    # Feature-train split
    engineer = FeatureEngineer()
    X, _ = engineer.build_features(df, oracle_df=oracle_df)
    
    common = X.index.intersection(y_clean.index)
    X_use = X.loc[common]
    y_use = y_clean.loc[common]
    
    n = len(X_use)
    if n < 500:
        print(f"  {name}: insufficient data ({n})")
        continue
    
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    
    X_train, y_train = X_use.iloc[:train_end], y_use.iloc[:train_end]
    X_val, y_val = X_use.iloc[train_end:val_end], y_use.iloc[train_end:val_end]
    X_test, y_test = X_use.iloc[val_end:], y_use.iloc[val_end:]
    
    # Train
    model = AlphaModel(n_estimators=100, max_depth=3, learning_rate=0.03)
    model.train(X_train, y_train, X_val, y_val)
    
    train_acc = model.model.score(X_train, y_train)
    val_acc = model.model.score(X_val, y_val)
    test_acc = model.model.score(X_test, y_test)
    gap = train_acc - test_acc
    
    # Feature correlation with triple-barrier labels
    corrs = []
    for col in X_use.columns:
        corr = X_use[col].corr(y_use.astype(float))
        corrs.append((col, abs(corr)))
    corrs.sort(key=lambda x: x[1], reverse=True)
    max_corr = corrs[0][1] if corrs else 0
    
    results.append({
        'name': name, 'tp': tp, 'sl': sl, 'max_bars': max_bars,
        'n': n, 'bal': bal, 'n_up': n_up, 'n_total': n_total,
        'train_acc': train_acc, 'val_acc': val_acc, 'test_acc': test_acc,
        'gap': gap, 'max_corr': max_corr, 'top_feat': corrs[0][0] if corrs else 'N/A'
    })
    
    print(f"\n  [{name}] TP={tp*100:.1f}% SL={sl*100:.1f}% Timeout={max_bars}h")
    print(f"    Samples: {n} | Balance: {bal:.1%} ({n_up}/{n_total} UP)")
    print(f"    Train={train_acc:.1%} Val={val_acc:.1%} Test={test_acc:.1%} Gap={gap:+.1%}")
    print(f"    Max feature corr: {max_corr:.4f} ({corrs[0][0]})")

# ── Summary table ──
print("\n" + "=" * 80)
print("Triple-Barrier Results Summary")
print("=" * 80)
print(f"{'Config':<14s} {'N':>5s} {'Bal':>7s} {'Train':>7s} {'Val':>7s} {'Test':>7s} {'Gap':>7s} {'MaxCorr':>8s}")
print("-" * 75)
for r in results:
    print(f"{r['name']:<14s} {r['n']:>5d} {r['bal']:6.1%} {r['train_acc']:6.1%} {r['val_acc']:6.1%} {r['test_acc']:6.1%} {r['gap']:+6.1%} {r['max_corr']:7.4f}")

# ── 2. Compare: Next-bar vs Triple-barrier feature importance ──
print("\n" + "=" * 80)
print("2. Feature Importance: Next-Bar vs Triple-Barrier (Moderate)")
print("=" * 80)

# Next-bar model
engineer = FeatureEngineer()
X_nb, y_nb = engineer.build_features(df, oracle_df=oracle_df)
common_nb = X_nb.index.intersection(y_nb.index)
X_nb = X_nb.loc[common_nb]
y_nb = y_nb.loc[common_nb]

n_nb = len(X_nb)
te_nb = int(n_nb * 0.70)
ve_nb = int(n_nb * 0.85)

model_nb = AlphaModel(n_estimators=100, max_depth=3, learning_rate=0.03)
model_nb.train(X_nb.iloc[:te_nb], y_nb.iloc[:te_nb], X_nb.iloc[te_nb:ve_nb], y_nb.iloc[te_nb:ve_nb])

# Triple-barrier model
y_tb = triple_barrier_labels(close, high, low, 0.02, 0.02, 24)
common_tb = X_nb.index.intersection(y_tb.dropna().index)
X_tb = X_nb.loc[common_tb]
y_tb = y_tb.loc[common_tb].dropna()

n_tb = len(X_tb)
te_tb = int(n_tb * 0.70)
ve_tb = int(n_tb * 0.85)

model_tb = AlphaModel(n_estimators=100, max_depth=3, learning_rate=0.03)
model_tb.train(X_tb.iloc[:te_tb], y_tb.iloc[:te_tb], X_tb.iloc[te_tb:ve_tb], y_tb.iloc[te_tb:ve_tb])

print(f"\n{'Feature':<20s} {'Next-Bar Imp':>14s} {'Triple-Bar Imp':>16s}")
print("-" * 55)
nb_imp = dict(model_nb.get_feature_importance())
tb_imp = dict(model_tb.get_feature_importance())
all_features = sorted(set(list(nb_imp.keys()) + list(tb_imp.keys())))
for feat in all_features[:15]:
    nb_val = nb_imp.get(feat, 0)
    tb_val = tb_imp.get(feat, 0)
    print(f"{feat:<20s} {nb_val:14.4f} {tb_val:16.4f}")

# ── 3. Backtest comparison ──
print("\n" + "=" * 80)
print("3. Backtest: Next-Bar vs Triple-Barrier on Test Data")
print("=" * 80)

from backtest.engine import BacktestEngine

for label_name, model_obj, X_data, y_data, te in [
    ('Next-Bar', model_nb, X_nb, y_nb, te_nb),
    ('Triple-Barrier', model_tb, X_tb, y_tb, te_tb),
]:
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    
    X_test = X_data.iloc[te:]
    test_close = close.loc[X_test.index]
    n_test = len(X_test)
    
    signals = np.zeros(n_test, dtype=int)
    pos = 0
    entry_price = 0.0
    threshold = 0.55
    sl, tp = 0.02, 0.04
    
    for i in range(n_test):
        row = X_test.iloc[[i]]
        price = float(test_close.iloc[i])
        try:
            prob = float(model_obj.predict(row)[0])
        except:
            if pos != 0:
                signals[i] = pos
            continue
        
        # Exit
        if pos == 1:
            if price <= entry_price * (1 - sl) or price >= entry_price * (1 + tp):
                pos = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if price >= entry_price * (1 + sl) or price <= entry_price * (1 - tp):
                pos = 0; continue
            signals[i] = -1; continue
        
        # Entry
        if pos == 0:
            if prob > threshold:
                pos = 1; entry_price = price; signals[i] = 1
            elif prob < (1 - threshold):
                pos = -1; entry_price = price; signals[i] = -1
    
    sig_series = pd.Series(signals, index=X_test.index)
    test_df = df.loc[X_test.index]
    bt = engine.run(test_df, sig_series)
    m = bt['metrics']
    
    print(f"\n  [{label_name}] Test set ({n_test} bars):")
    print(f"    Return={m['total_return_pct']:+.2f}% Sharpe={m['sharpe_ratio']:+.3f} MaxDD={m['max_drawdown_pct']:.1f}%")
    print(f"    Trades={m['total_trades']} WinRate={m['win_rate']:.0f}% PF={m['profit_factor']:.2f}")
    print(f"    AvgWin={m['avg_win_pct']:+.2f}% AvgLoss={m['avg_loss_pct']:+.2f}%")

# ── 4. Retrain and save model with best labeling ──
print("\n" + "=" * 80)
print("4. Retraining Model with Triple-Barrier Labels")
print("=" * 80)

# Use full data for final model
y_final = triple_barrier_labels(close, high, low, 0.02, 0.02, 24)
common_final = X_nb.index.intersection(y_final.dropna().index)
X_final = X_nb.loc[common_final]
y_final = y_final.loc[common_final]

n_final = len(X_final)
te_final = int(n_final * 0.85)
X_train_f = X_final.iloc[:te_final]
y_train_f = y_final.iloc[:te_final]
X_test_f = X_final.iloc[te_final:]
y_test_f = y_final.iloc[te_final:]

model_final = AlphaModel(n_estimators=200, max_depth=4, learning_rate=0.02, 
                         num_leaves=31, min_child_samples=30)
model_final.train(X_train_f, y_train_f)

test_acc_f = model_final.model.score(X_test_f, y_test_f)
train_acc_f = model_final.model.score(X_train_f, y_train_f)

print(f"  Full model: Train={train_acc_f:.1%} Test={test_acc_f:.1%} Gap={train_acc_f-test_acc_f:+.1%}")
print(f"  Features: {len(X_train_f.columns)} | Samples: {n_final}")
print(f"  Class balance: {y_final.mean():.1%}")

# Save
backup_path = 'ml_alpha/model_prev.pkl'
if os.path.exists('ml_alpha/model.pkl'):
    os.rename('ml_alpha/model.pkl', backup_path)
    print(f"  Backed up old model to {backup_path}")

new_path = 'ml_alpha/model.pkl'
model_final.save(new_path)
print(f"  Saved new model to {new_path}")

# Feature importance
print("\n  Top 10 Features (triple-barrier):")
for feat, imp in model_final.get_feature_importance():
    print(f"    {feat:<20s} {imp:.4f}")

# ── Summary ──
elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\n⏱️  Runtime: {elapsed:.1f}s")

# Write state file
os.makedirs('.aether', exist_ok=True)
prom_data = {
    'run_time': t0.isoformat(),
    'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
    'model_type': 'triple_barrier',
    'test_accuracy': float(test_acc_f),
    'train_accuracy': float(train_acc_f),
    'gap': float(train_acc_f - test_acc_f),
    'n_samples': n_final,
    'class_balance': float(y_final.mean()),
    'triple_barrier_results': [
        {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) 
         for k, v in r.items()} 
        for r in results
    ],
}

# Write to state/prometheus.json (merge ML results)
state_dir = '.aether/state'
os.makedirs(state_dir, exist_ok=True)
state_path = os.path.join(state_dir, 'prometheus.json')
existing = {}
if os.path.exists(state_path):
    try:
        with open(state_path) as f:
            existing = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
existing['triple_barrier'] = prom_data
existing['triple_barrier_verdict'] = f"DEAD_END ({test_acc_f*100:.1f}% test acc)"
existing['_updated_at'] = datetime.now(timezone.utc).isoformat()
with open(state_path, 'w') as f:
    json.dump(existing, f, indent=2, default=str)

# Also write legacy file for backward compat
os.makedirs('.aether', exist_ok=True)
with open('.aether/prometheus.json', 'w') as f:
    json.dump(prom_data, f, indent=2, default=str)

print("✅ prometheus.json written")
print("🔥 Triple-barrier retraining complete")
