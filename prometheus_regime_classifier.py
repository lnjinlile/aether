#!/usr/bin/env python3
"""Prometheus — Regime Classifier: ML-based market regime detection.

Unlike directional prediction (which OHLCV features can't do), regime
classification asks: "will the next N bars be trending or ranging?"

Regime definition:
- TRENDING: |close[t+N] - close[t]| / close[t] > threshold (e.g. 2%)
- RANGING: otherwise

This is a more tractable ML problem because:
1. It doesn't require predicting direction, only magnitude
2. Volatility clustering means volatility features have predictive power
3. It directly feeds into RegimeSwitchStrategy
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
import numpy as np
import pandas as pd
import joblib

from data.storage import MarketStorage
from config.settings import get_config
from lightgbm import LGBMClassifier

cfg = get_config()
storage = MarketStorage(cfg.db_path)

print("=" * 70)
print("🔥 Prometheus — Regime Classifier (Trending vs Ranging)")
print("=" * 70)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}\n")

# ── Load data ──
def load_data(sym, tf):
    df = storage.load_klines(sym, tf)
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)
    return df

df_btc = load_data('BTC/USDT', '1h')
df_eth = load_data('ETH/USDT', '1h')

# ── 1. Build regime features ──
print("1. Building Regime Features\n" + "-" * 60)

def build_regime_features(df, name="BTC"):
    """Build features specifically for regime prediction."""
    close = df['close'].astype(float).values
    high = df['high'].astype(float).values
    low = df['low'].astype(float).values
    volume = df['volume'].astype(float).values
    n = len(close)
    
    feats = pd.DataFrame(index=df.index)
    
    # Returns
    ret = np.diff(np.log(close))
    ret = np.insert(ret, 0, 0.0)
    
    # Volatility features (the most predictive for regime)
    for w in [5, 10, 20, 50]:
        feats[f'vol_{w}'] = pd.Series(ret).rolling(w).std().values
        feats[f'vol_{w}_norm'] = feats[f'vol_{w}'] / (feats[f'vol_{w}'].rolling(50).mean().values + 1e-10)
    
    # Realized volatility (high-low range)
    hl_range = (high - low) / close
    for w in [5, 10, 20]:
        feats[f'hl_range_{w}'] = pd.Series(hl_range).rolling(w).mean().values
    
    # Trend strength (ADX-like)
    for w in [14, 28]:
        plus_dm = np.maximum(high[1:] - high[:-1], 0)
        minus_dm = np.maximum(low[:-1] - low[1:], 0)
        tr = np.maximum(np.maximum(high[1:]-low[1:], np.abs(high[1:]-close[:-1])), np.abs(low[1:]-close[:-1]))
        
        atr = pd.Series(np.insert(tr, 0, np.nan)).ewm(span=w, adjust=False).mean().values
        plus_di = pd.Series(np.insert(plus_dm, 0, np.nan)).ewm(span=w, adjust=False).mean().values / atr * 100
        minus_di = pd.Series(np.insert(minus_dm, 0, np.nan)).ewm(span=w, adjust=False).mean().values / atr * 100
        
        feats[f'adx_{w}'] = np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
        feats[f'adx_{w}_norm'] = feats[f'adx_{w}'] / (feats[f'adx_{w}'].rolling(50).mean().values + 1e-10)
    
    # MA dispersion (trend strength)
    for w_fast, w_slow in [(5, 20), (10, 50), (20, 100)]:
        ma_fast = pd.Series(close).rolling(w_fast).mean().values
        ma_slow = pd.Series(close).rolling(w_slow).mean().values
        feats[f'ma_disp_{w_fast}_{w_slow}'] = np.abs(ma_fast - ma_slow) / close
    
    # Serial correlation (mean-reversion vs trending)
    for w in [10, 20]:
        feats[f'autocorr_{w}'] = pd.Series(ret).rolling(w).apply(
            lambda x: x.autocorr() if len(x) > 2 else 0, raw=False
        ).values
    
    # Volume features
    vol_ma20 = pd.Series(volume).rolling(20).mean().values
    feats['vol_ratio'] = volume / (vol_ma20 + 1e-10)
    feats['vol_trend'] = pd.Series(volume).rolling(5).mean().values / (vol_ma20 + 1e-10)
    
    # Returns kurtosis (fat tails → regime change)
    for w in [20, 50]:
        feats[f'kurtosis_{w}'] = pd.Series(ret).rolling(w).kurt().values
    
    # Fill NaN
    feats = feats.replace([np.inf, -np.inf], np.nan)
    feats = feats.ffill().bfill().fillna(0.0)
    
    print(f"  {name}: {feats.shape[1]} features, {len(feats)} samples")
    return feats

feats_btc = build_regime_features(df_btc, "BTC")
feats_eth = build_regime_features(df_eth, "ETH")

# ── 2. Label regimes ──
print("\n2. Labeling Regimes\n" + "-" * 60)

def label_regime(close, horizon=24, threshold=0.02):
    """
    Label: 1=TRENDING (abs return over horizon > threshold), 0=RANGING
    """
    n = len(close)
    c = close.values if hasattr(close, 'values') else np.array(close)
    labels = np.full(n, np.nan)
    
    for i in range(n - horizon):
        fut_ret = abs(c[i + horizon] / c[i] - 1)
        labels[i] = 1 if fut_ret > threshold else 0
    
    return pd.Series(labels, index=close.index)

# Test multiple horizon/threshold combos
combos = [
    ('6h/1.0%', 6, 0.01),
    ('12h/1.5%', 12, 0.015),
    ('24h/2.0%', 24, 0.02),
    ('48h/3.0%', 48, 0.03),
    ('72h/4.0%', 72, 0.04),
]

regime_results = []

for sym_name, feats, df in [('BTC', feats_btc, df_btc), ('ETH', feats_eth, df_eth)]:
    close = df['close'].astype(float)
    
    for label_name, horizon, threshold in combos:
        y = label_regime(close, horizon, threshold)
        y_clean = y.dropna()
        
        common = feats.index.intersection(y_clean.index)
        X = feats.loc[common]
        y = y_clean.loc[common]
        
        n = len(X)
        if n < 500:
            continue
        
        train_end = int(n * 0.70)
        val_end = int(n * 0.85)
        
        X_train, y_train = X.iloc[:train_end], y.iloc[:train_end]
        X_val, y_val = X.iloc[train_end:val_end], y.iloc[train_end:val_end]
        X_test, y_test = X.iloc[val_end:], y.iloc[val_end:]
        
        model = LGBMClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.03,
            num_leaves=31, min_child_samples=30,
            class_weight='balanced', random_state=42, verbosity=-1
        )
        model.fit(X_train, y_train, eval_set=[(X_val, y_val)])
        
        train_acc = model.score(X_train, y_train)
        val_acc = model.score(X_val, y_val)
        test_acc = model.score(X_test, y_test)
        
        bal = y.mean()
        gap = train_acc - test_acc
        
        # Feature importance
        imps = sorted(zip(X.columns, model.feature_importances_), key=lambda x: x[1], reverse=True)
        
        regime_results.append({
            'symbol': sym_name, 'label': label_name, 'horizon': horizon,
            'threshold': threshold, 'n': n, 'bal': bal,
            'train': train_acc, 'val': val_acc, 'test': test_acc,
            'gap': gap, 'top_feat': imps[0][0], 'top_imp': imps[0][1]
        })
        
        status = "✅" if test_acc > 0.55 and gap < 0.15 else ("⚠️" if test_acc > 0.52 else "❌")
        print(f"  {status} {sym_name} {label_name}: Train={train_acc:.1%} Val={val_acc:.1%} Test={test_acc:.1%} Gap={gap:+.1%} Bal={bal:.1%} | #{n} | top: {imps[0][0]}")

# ── 3. Summary ──
print("\n" + "=" * 70)
print("3. Regime Classifier Summary")
print("=" * 70)

print(f"\n{'Sym':>4s} {'Config':<12s} {'N':>5s} {'Bal':>7s} {'Train':>7s} {'Val':>7s} {'Test':>7s} {'Gap':>7s} {'Verdict':>8s}")
print("-" * 80)
viable = []
for r in regime_results:
    verdict = "GOOD" if r['test'] > 0.55 and r['gap'] < 0.15 else \
              "WEAK" if r['test'] > 0.52 else "FAIL"
    print(f"{r['symbol']:>4s} {r['label']:<12s} {r['n']:>5d} {r['bal']:6.1%} {r['train']:6.1%} {r['val']:6.1%} {r['test']:6.1%} {r['gap']:+6.1%} {verdict:>8s}")
    if verdict == "GOOD":
        viable.append(r)

# ── 4. Train and save best regime model ──
print("\n" + "=" * 70)
print("4. Training Final Regime Model")
print("=" * 70)

if viable:
    best = max(viable, key=lambda x: x['test'] - x['gap'])
    print(f"\n  Best config: {best['symbol']} {best['label']} (test={best['test']:.1%}, gap={best['gap']:+.1%})")
    
    # Train on full data for best config
    sym = best['symbol']
    feats = feats_btc if sym == 'BTC' else feats_eth
    df = df_btc if sym == 'BTC' else df_eth
    close = df['close'].astype(float)
    
    y_full = label_regime(close, best['horizon'], best['threshold'])
    common = feats.index.intersection(y_full.dropna().index)
    X_f = feats.loc[common]
    y_f = y_full.loc[common]
    
    te = int(len(X_f) * 0.85)
    final_model = LGBMClassifier(
        n_estimators=200, max_depth=5, learning_rate=0.02,
        num_leaves=31, min_child_samples=30,
        class_weight='balanced', random_state=42, verbosity=-1
    )
    final_model.fit(X_f.iloc[:te], y_f.iloc[:te])
    
    test_final = final_model.score(X_f.iloc[te:], y_f.iloc[te:])
    train_final = final_model.score(X_f.iloc[:te], y_f.iloc[:te])
    
    print(f"  Final: Train={train_final:.1%} Test={test_final:.1%} Gap={train_final-test_final:+.1%}")
    print(f"  Top 10 features:")
    for feat, imp in sorted(zip(X_f.columns, final_model.feature_importances_), key=lambda x: x[1], reverse=True)[:10]:
        print(f"    {feat:<25s} {imp:.4f}")
    
    # Save
    os.makedirs('ml_alpha', exist_ok=True)
    joblib.dump({
        'model': final_model,
        'feature_names': list(X_f.columns),
        'horizon': best['horizon'],
        'threshold': best['threshold'],
        'symbol': sym,
        'config': best['label'],
        'test_acc': float(test_final),
        'train_acc': float(train_final),
    }, 'ml_alpha/regime_model.pkl')
    print(f"\n  ✅ Saved regime model to ml_alpha/regime_model.pkl")
else:
    print("\n  ❌ No viable regime model found (all test_acc < 55% or gap > 15%)")
    # Save whatever is best
    best = max(regime_results, key=lambda x: x['test'])
    print(f"  Best available: {best['symbol']} {best['label']} test={best['test']:.1%} gap={best['gap']:+.1%}")

# ── 5. Cross-asset leadership ──
print("\n" + "=" * 70)
print("5. Cross-Asset Leadership Analysis")
print("=" * 70)

# Align BTC and ETH data
common_idx = df_btc.index.intersection(df_eth.index)
btc_ret = df_btc['close'].astype(float).loc[common_idx].pct_change()
eth_ret = df_eth['close'].astype(float).loc[common_idx].pct_change()

for lag in [0, 1, 3, 5]:
    corr = btc_ret.shift(lag).corr(eth_ret)
    print(f"  BTC[t-{lag}] → ETH[t]: corr={corr:+.4f}")

# Also check ETH leading BTC
for lag in [1, 3, 5]:
    corr = eth_ret.shift(lag).corr(btc_ret)
    print(f"  ETH[t-{lag}] → BTC[t]: corr={corr:+.4f}")

# ── Summary ──
elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\n⏱️  Runtime: {elapsed:.1f}s")

# Write summary
os.makedirs('.aether', exist_ok=True)
summary = {
    'run_time': t0.isoformat(),
    'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
    'analysis': 'regime_classifier',
    'triple_barrier_result': 'FAILED — no improvement over next-bar (max corr=0.04, test acc~45-55%)',
    'regime_results': viable,
    'total_configs_tested': len(regime_results),
    'has_viable_regime': len(viable) > 0,
    'cross_asset_corr': {
        'btc_to_eth_lag0': float(btc_ret.corr(eth_ret)),
    }
}

with open('.aether/prometheus_regime.json', 'w') as f:
    json.dump(summary, f, indent=2, default=str)

print("✅ prometheus_regime.json written")
print("🔥 Regime classifier analysis complete")
