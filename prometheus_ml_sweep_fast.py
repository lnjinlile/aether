#!/usr/bin/env python3
"""
Prometheus — Fast ML Hyperparameter Sweep (features pre-built)
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
from itertools import product
import numpy as np
import pandas as pd
from data.storage import MarketStorage
from config.settings import get_config
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from backtest.engine import BacktestEngine
from prometheus_ml_rollback import classify_market_state

cfg = get_config()
storage = MarketStorage(cfg.db_path)

print("🔥 Prometheus — Fast ML Sweep (pre-built features)")
print("=" * 60)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")

# Load data
df = storage.load_klines('BTC/USDT', '1h')
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)
df.sort_index(inplace=True)
print(f"Data: {len(df)} bars, {(df.index[-1]-df.index[0]).days}d")

# Pre-build features ONCE
engineer = FeatureEngineer()
print("Building features (once)...")
# Try to enrich with oracle features (OI/funding/orderbook)
oracle_df = None
try:
    from ml_alpha.oracle_features import merge_oracle_features
    enriched = merge_oracle_features(df, 'BTCUSDT')
    oracle_cols = [c for c in enriched.columns if c not in df.columns]
    if oracle_cols:
        oracle_df = enriched[oracle_cols]
        print(f"  Oracle features loaded: {len(oracle_cols)} columns")
    else:
        print("  No oracle features available (testnet)")
except Exception as e:
    print(f"  Oracle features unavailable: {e}")

X_full, y_full = engineer.build_features(df, oracle_df=oracle_df)
print(f"Features: {X_full.shape}")
oracle_feats_in_X = [c for c in X_full.columns if c.startswith(('ob_','fund_','oi_'))]
if oracle_feats_in_X:
    print(f"  Including oracle: {oracle_feats_in_X}")

# Pre-compute market states
states = classify_market_state(df)
state_series = states.loc[X_full.index]

# Pre-define train/test splits
n = len(X_full)
splits = {
    '60d': int(n * 0.25),
    '90d': int(n * 0.35),
    '120d': int(n * 0.45),
}
test_len = int(n * 0.08)

# Engine
engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

# Parameter grid
thresholds = [0.52, 0.55, 0.58, 0.60, 0.65]
sl_tp_pairs = [(0.01,0.02),(0.01,0.03),(0.015,0.03),(0.02,0.04),(0.02,0.05),(0.03,0.06)]
depths = [3, 5]
n_est_list = [100, 200]
state_filters = [None, 'TREND', 'RANGE']

total = len(splits) * len(thresholds) * len(sl_tp_pairs) * len(depths) * len(n_est_list) * len(state_filters)
print(f"Combos: {total}")

results = []
count = 0

for (tw_name, train_end), threshold, (sl, tp), depth, n_est, state_f in product(
    splits.items(), thresholds, sl_tp_pairs, depths, n_est_list, state_filters
):
    count += 1

    X_train = X_full.iloc[:train_end]
    y_train = y_full.iloc[:train_end]
    X_test = X_full.iloc[train_end:train_end+test_len]
    y_test = y_full.iloc[train_end:train_end+test_len]
    test_df = df.loc[X_test.index]
    test_states = state_series.loc[X_test.index]

    if len(X_train) < 100 or len(X_test) < 20:
        continue

    model = AlphaModel(n_estimators=n_est, max_depth=depth, learning_rate=0.03)
    model.train(X_train, y_train)

    # Generate signals
    signals = np.zeros(len(X_test), dtype=int)
    pos = 0
    entry_price = 0.0

    for i in range(len(X_test)):
        row = X_test.iloc[[i]]
        try:
            prob = float(model.predict(row)[0])
        except Exception:
            continue
        price = float(test_df['close'].iloc[i])
        state = test_states.iloc[i]
        allow = (state_f is None) or (state == state_f)

        if pos == 1:
            if price <= entry_price*(1-sl): signals[i]=0; pos=0; continue
            elif price >= entry_price*(1+tp): signals[i]=0; pos=0; continue
            else: signals[i]=1; continue
        elif pos == -1:
            if price >= entry_price*(1+sl): signals[i]=0; pos=0; continue
            elif price <= entry_price*(1-tp): signals[i]=0; pos=0; continue
            else: signals[i]=-1; continue

        if pos == 0 and allow:
            if prob > threshold: pos=1; entry_price=price; signals[i]=1
            elif prob < (1-threshold): pos=-1; entry_price=price; signals[i]=-1

    sig_series = pd.Series(signals, index=X_test.index)
    result = engine.run(test_df, sig_series, n_trials=90)
    m = result['metrics']

    results.append({
        'train_window': tw_name, 'threshold': threshold, 'sl_pct': sl, 'tp_pct': tp,
        'max_depth': depth, 'n_estimators': n_est, 'state_filter': state_f or 'ALL',
        'net_return_pct': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
        'max_dd_pct': m['max_drawdown_pct'], 'win_rate': m['win_rate'],
        'trades': m['total_trades'], 'profit_factor': m['profit_factor'],
        'train_acc': model.model.score(X_train, y_train),
        'test_acc': model.model.score(X_test, y_test),
    })

# Sort
results.sort(key=lambda x: (x['sharpe'] if x['trades']>=3 else -999, x['net_return_pct']), reverse=True)

print(f"\n{'='*70}")
print("TOP 20 (≥3 trades, sorted by Sharpe)")
print(f"{'='*70}")
print(f"{'#':>3s} {'Train':>5s} {'Thr':>5s} {'SL%':>5s} {'TP%':>5s} {'D':>2s} {'Est':>4s} {'State':>7s} "
      f"{'Net%':>7s} {'Shp':>6s} {'DD%':>5s} {'WR%':>4s} {'#T':>3s} {'PF':>5s} {'TrA':>5s} {'TsA':>5s}")

shown = 0
for r in results:
    if r['trades'] < 3: continue
    print(f"{shown+1:3d} {r['train_window']:>5s} {r['threshold']:.2f} {r['sl_pct']*100:4.1f}% {r['tp_pct']*100:4.1f}% "
          f"{r['max_depth']:2d} {r['n_estimators']:4d} {r['state_filter']:>7s} "
          f"{r['net_return_pct']:+6.2f}% {r['sharpe']:+5.2f} {r['max_dd_pct']:4.1f}% "
          f"{r['win_rate']:3.0f}% {r['trades']:3d} {r['profit_factor']:4.2f} "
          f"{r['train_acc']:.3f} {r['test_acc']:.3f}")
    shown += 1
    if shown >= 20: break

if shown == 0:
    print("  No viable configs (≥3 trades). Showing best by net return:")
    for i, r in enumerate(results[:10]):
        print(f"  {i+1:2d} {r['train_window']} thr={r['threshold']:.2f} sl={r['sl_pct']*100:.1f}% tp={r['tp_pct']*100:.1f}% "
              f"net={r['net_return_pct']:+.1f}% shp={r['sharpe']:+.2f} #T={r['trades']}")

# Best by state filter
print(f"\n{'='*70}")
print("BEST BY STATE FILTER (≥3 trades)")
print(f"{'='*70}")
for sf in ['ALL', 'TREND', 'RANGE']:
    sub = [r for r in results if r['state_filter']==sf and r['trades']>=3]
    if sub:
        b = sub[0]
        print(f"  {sf:>7s}: train={b['train_window']} thr={b['threshold']:.2f} SL={b['sl_pct']*100:.1f}% TP={b['tp_pct']*100:.1f}% "
              f"d={b['max_depth']} e={b['n_estimators']} → net={b['net_return_pct']:+.1f}% shp={b['sharpe']:+.2f} "
              f"dd={b['max_dd_pct']:.1f}% wr={b['win_rate']:.0f}% #T={b['trades']} PF={b['profit_factor']:.2f}")
    else:
        print(f"  {sf:>7s}: no viable configs")

# Best overall
viable = [r for r in results if r['trades']>=3]
if viable:
    best = viable[0]
    print(f"\n{'='*70}")
    print("🏆 BEST OVERALL")
    print(f"{'='*70}")
    print(f"  Train: {best['train_window']} | Thr: {best['threshold']} | SL: {best['sl_pct']*100:.1f}% | TP: {best['tp_pct']*100:.1f}%")
    print(f"  Depth: {best['max_depth']} | Est: {best['n_estimators']} | State: {best['state_filter']}")
    print(f"  Net: {best['net_return_pct']:+.2f}% | Sharpe: {best['sharpe']:+.2f} | DD: {best['max_dd_pct']:.1f}%")
    print(f"  WR: {best['win_rate']:.0f}% | Trades: {best['trades']} | PF: {best['profit_factor']:.2f}")
    print(f"  Train acc: {best['train_acc']:.3f} | Test acc: {best['test_acc']:.3f}")

# Determine if ML strategy should be enabled
if best['sharpe'] > 0.5 and best['net_return_pct'] > 0:
    verdict = "PROMISING — 建议启用 MLAlpha_BTC"
    action = "ENABLE"
elif best['sharpe'] > 0 and best['net_return_pct'] > -5:
    verdict = "MARGINAL — 暂不启用，需更多优化"
    action = "KEEP_DISABLED"
else:
    verdict = "NOT_READY — ML策略当前不可用"
    action = "KEEP_DISABLED"

print(f"\n  Verdict: {verdict}")

# Save
elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\n⏱️  {elapsed:.1f}s")

os.makedirs('.aether', exist_ok=True)
sweep_data = {
    'run_time': t0.isoformat(),
    'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
    'elapsed_s': round(elapsed, 1),
    'total_combos': total,
    'best_viable': best if viable else None,
    'verdict': verdict,
    'action': action,
    'top_20': results[:20],
}
with open('.aether/prometheus_ml_sweep.json', 'w') as f:
    json.dump(sweep_data, f, indent=2, default=str)
print("💾 prometheus_ml_sweep.json written")
