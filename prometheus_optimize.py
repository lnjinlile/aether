#!/usr/bin/env python3
"""
Prometheus — System Optimization Engine
Runs comprehensive parameter sweeps on full data range to find optimal configs.
"""
import sys, os, json, yaml
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, '/home/rinnen/binance_quant')

import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine

# Import signal generators from athena_backtest
from athena_backtest import trendfollow_signals, rsi_mr_signals, ma_cross_signals

cfg = get_config()
storage = MarketStorage(cfg.db_path)
engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

print("🔥 Prometheus — Full-Range Parameter Optimization")
print("=" * 70)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
print()

# ══════════════════════════════════════════════════════════════════════
# Load full data range
# ══════════════════════════════════════════════════════════════════════

data = {}
for sym in ['BTC/USDT', 'ETH/USDT']:
    for tf in ['15m', '1h']:
        df = storage.load_klines(sym, tf)
        if df.empty:
            print(f"  ⚠️ {sym} {tf}: no data")
            continue
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('open_time', inplace=True)
        df.sort_index(inplace=True)
        data[(sym, tf)] = df
        days = (df.index[-1] - df.index[0]).days
        print(f"  📊 {sym:10s} {tf:4s}: {len(df):5d} bars, {days}d "
              f"[{df.index[0].strftime('%m/%d')} → {df.index[-1].strftime('%m/%d')}]")

# ══════════════════════════════════════════════════════════════════════
# 1. TrendFollow Parameter Sweep — BTC 1h (90d data)
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("1. TRENDFOLLOW SWEEP — BTC/USDT 1h (90-day range)")
print("=" * 80)

tf_sweep_btc_1h = []
df = data.get(('BTC/USDT', '1h'))
if df is not None and len(df) > 100:
    n_trials = sum(1 for ema in [20,30,50,75,100,150,200] for sl in [0.01,0.015,0.02,0.025,0.03,0.04,0.05] for tp in [0.02,0.03,0.04,0.05,0.06,0.08,0.10] for cd in [5,8,10,15] if tp > sl)
    for ema in [20, 30, 50, 75, 100, 150, 200]:
        for sl in [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
            for tp in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
                if tp <= sl:
                    continue
                for cd in [5, 8, 10, 15]:
                    sig = trendfollow_signals(df, ema, sl, tp, cd)
                    res = engine.run(df, sig, n_trials=n_trials)
                    m = res['metrics']
                    tf_sweep_btc_1h.append({
                        'ema': ema, 'sl': sl, 'tp': tp, 'cd': cd,
                        'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                        'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                        'pf': m['profit_factor'], 'trades': m['total_trades'],
                        'avg_win': m['avg_win_pct'], 'avg_loss': m['avg_loss_pct'],
                    })

    tf_sweep_btc_1h.sort(key=lambda x: (x['sharpe'] if x['trades'] >= 5 else -999, x['net']), reverse=True)
    
    # Filter: require at least 5 trades and positive Sharpe
    viable = [r for r in tf_sweep_btc_1h if r['trades'] >= 5 and r['sharpe'] > 0.5]
    
    if viable:
        print(f"\n  Top 10 viable TrendFollow BTC 1h configs (≥5 trades, Sharpe>0.5):")
        print(f"  {'EMA':>4s} {'SL%':>5s} {'TP%':>5s} {'CD':>3s} {'Net%':>8s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s} {'AW%':>7s} {'AL%':>7s}")
        for r in viable[:15]:
            print(f"  {r['ema']:4d} {r['sl']*100:4.1f}% {r['tp']*100:4.1f}% {r['cd']:3d} "
                  f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d} "
                  f"{r['avg_win']:+6.2f}% {r['avg_loss']:+6.2f}%")
        
        best = viable[0]
        print(f"\n  ▶ BEST BTC 1h: EMA={best['ema']} SL={best['sl']*100:.1f}% TP={best['tp']*100:.1f}% CD={best['cd']} "
              f"→ net={best['net']:+.2f}% sharpe={best['sharpe']:+.2f} dd={best['dd']:.1f}% "
              f"wr={best['wr']:.0f}% #T={best['trades']}")
    else:
        print("  ⚠️ No configs met viability threshold (≥5 trades, Sharpe>0.5)")
        # Show best by Sharpe regardless
        top = tf_sweep_btc_1h[:10]
        print(f"\n  Top 10 overall (by Sharpe then net):")
        print(f"  {'EMA':>4s} {'SL%':>5s} {'TP%':>5s} {'CD':>3s} {'Net%':>8s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
        for r in top:
            print(f"  {r['ema']:4d} {r['sl']*100:4.1f}% {r['tp']*100:4.1f}% {r['cd']:3d} "
                  f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d}")

# ══════════════════════════════════════════════════════════════════════
# 2. TrendFollow Sweep — BTC 15m (30d data)
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("2. TRENDFOLLOW SWEEP — BTC/USDT 15m (30-day range)")
print("=" * 80)

tf_sweep_btc_15m = []
df = data.get(('BTC/USDT', '15m'))
if df is not None and len(df) > 100:
    n_trials15 = sum(1 for ema in [30,50,75,100,150,200,250] for sl in [0.005,0.01,0.015,0.02,0.025] for tp in [0.01,0.015,0.02,0.025,0.03,0.04,0.05] for cd in [5,8,10,15,20] if tp > sl)
    for ema in [30, 50, 75, 100, 150, 200, 250]:
        for sl in [0.005, 0.01, 0.015, 0.02, 0.025]:
            for tp in [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
                if tp <= sl:
                    continue
                for cd in [5, 8, 10, 15, 20]:
                    sig = trendfollow_signals(df, ema, sl, tp, cd)
                    res = engine.run(df, sig, n_trials=n_trials15)
                    m = res['metrics']
                    tf_sweep_btc_15m.append({
                        'ema': ema, 'sl': sl, 'tp': tp, 'cd': cd,
                        'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                        'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                        'pf': m['profit_factor'], 'trades': m['total_trades'],
                        'avg_win': m['avg_win_pct'], 'avg_loss': m['avg_loss_pct'],
                    })

    tf_sweep_btc_15m.sort(key=lambda x: (x['sharpe'] if x['trades'] >= 8 else -999, x['net']), reverse=True)
    
    viable = [r for r in tf_sweep_btc_15m if r['trades'] >= 8 and r['sharpe'] > 0.5]
    
    if viable:
        print(f"\n  Top 15 viable TrendFollow BTC 15m configs (≥8 trades, Sharpe>0.5):")
        print(f"  {'EMA':>4s} {'SL%':>5s} {'TP%':>5s} {'CD':>3s} {'Net%':>8s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s} {'AW%':>7s} {'AL%':>7s}")
        for r in viable[:15]:
            print(f"  {r['ema']:4d} {r['sl']*100:4.1f}% {r['tp']*100:4.1f}% {r['cd']:3d} "
                  f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d} "
                  f"{r['avg_win']:+6.2f}% {r['avg_loss']:+6.2f}%")
        
        best_15m = viable[0]
        print(f"\n  ▶ BEST BTC 15m: EMA={best_15m['ema']} SL={best_15m['sl']*100:.1f}% TP={best_15m['tp']*100:.1f}% CD={best_15m['cd']} "
              f"→ net={best_15m['net']:+.2f}% sharpe={best_15m['sharpe']:+.2f} dd={best_15m['dd']:.1f}% "
              f"wr={best_15m['wr']:.0f}% #T={best_15m['trades']}")
    else:
        print("  ⚠️ No configs met viability threshold (≥8 trades, Sharpe>0.5)")
        top = sorted(tf_sweep_btc_15m, key=lambda x: x['sharpe'], reverse=True)[:10]
        print(f"\n  Top 10 overall (by Sharpe):")
        for r in top:
            print(f"  EMA{r['ema']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% CD={r['cd']} "
                  f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} dd={r['dd']:.1f}% "
                  f"wr={r['wr']:.0f}% #T={r['trades']}")

# ══════════════════════════════════════════════════════════════════════
# 3. TrendFollow Sweep — ETH 1h (90d data)
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("3. TRENDFOLLOW SWEEP — ETH/USDT 1h (90-day range)")
print("=" * 80)

tf_sweep_eth_1h = []
df = data.get(('ETH/USDT', '1h'))
if df is not None and len(df) > 100:
    n_trials_eth = sum(1 for ema in [20,30,50,75,100,150,200] for sl in [0.01,0.015,0.02,0.025,0.03,0.04] for tp in [0.02,0.03,0.04,0.05,0.06,0.08] for cd in [5,8,10,15] if tp > sl)
    for ema in [20, 30, 50, 75, 100, 150, 200]:
        for sl in [0.01, 0.015, 0.02, 0.025, 0.03, 0.04]:
            for tp in [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]:
                if tp <= sl:
                    continue
                for cd in [5, 8, 10, 15]:
                    sig = trendfollow_signals(df, ema, sl, tp, cd)
                    res = engine.run(df, sig, n_trials=n_trials_eth)
                    m = res['metrics']
                    tf_sweep_eth_1h.append({
                        'ema': ema, 'sl': sl, 'tp': tp, 'cd': cd,
                        'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                        'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                        'pf': m['profit_factor'], 'trades': m['total_trades'],
                    })

    tf_sweep_eth_1h.sort(key=lambda x: (x['sharpe'] if x['trades'] >= 5 else -999, x['net']), reverse=True)
    
    viable = [r for r in tf_sweep_eth_1h if r['trades'] >= 5 and r['sharpe'] > 0.3]
    
    if viable:
        print(f"\n  Top 10 viable ETH 1h TrendFollow configs:")
        print(f"  {'EMA':>4s} {'SL%':>5s} {'TP%':>5s} {'CD':>3s} {'Net%':>8s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
        for r in viable[:10]:
            print(f"  {r['ema']:4d} {r['sl']*100:4.1f}% {r['tp']*100:4.1f}% {r['cd']:3d} "
                  f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d}")
        best_eth = viable[0]
        print(f"\n  ▶ BEST ETH 1h: EMA={best_eth['ema']} SL={best_eth['sl']*100:.1f}% TP={best_eth['tp']*100:.1f}% CD={best_eth['cd']} "
              f"→ net={best_eth['net']:+.2f}% sharpe={best_eth['sharpe']:+.2f}")
    else:
        print("  ⚠️ No viable ETH TrendFollow configs found on 90-day data")
        top = tf_sweep_eth_1h[:10]
        for r in top:
            print(f"  EMA{r['ema']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% CD={r['cd']} "
                  f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} #T={r['trades']}")

# ══════════════════════════════════════════════════════════════════════
# 4. RSI_MR Sweep on BTC 1h (90d)
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("4. RSI_MR SWEEP — BTC/USDT 1h (90-day range)")
print("=" * 80)

rsi_sweep_btc = []
df = data.get(('BTC/USDT', '1h'))
if df is not None and len(df) > 100:
    n_rsi_btc = sum(1 for rsi_p in [7,14,21] for os_level, ob_level in [(30,70),(25,75),(35,65),(20,80)] for sl in [0.02,0.03,0.04,0.05] for tp in [0.04,0.06,0.08,0.10] if tp > sl)
    for rsi_p in [7, 14, 21]:
        for os_level, ob_level in [(30, 70), (25, 75), (35, 65), (20, 80)]:
            for sl in [0.02, 0.03, 0.04, 0.05]:
                for tp in [0.04, 0.06, 0.08, 0.10]:
                    if tp <= sl:
                        continue
                    sig = rsi_mr_signals(df, rsi_p, os_level, ob_level, 50, sl, tp, 5)
                    res = engine.run(df, sig, n_trials=n_rsi_btc)
                    m = res['metrics']
                    rsi_sweep_btc.append({
                        'rsi_p': rsi_p, 'os': os_level, 'ob': ob_level, 'sl': sl, 'tp': tp,
                        'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                        'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                        'pf': m['profit_factor'], 'trades': m['total_trades'],
                    })

    rsi_sweep_btc.sort(key=lambda x: (x['sharpe'] if x['trades'] >= 4 else -999, x['net']), reverse=True)
    
    viable = [r for r in rsi_sweep_btc if r['trades'] >= 4 and r['sharpe'] > 0.5]
    if viable:
        print(f"\n  Top 10 RSI_MR BTC 1h configs (≥4 trades, Sharpe>0.5):")
        print(f"  {'RSI':>3s} {'OS':>3s} {'OB':>3s} {'SL%':>5s} {'TP%':>5s} {'Net%':>8s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
        for r in viable[:10]:
            print(f"  {r['rsi_p']:3d} {r['os']:3d} {r['ob']:3d} {r['sl']*100:4.1f}% {r['tp']*100:4.1f}% "
                  f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d}")
        best_rsi = viable[0]
        print(f"\n  ▶ BEST RSI BTC 1h: RSI={best_rsi['rsi_p']} OS={best_rsi['os']} OB={best_rsi['ob']} SL={best_rsi['sl']*100:.1f}% TP={best_rsi['tp']*100:.1f}% "
              f"→ net={best_rsi['net']:+.2f}% sharpe={best_rsi['sharpe']:+.2f}")
    else:
        top = rsi_sweep_btc[:10]
        print(f"\n  Top 10 overall:")
        for r in top:
            print(f"  RSI{r['rsi_p']} OS{r['os']} OB{r['ob']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% "
                  f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} #T={r['trades']}")

# ══════════════════════════════════════════════════════════════════════
# 5. RSI_MR Sweep — ETH 1h (90d)
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("5. RSI_MR SWEEP — ETH/USDT 1h (90-day range)")
print("=" * 80)

rsi_sweep_eth = []
df = data.get(('ETH/USDT', '1h'))
if df is not None and len(df) > 100:
    n_rsi_eth = sum(1 for rsi_p in [7,14,21] for os_level, ob_level in [(30,70),(25,75),(35,65),(20,80)] for sl in [0.02,0.03,0.04,0.05] for tp in [0.04,0.06,0.08,0.10] if tp > sl)
    for rsi_p in [7, 14, 21]:
        for os_level, ob_level in [(30, 70), (25, 75), (35, 65), (20, 80)]:
            for sl in [0.02, 0.03, 0.04, 0.05]:
                for tp in [0.04, 0.06, 0.08, 0.10]:
                    if tp <= sl:
                        continue
                    sig = rsi_mr_signals(df, rsi_p, os_level, ob_level, 50, sl, tp, 5)
                    res = engine.run(df, sig, n_trials=n_rsi_eth)
                    m = res['metrics']
                    rsi_sweep_eth.append({
                        'rsi_p': rsi_p, 'os': os_level, 'ob': ob_level, 'sl': sl, 'tp': tp,
                        'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                        'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                        'pf': m['profit_factor'], 'trades': m['total_trades'],
                    })

    rsi_sweep_eth.sort(key=lambda x: (x['sharpe'] if x['trades'] >= 4 else -999, x['net']), reverse=True)
    
    viable = [r for r in rsi_sweep_eth if r['trades'] >= 4 and r['sharpe'] > 0.3]
    if viable:
        print(f"\n  Top 10 RSI_MR ETH 1h configs (≥4 trades, Sharpe>0.3):")
        print(f"  {'RSI':>3s} {'OS':>3s} {'OB':>3s} {'SL%':>5s} {'TP%':>5s} {'Net%':>8s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
        for r in viable[:10]:
            print(f"  {r['rsi_p']:3d} {r['os']:3d} {r['ob']:3d} {r['sl']*100:4.1f}% {r['tp']*100:4.1f}% "
                  f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d}")
        best_rsi_eth = viable[0]
        print(f"\n  ▶ BEST RSI ETH 1h: RSI={best_rsi_eth['rsi_p']} OS={best_rsi_eth['os']} OB={best_rsi_eth['ob']} SL={best_rsi_eth['sl']*100:.1f}% TP={best_rsi_eth['tp']*100:.1f}% "
              f"→ net={best_rsi_eth['net']:+.2f}% sharpe={best_rsi_eth['sharpe']:+.2f}")
    else:
        top = rsi_sweep_eth[:8]
        print(f"\n  Top results:")
        for r in top:
            print(f"  RSI{r['rsi_p']} OS{r['os']} OB{r['ob']} "
                  f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} #T={r['trades']}")

# ══════════════════════════════════════════════════════════════════════
# SUMMARY & RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("PROMETHEUS RECOMMENDATIONS")
print("=" * 80)

current_config = {
    'TrendFollow_BTC': {'ema': 150, 'sl': 0.015, 'tp': 0.02, 'cd': 15, 'tf': '15m'},
    'TrendFollow_BTC_1h': {'ema': 150, 'sl': 0.03, 'tp': 0.05, 'cd': 15, 'tf': '1h'},
    'TrendFollow_ETH': {'ema': 150, 'sl': 0.025, 'tp': 0.06, 'cd': 15, 'tf': '15m'},
}

recommendations = []

# Compare current BTC 1h vs best found
if tf_sweep_btc_1h:
    best_btc_1h = tf_sweep_btc_1h[0]
    print(f"\n📊 TrendFollow_BTC_1h (1h):")
    print(f"   CURRENT: EMA=150 SL=3.0% TP=5.0% CD=15")
    print(f"   BEST:    EMA={best_btc_1h['ema']} SL={best_btc_1h['sl']*100:.1f}% TP={best_btc_1h['tp']*100:.1f}% CD={best_btc_1h['cd']}")
    print(f"            net={best_btc_1h['net']:+.2f}% sharpe={best_btc_1h['sharpe']:+.2f} dd={best_btc_1h['dd']:.1f}% "
          f"wr={best_btc_1h['wr']:.0f}% #T={best_btc_1h['trades']}")

# Compare current BTC 15m vs best found
if tf_sweep_btc_15m:
    best_btc_15m = tf_sweep_btc_15m[0]
    print(f"\n📊 TrendFollow_BTC (15m):")
    print(f"   CURRENT: EMA=150 SL=1.5% TP=2.0% CD=15")
    print(f"   BEST:    EMA={best_btc_15m['ema']} SL={best_btc_15m['sl']*100:.1f}% TP={best_btc_15m['tp']*100:.1f}% CD={best_btc_15m['cd']}")
    print(f"            net={best_btc_15m['net']:+.2f}% sharpe={best_btc_15m['sharpe']:+.2f} dd={best_btc_15m['dd']:.1f}% "
          f"wr={best_btc_15m['wr']:.0f}% #T={best_btc_15m['trades']}")

# ETH findings
if tf_sweep_eth_1h:
    best_eth = tf_sweep_eth_1h[0]
    print(f"\n📊 TrendFollow_ETH (1h):")
    print(f"   CURRENT (15m): EMA=150 SL=2.5% TP=6.0% CD=15 (DISABLED)")
    print(f"   BEST 1h: EMA={best_eth['ema']} SL={best_eth['sl']*100:.1f}% TP={best_eth['tp']*100:.1f}% CD={best_eth['cd']}")
    print(f"            net={best_eth['net']:+.2f}% sharpe={best_eth['sharpe']:+.2f} dd={best_eth['dd']:.1f}% "
          f"wr={best_eth['wr']:.0f}% #T={best_eth['trades']}")

# RSI findings
if rsi_sweep_btc:
    best_rsi = rsi_sweep_btc[0]
    print(f"\n📊 RSI_MR (BTC 1h):")
    print(f"   BEST: RSI={best_rsi['rsi_p']} OS={best_rsi['os']} OB={best_rsi['ob']} SL={best_rsi['sl']*100:.1f}% TP={best_rsi['tp']*100:.1f}%")
    print(f"         net={best_rsi['net']:+.2f}% sharpe={best_rsi['sharpe']:+.2f} dd={best_rsi['dd']:.1f}% "
          f"wr={best_rsi['wr']:.0f}% #T={best_rsi['trades']}")
    if best_rsi['sharpe'] > 1.0 and best_rsi['trades'] >= 4:
        recommendations.append({
            'action': 'ENABLE',
            'strategy': 'RSI_MR (BTC 1h)',
            'params': f"RSI={best_rsi['rsi_p']} OS={best_rsi['os']} OB={best_rsi['ob']} SL={best_rsi['sl']*100:.1f}% TP={best_rsi['tp']*100:.1f}%",
            'reason': f"net={best_rsi['net']:+.2f}% sharpe={best_rsi['sharpe']:+.2f}"
        })

if rsi_sweep_eth:
    best_rsi_eth = rsi_sweep_eth[0]
    print(f"\n📊 RSI_MR (ETH 1h):")
    print(f"   BEST: RSI={best_rsi_eth['rsi_p']} OS={best_rsi_eth['os']} OB={best_rsi_eth['ob']} SL={best_rsi_eth['sl']*100:.1f}% TP={best_rsi_eth['tp']*100:.1f}%")
    print(f"         net={best_rsi_eth['net']:+.2f}% sharpe={best_rsi_eth['sharpe']:+.2f} dd={best_rsi_eth['dd']:.1f}% "
          f"wr={best_rsi_eth['wr']:.0f}% #T={best_rsi_eth['trades']}")

print("\n" + "=" * 80)
print("ACTIONS TAKEN")
print("=" * 80)

if recommendations:
    for r in recommendations:
        print(f"  {r['action']}: {r['strategy']} — {r['reason']}")

print(f"\n⏱️ Runtime: {(datetime.now(timezone.utc) - t0).total_seconds():.1f}s")
print("🔥 Prometheus optimization complete")
