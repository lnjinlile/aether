#!/usr/bin/env python3
"""Prometheus — Targeted Optimization (fast, standalone)"""
import sys, os, json
sys.path.insert(0, '/home/rinnen/binance_quant')
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine

# ── Signal generators (inlined from athena_backtest to avoid import side-effects) ──

def trendfollow_signals(df: pd.DataFrame, ema_period: int,
                         sl_pct: float, tp_pct: float,
                         cooldown_bars: int):
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

def rsi_mr_signals(df, rsi_period, oversold, overbought, exit_rsi, sl_pct, tp_pct, cooldown_bars):
    close = df['close'].values; n = len(close)
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0); loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/rsi_period, adjust=False).mean().values
    avg_loss = loss.ewm(alpha=1/rsi_period, adjust=False).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.inf), where=avg_loss != 0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[avg_loss == 0] = 100.0; rsi[avg_gain == 0] = 0.0
    cross_below_os = (rsi < oversold) & (np.roll(rsi, 1) >= oversold); cross_below_os[:1] = False
    cross_above_ob = (rsi > overbought) & (np.roll(rsi, 1) <= overbought); cross_above_ob[:1] = False
    cross_above_ex = (rsi > exit_rsi) & (np.roll(rsi, 1) <= exit_rsi); cross_above_ex[:1] = False
    cross_below_ex = (rsi < exit_rsi) & (np.roll(rsi, 1) >= exit_rsi); cross_below_ex[:1] = False
    signals = np.zeros(n, dtype=int); pos = 0; entry_price = 0.0
    bars_since_trade = cooldown_bars + 1; min_bars = rsi_period * 3
    for i in range(min_bars, n):
        bars_since_trade += 1; price = close[i]
        if pos == 1:
            exit_trigger = False
            if cross_above_ex[i]: exit_trigger = True
            elif price <= entry_price * (1 - sl_pct): exit_trigger = True
            elif price >= entry_price * (1 + tp_pct): exit_trigger = True
            if exit_trigger: signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        elif pos == -1:
            exit_trigger = False
            if cross_below_ex[i]: exit_trigger = True
            elif price >= entry_price * (1 + sl_pct): exit_trigger = True
            elif price <= entry_price * (1 - tp_pct): exit_trigger = True
            if exit_trigger: signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        if pos != 0: signals[i] = pos
        if pos == 0 and bars_since_trade > cooldown_bars:
            if cross_below_os[i]: pos = 1; entry_price = price; signals[i] = 1; bars_since_trade = 0
            elif cross_above_ob[i]: pos = -1; entry_price = price; signals[i] = -1; bars_since_trade = 0
    return pd.Series(signals, index=df.index)

# ═══════════════════════════════════════════════════════════

cfg = get_config()
storage = MarketStorage(cfg.db_path)
engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

print("🔥 Prometheus — Targeted Optimization")
print("=" * 70)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}\n")

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
            print(f"  📊 {sym} {tf}: {len(df)} bars, {(df.index[-1]-df.index[0]).days}d")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRIORITY 1: TrendFollow BTC 1h (0 trades → fix)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "━" * 70)
print("PRIORITY 1: TrendFollow_BTC_1h — EMA sweep (CURRENT EMA=150 → 0 trades)")
print("━" * 70)

df = data[('BTC/USDT', '1h')]
price = float(df.iloc[-1]['close'])
print(f"  Data: {len(df)} bars, {(df.index[-1]-df.index[0]).days}d, last price={price:.1f}")

results_1h = []
# Pre-count valid combos for DSR
n_combos_1h = sum(1 for ema in [20,30,50,75,100] for sl in [0.01,0.015,0.02,0.03] for tp in [0.02,0.03,0.04,0.05] if tp > sl)
for ema in [20, 30, 50, 75, 100]:
    for sl in [0.01, 0.015, 0.02, 0.03]:
        for tp in [0.02, 0.03, 0.04, 0.05]:
            if tp <= sl: continue
            sig = trendfollow_signals(df, ema, sl, tp, cooldown_bars=8)
            res = engine.run(df, sig, n_trials=n_combos_1h)
            m = res['metrics']
            results_1h.append({'ema':ema,'sl':sl,'tp':tp,
                'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],'dsr':m['deflated_sharpe_ratio'],
                'dd':m['max_drawdown_pct'],'wr':m['win_rate'],'trades':m['total_trades']})

results_1h.sort(key=lambda x: (x['sharpe'] if x['trades']>=4 else -999, x['net']), reverse=True)
print(f"\n  Top 15 (≥4 trades prioritized):")
for r in results_1h[:15]:
    print(f"  EMA{r['ema']:4d} SL={r['sl']*100:4.1f}% TP={r['tp']*100:4.1f}% "
          f"net={r['net']:+7.2f}% shp={r['sharpe']:+6.2f} dsr={r['dsr']:.4f} dd={r['dd']:5.1f}% "
          f"wr={r['wr']:4.0f}% #T={r['trades']}")
best_1h = results_1h[0]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRIORITY 2: TrendFollow BTC 15m (losing → fix)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "━" * 70)
print("PRIORITY 2: TrendFollow_BTC 15m — fix losing params")
print("━" * 70)

df15 = data[('BTC/USDT', '15m')]
results_15m = []
n_combos_15m = sum(1 for ema in [30,50,75,100,150,200] for sl in [0.005,0.01,0.015,0.02] for tp in [0.01,0.015,0.02,0.025,0.03] if tp > sl)
for ema in [30, 50, 75, 100, 150, 200]:
    for sl in [0.005, 0.01, 0.015, 0.02]:
        for tp in [0.01, 0.015, 0.02, 0.025, 0.03]:
            if tp <= sl: continue
            sig = trendfollow_signals(df15, ema, sl, tp, cooldown_bars=10)
            res = engine.run(df15, sig, n_trials=n_combos_15m)
            m = res['metrics']
            results_15m.append({'ema':ema,'sl':sl,'tp':tp,
                'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],'dsr':m['deflated_sharpe_ratio'],
                'dd':m['max_drawdown_pct'],'wr':m['win_rate'],'trades':m['total_trades']})

results_15m.sort(key=lambda x: (x['sharpe'] if x['trades']>=6 else -999, x['net']), reverse=True)
print(f"\n  Top 15 (≥6 trades prioritized):")
for r in results_15m[:15]:
    print(f"  EMA{r['ema']:4d} SL={r['sl']*100:4.1f}% TP={r['tp']*100:4.1f}% "
          f"net={r['net']:+7.2f}% shp={r['sharpe']:+6.2f} dsr={r['dsr']:.4f} dd={r['dd']:5.1f}% "
          f"wr={r['wr']:4.0f}% #T={r['trades']}")
best_15m = results_15m[0]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRIORITY 3: RSI_MR BTC 1h
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "━" * 70)
print("PRIORITY 3: RSI_MR BTC 1h — full-range validation")
print("━" * 70)

rsi_r = []
n_rsi_combos = 2 * 3 * 4  # 2 rsi_p × 3 os/ob pairs × 4 sl/tp pairs = 24
for rsi_p in [7, 14]:
    for os_l, ob_l in [(30,70),(25,75),(35,65)]:
        for sl, tp in [(0.02,0.04),(0.02,0.06),(0.03,0.06),(0.03,0.08)]:
            sig = rsi_mr_signals(df, rsi_p, os_l, ob_l, 50, sl, tp, 5)
            res = engine.run(df, sig, n_trials=n_rsi_combos)
            m = res['metrics']
            rsi_r.append({'rsi_p':rsi_p,'os':os_l,'ob':ob_l,'sl':sl,'tp':tp,
                'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],'dsr':m['deflated_sharpe_ratio'],
                'dd':m['max_drawdown_pct'],'wr':m['win_rate'],'trades':m['total_trades']})

rsi_r.sort(key=lambda x: (x['sharpe'] if x['trades']>=3 else -999, x['net']), reverse=True)
print(f"\n  Top 10 (≥3 trades prioritized):")
for r in rsi_r[:10]:
    print(f"  RSI{r['rsi_p']} OS{r['os']} OB{r['ob']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% "
          f"net={r['net']:+7.2f}% shp={r['sharpe']:+6.2f} dsr={r['dsr']:.4f} dd={r['dd']:5.1f}% "
          f"wr={r['wr']:4.0f}% #T={r['trades']}")
best_rsi = rsi_r[0] if rsi_r else None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRIORITY 4: ETH 1h
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "━" * 70)
print("PRIORITY 4: TrendFollow ETH 1h")
print("━" * 70)

df_eth = data.get(('ETH/USDT', '1h'))
if df_eth is not None:
    eth_r = []
    n_eth_combos = sum(1 for ema in [20,30,50,75] for sl in [0.015,0.02,0.025,0.03] for tp in [0.03,0.04,0.05,0.06] if tp > sl)
    for ema in [20, 30, 50, 75]:
        for sl in [0.015, 0.02, 0.025, 0.03]:
            for tp in [0.03, 0.04, 0.05, 0.06]:
                if tp <= sl: continue
                sig = trendfollow_signals(df_eth, ema, sl, tp, cooldown_bars=8)
                res = engine.run(df_eth, sig, n_trials=n_eth_combos)
                m = res['metrics']
                eth_r.append({'ema':ema,'sl':sl,'tp':tp,
                    'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],'dsr':m['deflated_sharpe_ratio'],
                    'dd':m['max_drawdown_pct'],'wr':m['win_rate'],'trades':m['total_trades']})
    eth_r.sort(key=lambda x: (x['sharpe'] if x['trades']>=3 else -999, x['net']), reverse=True)
    print(f"\n  Top 10:")
    for r in eth_r[:10]:
        print(f"  EMA{r['ema']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% "
              f"net={r['net']:+7.2f}% shp={r['sharpe']:+6.2f} dsr={r['dsr']:.4f} dd={r['dd']:5.1f}% "
              f"wr={r['wr']:4.0f}% #T={r['trades']}")
    best_eth = eth_r[0] if eth_r else None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SUMMARY & CONFIG UPDATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
print("\n" + "═" * 70)
print("PROMETHEUS ACTIONS")
print("═" * 70)

actions = []

print(f"\n📊 TrendFollow_BTC_1h:")
print(f"   CURRENT: EMA=150 SL=3.0% TP=5.0% → 0 TRADES")
print(f"   BEST:    EMA={best_1h['ema']} SL={best_1h['sl']*100:.1f}% TP={best_1h['tp']*100:.1f}%")
print(f"            net={best_1h['net']:+.2f}% shp={best_1h['sharpe']:+.2f} dsr={best_1h['dsr']:.4f} dd={best_1h['dd']:.1f}% wr={best_1h['wr']:.0f}% #T={best_1h['trades']}")
if best_1h['sharpe'] > 0 and best_1h['trades'] >= 3:
    print(f"   ✅ UPDATE: EMA={best_1h['ema']} SL={best_1h['sl']*100:.1f}% TP={best_1h['tp']*100:.1f}%")
    actions.append(('TrendFollow_BTC_1h', best_1h['ema'], best_1h['sl'], best_1h['tp'], 8))

print(f"\n📊 TrendFollow_BTC (15m):")
print(f"   CURRENT: EMA=150 SL=1.5% TP=2.0% → -1.40%")
print(f"   BEST:    EMA={best_15m['ema']} SL={best_15m['sl']*100:.1f}% TP={best_15m['tp']*100:.1f}%")
print(f"            net={best_15m['net']:+.2f}% shp={best_15m['sharpe']:+.2f} dsr={best_15m['dsr']:.4f} dd={best_15m['dd']:.1f}% wr={best_15m['wr']:.0f}% #T={best_15m['trades']}")
if best_15m['sharpe'] > 0.2 and best_15m['trades'] >= 4:
    print(f"   ✅ UPDATE: EMA={best_15m['ema']} SL={best_15m['sl']*100:.1f}% TP={best_15m['tp']*100:.1f}%")
    actions.append(('TrendFollow_BTC', best_15m['ema'], best_15m['sl'], best_15m['tp'], 10))
else:
    print(f"   ⚠️ No viable 15m config — keep disabled")

if best_rsi:
    print(f"\n📊 RSI_MR (BTC 1h):")
    print(f"   CURRENT: DISABLED")
    print(f"   BEST: RSI={best_rsi['rsi_p']} OS={best_rsi['os']} OB={best_rsi['ob']} SL={best_rsi['sl']*100:.1f}% TP={best_rsi['tp']*100:.1f}%")
    print(f"         net={best_rsi['net']:+.2f}% shp={best_rsi['sharpe']:+.2f} dsr={best_rsi['dsr']:.4f} #T={best_rsi['trades']}")
    if best_rsi['sharpe'] > 0.8 and best_rsi['trades'] >= 3:
        print(f"   ✅ ENABLE with optimized params")

# Update strategies.yaml
if actions:
    import yaml
    with open('config/strategies.yaml') as f:
        config = yaml.safe_load(f)
    
    for name, ema, sl, tp, cd in actions:
        for s in config['strategies']:
            if s['name'] == name:
                s['params']['ema_period'] = ema
                s['params']['stop_loss_pct'] = float(sl)
                s['params']['take_profit_pct'] = float(tp)
                s['params']['cooldown_bars'] = cd
                s['enabled'] = True
                print(f"  ✅ {name}: EMA={ema} SL={sl*100:.1f}% TP={tp*100:.1f}% CD={cd}")
    
    with open('config/strategies.yaml', 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"\n  📝 strategies.yaml updated")

# Save to state/prometheus.json (merge parameter sweep findings)
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

# Build prom_data for both state and legacy files
prom_data = {
    'run_time': t0.isoformat(),
    'findings': {
        'btc_1h_best': {'ema': best_1h['ema'], 'sl': best_1h['sl'], 'tp': best_1h['tp'],
                        'net': best_1h['net'], 'sharpe': best_1h['sharpe'], 'trades': best_1h['trades']},
        'btc_15m_best': {'ema': best_15m['ema'], 'sl': best_15m['sl'], 'tp': best_15m['tp'],
                         'net': best_15m['net'], 'sharpe': best_15m['sharpe'], 'trades': best_15m['trades']},
    },
    'actions': [{'strategy': a[0], 'ema': a[1], 'sl': a[2], 'tp': a[3]} for a in actions],
    'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
}
if best_rsi:
    prom_data['findings']['rsi_best'] = {'rsi_p': best_rsi['rsi_p'], 'os': best_rsi['os'], 'ob': best_rsi['ob'],
                                          'sl': best_rsi['sl'], 'tp': best_rsi['tp'],
                                          'net': best_rsi['net'], 'sharpe': best_rsi['sharpe']}

existing['parameter_sweep'] = prom_data
existing['_updated_at'] = datetime.now(timezone.utc).isoformat()
with open(state_path, 'w') as f:
    json.dump(existing, f, indent=2, default=str)

# Also write legacy file
os.makedirs('.aether', exist_ok=True)
with open('.aether/prometheus.json', 'w') as f:
    json.dump(prom_data, f, indent=2, default=str)
print(f"💾 prometheus.json written")

elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\n⏱️ {elapsed:.0f}s | 🔥 Prometheus complete")
