#!/usr/bin/env python3
"""
Prometheus — ML Hyperparameter Optimization + Market-State-Aware Tuning

对 ML 策略做参数扫描：
  1. 训练窗口 (60d/90d/120d)
  2. 置信度阈值 (0.52/0.55/0.58/0.60/0.65)
  3. SL/TP 组合
  4. 模型复杂度 (max_depth, n_estimators)
  5. 状态过滤 (仅趋势市交易 / 全状态)

找出最优配置。
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from itertools import product
import numpy as np
import pandas as pd
from collections import defaultdict

from data.storage import MarketStorage
from config.settings import get_config
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from backtest.engine import BacktestEngine
from prometheus_ml_rollback import classify_market_state


def ml_single_backtest(
    df: pd.DataFrame,
    train_end_idx: int,
    test_len: int,
    confidence_threshold: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    n_estimators: int = 200,
    max_depth: int = 3,
    learning_rate: float = 0.03,
    state_filter: str = None,  # 'TREND', 'RANGE', 'VOLATILE', or None (all)
) -> dict:
    """Single train/test split and backtest."""
    engineer = FeatureEngineer()
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    train_df = df.iloc[:train_end_idx]
    test_df = df.iloc[train_end_idx:train_end_idx + test_len]

    if len(train_df) < 100 or len(test_df) < 10:
        return None

    X_train, y_train = engineer.build_features(train_df)
    X_test, y_test = engineer.build_features(test_df)

    if len(X_train) < 100:
        return None

    model = AlphaModel(n_estimators=n_estimators, max_depth=max_depth, learning_rate=learning_rate)
    model.train(X_train, y_train)

    # Market states for test period
    test_states = classify_market_state(test_df)

    signals = np.zeros(len(test_df), dtype=int)
    pos = 0
    entry_price = 0.0

    for i in range(len(test_df)):
        row = X_test.iloc[[i]]
        try:
            prob = float(model.predict(row)[0])
        except Exception:
            continue

        price = float(test_df['close'].iloc[i])
        state = test_states.iloc[i] if i < len(test_states) else 'RANGE'

        # State filter: skip entry if not in allowed state
        allow_entry = (state_filter is None) or (state == state_filter)

        # Exit logic
        if pos == 1:
            if price <= entry_price * (1 - stop_loss_pct):
                signals[i] = 0; pos = 0; continue
            elif price >= entry_price * (1 + take_profit_pct):
                signals[i] = 0; pos = 0; continue
            else:
                signals[i] = 1; continue
        elif pos == -1:
            if price >= entry_price * (1 + stop_loss_pct):
                signals[i] = 0; pos = 0; continue
            elif price <= entry_price * (1 - take_profit_pct):
                signals[i] = 0; pos = 0; continue
            else:
                signals[i] = -1; continue

        # Entry logic (with state filter)
        if pos == 0 and allow_entry:
            if prob > confidence_threshold:
                pos = 1; entry_price = price; signals[i] = 1
            elif prob < (1 - confidence_threshold):
                pos = -1; entry_price = price; signals[i] = -1

    sig_series = pd.Series(signals, index=test_df.index)
    result = engine.run(test_df, sig_series, n_trials=100)
    m = result['metrics']

    return {
        'net_return_pct': m['total_return_pct'],
        'sharpe': m['sharpe_ratio'],
        'max_dd_pct': m['max_drawdown_pct'],
        'win_rate': m['win_rate'],
        'trades': m['total_trades'],
        'profit_factor': m['profit_factor'],
        'train_acc': model.model.score(X_train, y_train),
        'test_acc': model.model.score(X_test, y_test),
        'test_bars': len(test_df),
    }


def run_sweep(df: pd.DataFrame):
    """Run parameter sweep across multiple dimensions."""
    print("🔥 Prometheus — ML Hyperparameter Sweep")
    print("=" * 70)
    t0 = datetime.now(timezone.utc)

    results = []

    # Parameter grid
    train_windows = [
        ('60d', int(len(df) * 0.25)),   # ~25% of data for train; use fixed idx
        ('90d', int(len(df) * 0.35)),
        ('120d', int(len(df) * 0.45)),
    ]
    thresholds = [0.52, 0.55, 0.58, 0.60, 0.65]
    sl_tp_pairs = [
        (0.01, 0.02), (0.01, 0.03), (0.015, 0.03),
        (0.02, 0.04), (0.02, 0.05), (0.03, 0.06),
    ]
    depths = [3, 5]
    n_estimators_list = [100, 200]
    state_filters = [None, 'TREND', 'RANGE']  # None = all states

    # Fix test window at ~15% of data
    test_len = int(len(df) * 0.08)  # ~8% → ~30d of test

    # Limit combos for speed
    total_combos = len(train_windows) * len(thresholds) * len(sl_tp_pairs) * len(depths) * len(n_estimators_list) * len(state_filters)
    print(f"Total combinations: {total_combos}")
    print(f"Data: {len(df)} bars, test_len={test_len} bars\n")

    count = 0
    for (tw_name, train_end), threshold, (sl, tp), depth, n_est, state_f in product(
        train_windows, thresholds, sl_tp_pairs, depths, n_estimators_list, state_filters
    ):
        count += 1
        if count % 100 == 0:
            print(f"  ... {count}/{total_combos}")

        r = ml_single_backtest(
            df, train_end, test_len,
            confidence_threshold=threshold,
            stop_loss_pct=sl, take_profit_pct=tp,
            max_depth=depth, n_estimators=n_est,
            state_filter=state_f,
        )
        if r is None:
            continue

        results.append({
            'train_window': tw_name,
            'threshold': threshold,
            'sl_pct': sl,
            'tp_pct': tp,
            'max_depth': depth,
            'n_estimators': n_est,
            'state_filter': state_f or 'ALL',
            **r,
        })

    # Sort by Sharpe (with trade minimum)
    results.sort(key=lambda x: (x['sharpe'] if x['trades'] >= 3 else -999, x['net_return_pct']), reverse=True)

    print(f"\n{'='*70}")
    print("TOP 20 CONFIGURATIONS (≥3 trades, sorted by Sharpe)")
    print(f"{'='*70}")
    print(f"{'#':>3s} {'Train':>5s} {'Thr':>5s} {'SL%':>5s} {'TP%':>5s} {'Dep':>3s} {'Est':>4s} {'State':>7s} "
          f"{'Net%':>7s} {'Shp':>6s} {'DD%':>5s} {'WR%':>4s} {'#T':>3s} {'PF':>5s} {'TrAcc':>6s} {'TsAcc':>6s}")

    top_shown = 0
    for i, r in enumerate(results):
        if r['trades'] < 3:
            continue
        print(f"{top_shown+1:3d} {r['train_window']:>5s} {r['threshold']:.2f} {r['sl_pct']*100:4.1f}% {r['tp_pct']*100:4.1f}% "
              f"{r['max_depth']:3d} {r['n_estimators']:4d} {r['state_filter']:>7s} "
              f"{r['net_return_pct']:+6.2f}% {r['sharpe']:+5.2f} {r['max_dd_pct']:4.1f}% "
              f"{r['win_rate']:3.0f}% {r['trades']:3d} {r['profit_factor']:4.2f} "
              f"{r['train_acc']:.3f} {r['test_acc']:.3f}")
        top_shown += 1
        if top_shown >= 20:
            break

    if top_shown == 0:
        print("  No viable configs found (all had < 3 trades).")
        print("\n  Showing best by net return (regardless of trades):")
        for i, r in enumerate(results[:10]):
            print(f"  {i+1:2d} {r['train_window']:>5s} thr={r['threshold']:.2f} sl={r['sl_pct']*100:.1f}% "
                  f"tp={r['tp_pct']*100:.1f}% dep={r['max_depth']} est={r['n_estimators']} "
                  f"state={r['state_filter']} net={r['net_return_pct']:+.2f}% shp={r['sharpe']:+.2f} "
                  f"#T={r['trades']}")

    # ── Best by category ──
    print(f"\n{'='*70}")
    print("BEST BY STATE FILTER")
    print(f"{'='*70}")
    for sf in ['ALL', 'TREND', 'RANGE']:
        subset = [r for r in results if r['state_filter'] == sf and r['trades'] >= 3]
        if subset:
            b = subset[0]
            print(f"  {sf:>7s}: train={b['train_window']} thr={b['threshold']:.2f} sl={b['sl_pct']*100:.1f}% tp={b['tp_pct']*100:.1f}% "
                  f"dep={b['max_depth']} est={b['n_estimators']} → net={b['net_return_pct']:+.2f}% shp={b['sharpe']:+.2f} "
                  f"dd={b['max_dd_pct']:.1f}% wr={b['win_rate']:.0f}% #T={b['trades']} PF={b['profit_factor']:.2f}")
        else:
            print(f"  {sf:>7s}: no viable configs")

    # ── Best by train window ──
    print(f"\n{'='*70}")
    print("BEST BY TRAIN WINDOW")
    print(f"{'='*70}")
    for tw in ['60d', '90d', '120d']:
        subset = [r for r in results if r['train_window'] == tw and r['trades'] >= 3]
        if subset:
            b = subset[0]
            print(f"  {tw:>5s}: thr={b['threshold']:.2f} sl={b['sl_pct']*100:.1f}% tp={b['tp_pct']*100:.1f}% "
                  f"state={b['state_filter']} → net={b['net_return_pct']:+.2f}% shp={b['sharpe']:+.2f} "
                  f"dd={b['max_dd_pct']:.1f}% wr={b['win_rate']:.0f}% #T={b['trades']}")

    # ── Overall best ──
    best_viable = [r for r in results if r['trades'] >= 3]
    if best_viable:
        best = best_viable[0]
        print(f"\n{'='*70}")
        print("BEST OVERALL CONFIGURATION")
        print(f"{'='*70}")
        print(f"  Train window:   {best['train_window']}")
        print(f"  Threshold:      {best['threshold']}")
        print(f"  SL / TP:        {best['sl_pct']*100:.1f}% / {best['tp_pct']*100:.1f}%")
        print(f"  Max depth:      {best['max_depth']}")
        print(f"  N estimators:   {best['n_estimators']}")
        print(f"  State filter:   {best['state_filter']}")
        print(f"  Net return:     {best['net_return_pct']:+.2f}%")
        print(f"  Sharpe:         {best['sharpe']:+.2f}")
        print(f"  Max DD:         {best['max_dd_pct']:.1f}%")
        print(f"  Win rate:       {best['win_rate']:.0f}%")
        print(f"  Trades:         {best['trades']}")
        print(f"  Profit factor:  {best['profit_factor']:.2f}")
        print(f"  Train acc:      {best['train_acc']:.3f}")
        print(f"  Test acc:       {best['test_acc']:.3f}")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f"\n⏱️  Runtime: {elapsed:.1f}s")

    # Save
    os.makedirs('.aether', exist_ok=True)
    sweep_data = {
        'run_time': t0.isoformat(),
        'total_combos': total_combos,
        'best_viable': best_viable[0] if best_viable else None,
        'top_20': results[:20],
    }
    with open('.aether/prometheus_ml_sweep.json', 'w') as f:
        json.dump(sweep_data, f, indent=2, default=str)

    return results


if __name__ == '__main__':
    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    df = storage.load_klines('BTC/USDT', '1h')
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)

    results = run_sweep(df)
