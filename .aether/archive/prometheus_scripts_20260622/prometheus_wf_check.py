#!/usr/bin/env python3
"""Prometheus — WF Validation for Athena Recommendations"""
import sys, os, json
sys.path.insert(0, '.')
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.walk_forward import walk_forward_validate

# Signal generator (inlined from prometheus_fast.py)
def trendfollow_signals(df, ema_period, sl_pct, tp_pct, cooldown_bars):
    close = df['close'].values
    n = len(close)
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]
    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = max(ema_period * 2, 100)
    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        uptrend = ema_slope[i] > 0
        if pos == 1:
            exit_trigger = False
            if not uptrend: exit_trigger = True
            elif price <= entry_price * (1 - sl_pct): exit_trigger = True
            elif price >= entry_price * (1 + tp_pct): exit_trigger = True
            if exit_trigger:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        elif pos == -1:
            exit_trigger = False
            if uptrend: exit_trigger = True
            elif price >= entry_price * (1 + sl_pct): exit_trigger = True
            elif price <= entry_price * (1 - tp_pct): exit_trigger = True
            if exit_trigger:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        if pos != 0: signals[i] = pos
        if pos == 0 and bars_since_trade > cooldown_bars:
            if uptrend: pos = 1; entry_price = price; signals[i] = 1; bars_since_trade = 0
            else: pos = -1; entry_price = price; signals[i] = -1; bars_since_trade = 0
    return pd.Series(signals, index=df.index)

cfg = get_config()
storage = MarketStorage(cfg.db_path)
engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

print("Prometheus - WF Validation for Athena Recommendations")
print("=" * 70)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
print()

# Load data
data = {}
for sym, tf in [('BTC/USDT', '1h'), ('ETH/USDT', '1h'), ('BTC/USDT', '15m')]:
    df = storage.load_klines(sym, tf)
    if not df.empty:
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('open_time', inplace=True)
        df.sort_index(inplace=True)
        data[(sym, tf)] = df
        days = (df.index[-1] - df.index[0]).days
        print(f"  DATA {sym:10s} {tf:4s}: {len(df):5d} bars, {days}d [{df.index[0].strftime('%m/%d')} -> {df.index[-1].strftime('%m/%d')}]")

results = {}

# --- 1. TrendFollow_BTC_1h ---
print()
print("-" * 70)
print("1. TrendFollow_BTC_1h (EMA=50, SL=1.5%, TP=5%, CD=8)")
print("-" * 70)

df_btc_1h = data[('BTC/USDT', '1h')]
params_btc_1h = {'ema_period': 50, 'sl_pct': 0.015, 'tp_pct': 0.05, 'cooldown_bars': 8}

sig_full = trendfollow_signals(df_btc_1h, **params_btc_1h)
res_full = engine.run(df_btc_1h, sig_full)
m = res_full['metrics']
print(f"  Full: net={m['total_return_pct']:+.2f}% sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% wr={m['win_rate']:.0f}% trades={m['total_trades']} pf={m['profit_factor']:.2f}")

wf_btc_1h = walk_forward_validate(df_btc_1h, trendfollow_signals, engine,
                                   train_days=45, test_days=21,
                                   min_train_bars=100, min_test_bars=30,
                                   **params_btc_1h)
print(f"  WF: {wf_btc_1h['interpretation']}")
print(f"  IS={wf_btc_1h['total_is_return_pct']:+.2f}% OOS={wf_btc_1h['total_oos_return_pct']:+.2f}% WFE={wf_btc_1h['wfe']:.3f} windows={wf_btc_1h['windows']} passed={wf_btc_1h['passed']}")
results['TrendFollow_BTC_1h'] = {'full': m, 'wf': wf_btc_1h, 'params': params_btc_1h}

# --- 2. TrendFollow_ETH (1h) ---
print()
print("-" * 70)
print("2. TrendFollow_ETH (1h) (EMA=50, SL=3%, TP=6%, CD=8)")
print("-" * 70)

df_eth_1h = data[('ETH/USDT', '1h')]
params_eth = {'ema_period': 50, 'sl_pct': 0.03, 'tp_pct': 0.06, 'cooldown_bars': 8}

sig_full = trendfollow_signals(df_eth_1h, **params_eth)
res_full = engine.run(df_eth_1h, sig_full)
m = res_full['metrics']
print(f"  Full: net={m['total_return_pct']:+.2f}% sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% wr={m['win_rate']:.0f}% trades={m['total_trades']} pf={m['profit_factor']:.2f}")

wf_eth = walk_forward_validate(df_eth_1h, trendfollow_signals, engine,
                                train_days=45, test_days=21,
                                min_train_bars=100, min_test_bars=30,
                                **params_eth)
print(f"  WF: {wf_eth['interpretation']}")
print(f"  IS={wf_eth['total_is_return_pct']:+.2f}% OOS={wf_eth['total_oos_return_pct']:+.2f}% WFE={wf_eth['wfe']:.3f} windows={wf_eth['windows']} passed={wf_eth['passed']}")
results['TrendFollow_ETH'] = {'full': m, 'wf': wf_eth, 'params': params_eth}

# --- 3. TrendFollow_BTC (15m) ---
print()
print("-" * 70)
print("3. TrendFollow_BTC (15m) (EMA=75, SL=1%, TP=1.5%, CD=10)")
print("-" * 70)

df_btc_15m = data[('BTC/USDT', '15m')]
params_btc_15m = {'ema_period': 75, 'sl_pct': 0.01, 'tp_pct': 0.015, 'cooldown_bars': 10}

sig_full = trendfollow_signals(df_btc_15m, **params_btc_15m)
res_full = engine.run(df_btc_15m, sig_full)
m = res_full['metrics']
print(f"  Full: net={m['total_return_pct']:+.2f}% sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% wr={m['win_rate']:.0f}% trades={m['total_trades']} pf={m['profit_factor']:.2f}")

wf_btc_15m = walk_forward_validate(df_btc_15m, trendfollow_signals, engine,
                                    train_days=14, test_days=7,
                                    min_train_bars=100, min_test_bars=50,
                                    **params_btc_15m)
print(f"  WF (14d/7d): {wf_btc_15m['interpretation']}")
print(f"  IS={wf_btc_15m['total_is_return_pct']:+.2f}% OOS={wf_btc_15m['total_oos_return_pct']:+.2f}% WFE={wf_btc_15m['wfe']:.3f} windows={wf_btc_15m['windows']} passed={wf_btc_15m['passed']}")
results['TrendFollow_BTC'] = {'full': m, 'wf': wf_btc_15m, 'params': params_btc_15m}

# --- 4. RegimeSwitch_BTC ---
print()
print("-" * 70)
print("4. RegimeSwitch_BTC (1h, Heuristic Regime + TrendFollow/MR sub)")
print("-" * 70)

def regimeswitch_signals(df, trend_ema=50, mr_rsi=14, mr_over=70, mr_under=30,
                          trend_sl=0.02, trend_tp=0.05, mr_sl=0.02, mr_tp=0.04,
                          vol_window=20, trend_window=50, cooldown=5):
    """Simplified RegimeSwitch signal generator for WF validation.
    Uses heuristic regime detection (HMM not available in batch)."""
    close = df['close'].values
    n = len(close)
    signals = np.zeros(n, dtype=int)
    returns = np.diff(np.log(close))
    returns = np.insert(returns, 0, 0.0)
    vol = pd.Series(returns).rolling(vol_window).std().fillna(0).values
    ema = pd.Series(close).ewm(span=trend_ema, adjust=False).mean().values
    ema_slope = np.zeros(n); ema_slope[5:] = ema[5:] - ema[:-5]
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=mr_rsi, adjust=False).mean()
    avg_loss = loss.ewm(span=mr_rsi, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_vals = (100 - (100 / (1 + rs))).fillna(50).values
    pos = 0; entry_price = 0.0
    bars_since_trade = cooldown + 1
    min_bars = max(trend_window, 100)
    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        median_vol = np.nanmedian(vol[max(0,i-100):i+1])
        current_vol = vol[i]; uptrend = ema_slope[i] > 0
        if current_vol > median_vol * 1.5: regime = 'HIGH_VOL'
        elif abs(ema_slope[i])/price > 0.001: regime = 'TRENDING'
        elif current_vol < median_vol * 0.5: regime = 'LOW_VOL'
        else: regime = 'RANGING'
        if pos == 1:
            if not uptrend or price <= entry_price*(1-trend_sl) or price >= entry_price*(1+trend_tp):
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            signals[i] = 1
        elif pos == -1:
            if uptrend or price >= entry_price*(1+trend_sl) or price <= entry_price*(1-trend_tp):
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            signals[i] = -1
        if pos == 0 and bars_since_trade > cooldown:
            if regime == 'TRENDING':
                if uptrend: pos=1; entry_price=price; signals[i]=1; bars_since_trade=0
                else: pos=-1; entry_price=price; signals[i]=-1; bars_since_trade=0
            elif regime == 'RANGING':
                if rsi_vals[i] < mr_under: pos=1; entry_price=price; signals[i]=1; bars_since_trade=0
                elif rsi_vals[i] > mr_over: pos=-1; entry_price=price; signals[i]=-1; bars_since_trade=0
    return pd.Series(signals, index=df.index)

df_btc_1h_rs = data[('BTC/USDT', '1h')]
params_rs = {'trend_ema': 50, 'mr_rsi': 14, 'mr_over': 70, 'mr_under': 30,
             'trend_sl': 0.02, 'trend_tp': 0.05, 'mr_sl': 0.02, 'mr_tp': 0.04,
             'vol_window': 20, 'trend_window': 50, 'cooldown': 5}

sig_full = regimeswitch_signals(df_btc_1h_rs, **params_rs)
res_full = engine.run(df_btc_1h_rs, sig_full)
m = res_full['metrics']
print(f"  Full: net={m['total_return_pct']:+.2f}% sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% wr={m['win_rate']:.0f}% trades={m['total_trades']} pf={m['profit_factor']:.2f}")

wf_rs = walk_forward_validate(df_btc_1h_rs, regimeswitch_signals, engine,
                               train_days=60, test_days=30,
                               min_train_bars=100, min_test_bars=30,
                               **params_rs)
print(f"  WF: {wf_rs['interpretation']}")
print(f"  IS={wf_rs['total_is_return_pct']:+.2f}% OOS={wf_rs['total_oos_return_pct']:+.2f}% WFE={wf_rs['wfe']:.3f} windows={wf_rs['windows']} passed={wf_rs['passed']}")
for i, w in enumerate(wf_rs['window_details']):
    print(f"    Win{i+1}: IS={w['is_return']:+.2f}% OOS={w['oos_return']:+.2f}% | {w['test'][0][:10]}->{w['test'][1][:10]} | T={w['oos_trades']} WR={w['oos_win_rate']:.0f}%")

results['RegimeSwitch_BTC'] = {'full': m, 'wf': wf_rs, 'params': params_rs}

# --- 5. RSI_MR_BTC ---
print()
print("-" * 70)
print("5. RSI_MR_BTC (1h, RSI=14, OS=30, OB=70, SL=3%, TP=6%)")
print("-" * 70)

def rsi_mr_signals(df, rsi_period=14, oversold=30, overbought=70, sl_pct=0.03,
                    tp_pct=0.06, cooldown=5):
    close = df['close'].values; n = len(close)
    signals = np.zeros(n, dtype=int)
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(span=rsi_period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_vals = (100 - (100/(1+rs))).fillna(50).values
    pos = 0; entry_price = 0.0
    bars_since_trade = cooldown + 1
    min_bars = max(rsi_period * 3, 50)
    for i in range(min_bars, n):
        bars_since_trade += 1; price = close[i]
        if pos == 1:
            if rsi_vals[i] >= 50 or price <= entry_price*(1-sl_pct) or price >= entry_price*(1+tp_pct):
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            signals[i] = 1
        elif pos == -1:
            if rsi_vals[i] <= 50 or price >= entry_price*(1+sl_pct) or price <= entry_price*(1-tp_pct):
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            signals[i] = -1
        if pos == 0 and bars_since_trade > cooldown:
            if rsi_vals[i] < oversold: pos=1; entry_price=price; signals[i]=1; bars_since_trade=0
            elif rsi_vals[i] > overbought: pos=-1; entry_price=price; signals[i]=-1; bars_since_trade=0
    return pd.Series(signals, index=df.index)

params_rsi = {'rsi_period': 14, 'oversold': 30, 'overbought': 70,
              'sl_pct': 0.03, 'tp_pct': 0.06, 'cooldown': 5}

sig_full = rsi_mr_signals(df_btc_1h_rs, **params_rsi)
res_full = engine.run(df_btc_1h_rs, sig_full)
m = res_full['metrics']
print(f"  Full: net={m['total_return_pct']:+.2f}% sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% wr={m['win_rate']:.0f}% trades={m['total_trades']} pf={m['profit_factor']:.2f}")

wf_rsi = walk_forward_validate(df_btc_1h_rs, rsi_mr_signals, engine,
                                train_days=60, test_days=30,
                                min_train_bars=100, min_test_bars=30,
                                **params_rsi)
print(f"  WF: {wf_rsi['interpretation']}")
print(f"  IS={wf_rsi['total_is_return_pct']:+.2f}% OOS={wf_rsi['total_oos_return_pct']:+.2f}% WFE={wf_rsi['wfe']:.3f} windows={wf_rsi['windows']} passed={wf_rsi['passed']}")
for i, w in enumerate(wf_rsi['window_details']):
    print(f"    Win{i+1}: IS={w['is_return']:+.2f}% OOS={w['oos_return']:+.2f}% | {w['test'][0][:10]}->{w['test'][1][:10]} | T={w['oos_trades']} WR={w['oos_win_rate']:.0f}%")

results['RSI_MR_BTC'] = {'full': m, 'wf': wf_rsi, 'params': params_rsi}

# --- Save to state/prometheus.json (merge with existing) ---
state_dir = '.aether/state'
os.makedirs(state_dir, exist_ok=True)
state_path = os.path.join(state_dir, 'prometheus.json')

# Load existing state to preserve engine-managed keys
existing = {}
if os.path.exists(state_path):
    try:
        with open(state_path) as f:
            existing = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

# Build wf_findings (engine.py preserves this key at lines 645-649)
wf_findings = existing.get('wf_findings', {})
for name, r in results.items():
    wf_findings[f'{name}_WF'] = r['wf']['interpretation']

# Merge: update existing with our data
existing['wf_findings'] = wf_findings
existing['wf_validation'] = {}
for name, r in results.items():
    existing['wf_validation'][name] = {
        'full_net': r['full']['total_return_pct'],
        'full_sharpe': r['full']['sharpe_ratio'],
        'full_dd': r['full']['max_drawdown_pct'],
        'full_wr': r['full']['win_rate'],
        'full_trades': r['full']['total_trades'],
        'full_pf': r['full']['profit_factor'],
        'wf_wfe': r['wf']['wfe'],
        'wf_is_return': r['wf']['total_is_return_pct'],
        'wf_oos_return': r['wf']['total_oos_return_pct'],
        'wf_passed': r['wf']['passed'],
        'wf_interpretation': r['wf']['interpretation'],
        'wf_windows': r['wf']['windows'],
    }
existing['_updated_at'] = datetime.now(timezone.utc).isoformat()

with open(state_path, 'w') as f:
    json.dump(existing, f, indent=2, default=str, ensure_ascii=False)

# Also write legacy path for tools that read it
os.makedirs('.aether', exist_ok=True)
with open('.aether/prometheus.json', 'w') as f:
    json.dump({'wf_validation': existing['wf_validation'], 'wf_findings': wf_findings,
               '_updated_at': existing['_updated_at']}, f, indent=2, default=str)

elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\nTime: {elapsed:.1f}s | state/prometheus.json written")
print("WF Validation complete")

# --- Recommendations ---
print()
print("=" * 70)
print("RECOMMENDATIONS")
print("=" * 70)

actions = []
btc_1h = results['TrendFollow_BTC_1h']
eth = results['TrendFollow_ETH']
regime_switch = results.get('RegimeSwitch_BTC')
rsi_mr = results.get('RSI_MR_BTC')

if btc_1h['wf']['passed'] and btc_1h['full']['total_return_pct'] > 0.0:
    print(f"\n>> ENABLE TrendFollow_BTC_1h: {btc_1h['wf']['interpretation']}")
    print(f"   Full: net={btc_1h['full']['total_return_pct']:+.2f}% sharpe={btc_1h['full']['sharpe_ratio']:+.2f} dd={btc_1h['full']['max_drawdown_pct']:.1f}%")
    actions.append(('TrendFollow_BTC_1h', btc_1h['params']))
else:
    print(f"\n>> KEEP DISABLED TrendFollow_BTC_1h: {btc_1h['wf']['interpretation']}")

if eth['wf']['passed'] and eth['full']['total_return_pct'] > 0.0:
    print(f"\n>> ENABLE TrendFollow_ETH: {eth['wf']['interpretation']}")
    print(f"   Full: net={eth['full']['total_return_pct']:+.2f}% sharpe={eth['full']['sharpe_ratio']:+.2f} dd={eth['full']['max_drawdown_pct']:.1f}%")
    actions.append(('TrendFollow_ETH', eth['params']))
else:
    print(f"\n>> KEEP DISABLED TrendFollow_ETH: {eth['wf']['interpretation']}")

if regime_switch:
    rs_wf = regime_switch['wf']
    rs_full = regime_switch['full']
    if rs_wf['passed'] and rs_full['total_return_pct'] > 0.0:
        print(f"\n>> KEEP RegimeSwitch_BTC: {rs_wf['interpretation']}")
        print(f"   Full: net={rs_full['total_return_pct']:+.2f}% sharpe={rs_full['sharpe_ratio']:+.2f} dd={rs_full['max_drawdown_pct']:.1f}%")
    else:
        # Check if recent windows show improvement
        recent_positive = any(w['oos_return'] > 0 for w in rs_wf['window_details'][-2:]) if len(rs_wf['window_details']) >= 2 else False
        if recent_positive:
            print(f"\n>> MONITOR RegimeSwitch_BTC: {rs_wf['interpretation']} — but recent windows positive, keep enabled with watch")
        else:
            print(f"\n>> FLAG RegimeSwitch_BTC: {rs_wf['interpretation']} — consider disable if 1 more negative window")

if rsi_mr:
    rsi_wf = rsi_mr['wf']
    rsi_full = rsi_mr['full']
    if rsi_wf['passed'] and rsi_full['total_return_pct'] > 0.0:
        print(f"\n>> KEEP RSI_MR_BTC: {rsi_wf['interpretation']}")
        print(f"   Full: net={rsi_full['total_return_pct']:+.2f}% sharpe={rsi_full['sharpe_ratio']:+.2f} dd={rsi_full['max_drawdown_pct']:.1f}%")
    elif rsi_wf['passed']:
        print(f"\n>> CAUTION RSI_MR_BTC: {rsi_wf['interpretation']} — WF not overfit but full negative")
    else:
        print(f"\n>> FLAG RSI_MR_BTC: {rsi_wf['interpretation']}")

# Apply actions to strategies.yaml
if actions:
    import yaml
    with open('config/strategies.yaml') as f:
        config = yaml.safe_load(f)
    
    for name, params in actions:
        for s in config['strategies']:
            if s['name'] == name:
                s['enabled'] = True
                s['params']['ema_period'] = params['ema_period']
                s['params']['stop_loss_pct'] = params['sl_pct']
                s['params']['take_profit_pct'] = params['tp_pct']
                s['params']['cooldown_bars'] = params['cooldown_bars']
                # Ensure timeframe is correct
                if '1h' in name:
                    s['params']['timeframes'] = ['1h']
                print(f"  Applied: {name} enabled, EMA={params['ema_period']} SL={params['sl_pct']*100:.1f}% TP={params['tp_pct']*100:.1f}% CD={params['cooldown_bars']}")
    
    with open('config/strategies.yaml', 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"\n  strategies.yaml updated with {len(actions)} actions")
else:
    print("\n  No changes to strategies.yaml")
