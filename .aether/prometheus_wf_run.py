#!/usr/bin/env python3
"""Prometheus Walk-Forward Anti-Overfitting Validation"""
import sys, json, os
sys.path.insert(0, '/home/rinnen/binance_quant')

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.walk_forward import walk_forward_validate
from athena_backtest import trendfollow_signals, rsi_mr_signals, ma_cross_signals

cfg = get_config()
storage = MarketStorage(cfg.db_path)
engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

print('Prometheus -- Walk-Forward Anti-Overfitting Validation')
print('=' * 70)
print(f'Run: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}')
print()

# Load data
data = {}
for sym in ['BTC/USDT', 'ETH/USDT']:
    for tf in ['15m', '1h']:
        df = storage.load_klines(sym, tf)
        if not df.empty:
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
            df.set_index('open_time', inplace=True)
            df.sort_index(inplace=True)
            data[(sym, tf)] = df
            print(f'  DATA {sym:10s} {tf:4s}: {len(df):5d} bars, {(df.index[-1]-df.index[0]).days}d')

results = []

# --- TrendFollow_BTC (15m, enabled) ---
print()
print('-' * 70)
print('WF1: TrendFollow_BTC 15m (EMA=75 SL=1.0% TP=1.5% CD=10)')
df = data[('BTC/USDT', '15m')]
wf = walk_forward_validate(df, trendfollow_signals, engine, train_days=30, test_days=15,
    ema_period=75, sl_pct=0.01, tp_pct=0.015, cooldown_bars=10)
print(f'  WFE={wf["wfe"]:.3f} | IS={wf["total_is_return_pct"]:+.2f}% OOS={wf["total_oos_return_pct"]:+.2f}%')
print(f'  IS_Shp={wf["is_sharpe"]:.3f} OOS_Shp={wf["oos_sharpe"]:.3f} | windows={wf["windows"]}')
print(f'  {wf["interpretation"]}')
results.append(('TrendFollow_BTC', wf))

# --- TrendFollow_BTC_1h (disabled, validate only) ---
print()
print('-' * 70)
print('WF2: TrendFollow_BTC_1h (EMA=50 SL=1.5% TP=5.0% CD=8) -- disabled, validation only')
df = data[('BTC/USDT', '1h')]
wf = walk_forward_validate(df, trendfollow_signals, engine, train_days=45, test_days=21,
    ema_period=50, sl_pct=0.015, tp_pct=0.05, cooldown_bars=8)
print(f'  WFE={wf["wfe"]:.3f} | IS={wf["total_is_return_pct"]:+.2f}% OOS={wf["total_oos_return_pct"]:+.2f}%')
print(f'  IS_Shp={wf["is_sharpe"]:.3f} OOS_Shp={wf["oos_sharpe"]:.3f} | windows={wf["windows"]}')
print(f'  {wf["interpretation"]}')
results.append(('TrendFollow_BTC_1h', wf))

# --- RSI_MR_ETH (enabled) ---
print()
print('-' * 70)
print('WF3: RSI_MR_ETH 1h (RSI=7 OS=35 OB=65 SL=3% TP=6%)')
df = data[('ETH/USDT', '1h')]
wf = walk_forward_validate(df, rsi_mr_signals, engine, train_days=45, test_days=21,
    rsi_period=7, oversold=35, overbought=65, exit_rsi=50, sl_pct=0.03, tp_pct=0.06, cooldown_bars=5)
print(f'  WFE={wf["wfe"]:.3f} | IS={wf["total_is_return_pct"]:+.2f}% OOS={wf["total_oos_return_pct"]:+.2f}%')
print(f'  IS_Shp={wf["is_sharpe"]:.3f} OOS_Shp={wf["oos_sharpe"]:.3f} | windows={wf["windows"]}')
print(f'  {wf["interpretation"]}')
results.append(('RSI_MR_ETH', wf))

# --- RSI_MR_BTC (disabled, but Athena says +6.14% 7d) ---
print()
print('-' * 70)
print('WF4: RSI_MR_BTC 1h (RSI=14 OS=30 OB=70 SL=3% TP=6%) -- disabled, Athena +6.14% 7d')
df = data[('BTC/USDT', '1h')]
wf = walk_forward_validate(df, rsi_mr_signals, engine, train_days=45, test_days=21,
    rsi_period=14, oversold=30, overbought=70, exit_rsi=50, sl_pct=0.03, tp_pct=0.06, cooldown_bars=5)
print(f'  WFE={wf["wfe"]:.3f} | IS={wf["total_is_return_pct"]:+.2f}% OOS={wf["total_oos_return_pct"]:+.2f}%')
print(f'  IS_Shp={wf["is_sharpe"]:.3f} OOS_Shp={wf["oos_sharpe"]:.3f} | windows={wf["windows"]}')
print(f'  {wf["interpretation"]}')
results.append(('RSI_MR_BTC', wf))

# --- MA_Cross_BTC (enabled) ---
print()
print('-' * 70)
print('WF5: MA_Cross_BTC 1h (FAST=5 SLOW=20)')
df = data[('BTC/USDT', '1h')]
wf = walk_forward_validate(df, ma_cross_signals, engine, train_days=45, test_days=21,
    fast_period=5, slow_period=20, atr_period=14, atr_sl_mult=2.0, atr_tp_mult=4.0, cooldown_bars=5)
print(f'  WFE={wf["wfe"]:.3f} | IS={wf["total_is_return_pct"]:+.2f}% OOS={wf["total_oos_return_pct"]:+.2f}%')
print(f'  IS_Shp={wf["is_sharpe"]:.3f} OOS_Shp={wf["oos_sharpe"]:.3f} | windows={wf["windows"]}')
print(f'  {wf["interpretation"]}')
results.append(('MA_Cross_BTC', wf))

# --- MA_Cross_ETH (enabled) ---
print()
print('-' * 70)
print('WF6: MA_Cross_ETH 1h (FAST=5 SLOW=13)')
df = data[('ETH/USDT', '1h')]
wf = walk_forward_validate(df, ma_cross_signals, engine, train_days=45, test_days=21,
    fast_period=5, slow_period=13, atr_period=14, atr_sl_mult=2.0, atr_tp_mult=3.0, cooldown_bars=5)
print(f'  WFE={wf["wfe"]:.3f} | IS={wf["total_is_return_pct"]:+.2f}% OOS={wf["total_oos_return_pct"]:+.2f}%')
print(f'  IS_Shp={wf["is_sharpe"]:.3f} OOS_Shp={wf["oos_sharpe"]:.3f} | windows={wf["windows"]}')
print(f'  {wf["interpretation"]}')
results.append(('MA_Cross_ETH', wf))

# --- Summary ---
print()
print('=' * 70)
print('WALK-FORWARD SUMMARY')
print('=' * 70)
print(f'  {"Strategy":<22s} {"WFE":>6s} {"IS%":>8s} {"OOS%":>8s} {"IS_Shp":>7s} {"OOS_Shp":>7s} {"Wins":>5s} {"PASS":>5s}')
print('  ' + '-' * 75)
for name, wf in results:
    flag = 'PASS' if wf['passed'] else 'FAIL'
    print(f'  {name:<22s} {wf["wfe"]:>6.3f} {wf["total_is_return_pct"]:>+7.2f}% {wf["total_oos_return_pct"]:>+7.2f}% {wf["is_sharpe"]:>+7.3f} {wf["oos_sharpe"]:>+7.3f} {wf["windows"]:>5d} {flag:>5s}')

overfit = [(n, w) for n, w in results if not w['passed']]
if overfit:
    print(f'\nWARNING: OVERFITTING DETECTED in {len(overfit)} strategies:')
    for n, w in overfit:
        print(f'     {n}: {w["interpretation"]}')
else:
    print(f'\nAll {len(results)} strategies passed walk-forward validation.')

# Save
os.makedirs('.aether', exist_ok=True)
wf_data = {
    'run_time': datetime.now(timezone.utc).isoformat(),
    'results': {n: {'wfe': w['wfe'], 'is_return': w['total_is_return_pct'],
                     'oos_return': w['total_oos_return_pct'], 'passed': w['passed'],
                     'interpretation': w['interpretation']} for n, w in results},
}
with open('.aether/prometheus_wf.json', 'w') as f:
    json.dump(wf_data, f, indent=2, default=str)
print(f'\nWalk-forward results saved to .aether/prometheus_wf.json')
print('Prometheus Walk-Forward validation complete')
