#!/usr/bin/env python3
"""Prometheus — ML WF Validation: Test multi-horizon labels + feature analysis"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
import numpy as np
import pandas as pd

from data.storage import MarketStorage
from config.settings import get_config
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from backtest.engine import BacktestEngine

cfg = get_config()
storage = MarketStorage(cfg.db_path)

print("🔥 Prometheus — ML WF Multi-Horizon Validation")
print("=" * 70)
t0 = datetime.now(timezone.utc)

# Load BTC 1h data
df = storage.load_klines('BTC/USDT', '1h')
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)
df.sort_index(inplace=True)
print(f"Data: {len(df)} bars, {(df.index[-1]-df.index[0]).days}d")

engineer = FeatureEngineer()
X_full, _ = engineer.build_features(df)
print(f"Features: {X_full.shape}")

# Test different prediction horizons
# Instead of binary next-bar direction, predict k-bar forward return
horizons = [1, 3, 5, 10]
close = df['close'].astype(float).loc[X_full.index]

results = []

for horizon in horizons:
    # Build multi-horizon target: 1 if k-bar forward return > 0, else 0
    future_ret = np.log(close.shift(-horizon) / close)
    y = (future_ret > 0).astype(int)
    y = y.dropna()
    
    common_idx = X_full.index.intersection(y.index)
    X = X_full.loc[common_idx]
    y = y.loc[common_idx]
    
    n = len(X)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    
    X_train, y_train = X.iloc[:train_end], y.iloc[:train_end]
    X_val, y_val = X.iloc[train_end:val_end], y.iloc[train_end:val_end]
    X_test, y_test = X.iloc[val_end:], y.iloc[val_end:]
    
    if len(X_train) < 200:
        continue
    
    model = AlphaModel(n_estimators=100, max_depth=3, learning_rate=0.03)
    model.train(X_train, y_train, X_val, y_val)
    
    train_acc = model.model.score(X_train, y_train)
    val_acc = model.model.score(X_val, y_val)
    test_acc = model.model.score(X_test, y_test)
    
    # Quick backtest on test set
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    test_df = df.loc[X_test.index]
    signals = np.zeros(len(X_test), dtype=int)
    pos = 0
    entry_price = 0.0
    threshold = 0.55
    sl, tp = 0.02, 0.04
    
    for i in range(len(X_test)):
        row = X_test.iloc[[i]]
        try:
            prob = float(model.predict(row)[0])
        except:
            continue
        price = float(test_df['close'].iloc[i])
        
        if pos == 1:
            if price <= entry_price * (1 - sl):
                pos = 0; continue
            elif price >= entry_price * (1 + tp):
                pos = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if price >= entry_price * (1 + sl):
                pos = 0; continue
            elif price <= entry_price * (1 - tp):
                pos = 0; continue
            signals[i] = -1; continue
        
        if pos == 0:
            if prob > threshold:
                pos = 1; entry_price = price; signals[i] = 1
            elif prob < (1 - threshold):
                pos = -1; entry_price = price; signals[i] = -1
    
    sig_series = pd.Series(signals, index=X_test.index)
    bt = engine.run(test_df, sig_series)
    m = bt['metrics']
    
    class_bal = y.mean()
    
    results.append({
        'horizon': horizon,
        'n_samples': n,
        'train_acc': train_acc,
        'val_acc': val_acc,
        'test_acc': test_acc,
        'class_bal': class_bal,
        'net_return': m['total_return_pct'],
        'sharpe': m['sharpe_ratio'],
        'max_dd': m['max_drawdown_pct'],
        'win_rate': m['win_rate'],
        'trades': m['total_trades'],
        'overfit_gap': train_acc - test_acc,
    })

print(f"\n{'='*80}")
print(f"{'Horizon':>8s} {'N':>5s} {'Train':>7s} {'Val':>7s} {'Test':>7s} {'Gap':>7s} {'Bal':>7s} {'Net%':>8s} {'Shp':>7s} {'DD%':>7s} {'WR%':>5s} {'#T':>4s}")
print(f"{'='*80}")
for r in results:
    print(f"{r['horizon']:>4d}bar {r['n_samples']:>5d} {r['train_acc']:6.1%} {r['val_acc']:6.1%} {r['test_acc']:6.1%} {r['overfit_gap']:+6.1%} {r['class_bal']:6.1%} {r['net_return']:+7.2f}% {r['sharpe']:+6.2f} {r['max_dd']:+6.2f}% {r['win_rate']:+4.0f}% {r['trades']:>4d}")

# Feature correlation with target
print(f"\n{'='*80}")
print("Feature-Target Analysis (1h BTC, k=5 forward return)")
print(f"{'='*80}")

future_ret_5 = np.log(close.shift(-5) / close)
y5 = (future_ret_5 > 0).astype(int)
common_idx = X_full.index.intersection(y5.dropna().index)
X5 = X_full.loc[common_idx]
y5 = y5.loc[common_idx]

print(f"\n{'Feature':<25s} {'Corr w/ target':>15s} {'|Corr|':>10s}")
print("-" * 55)
corrs = []
for col in X5.columns:
    corr = X5[col].corr(y5.astype(float))
    corrs.append((col, corr, abs(corr)))

corrs.sort(key=lambda x: x[2], reverse=True)
for name, corr, abs_corr in corrs[:12]:
    print(f"{name:<25s} {corr:>+14.4f} {abs_corr:>9.4f}")

elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\n⏱️  {elapsed:.1f}s")
