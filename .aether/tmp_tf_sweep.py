#!/usr/bin/env python3
"""TrendFollow_BTC_1h targeted parameter sweep to find PAPER-viable config"""
import pandas as pd
import numpy as np
import sys, os, time, json

# Add project root AFTER standard library imports
sys.path.insert(0, "/home/rinnen/binance_quant")

from data.storage import MarketStorage
from backtest.signal_gen import trendfollow_signals
from backtest.engine import BacktestEngine

storage = MarketStorage()
df_raw = storage.load_klines("BTC/USDT", "1h")

# Timestamp conversion (match engine.py)
df = df_raw.copy()
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)
df.sort_index(inplace=True)

print(f"Loaded {len(df)} bars, {df.index[0]} → {df.index[-1]}", flush=True)

# Param grid — targeted sweep
param_grid = {
    'ema_period': [50, 75, 100],
    'stop_loss_pct': [0.01, 0.015, 0.02],
    'take_profit_pct': [0.02, 0.03, 0.04, 0.05],
    'cooldown_bars': [5, 8, 10],
}

results = []
done = 0
for ema in param_grid['ema_period']:
    for sl in param_grid['stop_loss_pct']:
        for tp in param_grid['take_profit_pct']:
            for cd in param_grid['cooldown_bars']:
                if tp <= sl:
                    continue
                done += 1
                engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
                signals = trendfollow_signals(df, ema_period=ema, sl_pct=sl, tp_pct=tp, cooldown_bars=cd)
                bt = engine.run(df, signals, leverage=3)
                m = bt['metrics']
                results.append({
                    'ema': ema, 'sl': sl, 'tp': tp, 'cd': cd,
                    'ret': round(m['total_return_pct'], 2),
                    'sr': round(m['sharpe_ratio'], 4),
                    'dd': round(m['max_drawdown_pct'], 2),
                    'wr': round(m['win_rate'], 1),
                    'trades': m['total_trades'],
                    'pf': round(m['profit_factor'], 2),
                })
                print(f"  [{done:3d}] ema={ema} sl={sl*100:.1f}% tp={tp*100:.1f}% cd={cd} → "
                      f"Ret={m['total_return_pct']:7.1f}% SR={m['sharpe_ratio']:7.3f} "
                      f"DD={m['max_drawdown_pct']:5.1f}% WR={m['win_rate']:5.0f}% T={m['total_trades']:3d}", 
                      flush=True)

results.sort(key=lambda x: x['sr'], reverse=True)

# Print summary
print(f"\n{'='*90}")
print(f"TrendFollow_BTC_1h Parameter Sweep — {len(results)} combos tested")
print(f"Data: {df.index[0]} → {df.index[-1]} ({len(df)} bars)")
print(f"{'='*90}")
print(f"Baseline (ema=50, sl=1.5%, tp=5%, cd=8): Ret=38.45% SR=0.39 DD=28.3% WR=34% T=50\n")

# PAPER candidates
candidates = [r for r in results if r['dd'] < 20 and r['sr'] > 0.4 and r['trades'] >= 30]
if candidates:
    print(f"🎯 Viable candidates (DD<20%, SR>0.4, T≥30): {len(candidates)}")
    for r in candidates[:10]:
        meets_all = r['sr'] >= 0.5 and r['wr'] >= 40
        tag = "✅ PAPER_READY" if meets_all else "⚠️ NEAR"
        print(f"  {tag} ema={r['ema']} sl={r['sl']*100:.1f}% tp={r['tp']*100:.1f}% cd={r['cd']} "
              f"→ Ret={r['ret']:7.1f}% SR={r['sr']:.3f} DD={r['dd']:5.1f}% WR={r['wr']:5.0f}% T={r['trades']:3d} PF={r['pf']:.2f}")
else:
    print("❌ No candidate meets DD<20% + SR>0.4 + T≥30")

# Top 5 by SR
print(f"\n📈 Top 5 by Sharpe:")
for r in results[:5]:
    print(f"  ema={r['ema']} sl={r['sl']*100:.1f}% tp={r['tp']*100:.1f}% cd={r['cd']} "
          f"→ Ret={r['ret']:7.1f}% SR={r['sr']:.3f} DD={r['dd']:5.1f}% WR={r['wr']:5.0f}% T={r['trades']:3d} PF={r['pf']:.2f}")

# Top 5 by DD
by_dd = sorted(results, key=lambda x: x['dd'])
print(f"\n📈 Top 5 by Drawdown:")
for r in by_dd[:5]:
    print(f"  ema={r['ema']} sl={r['sl']*100:.1f}% tp={r['tp']*100:.1f}% cd={r['cd']} "
          f"→ Ret={r['ret']:7.1f}% SR={r['sr']:.3f} DD={r['dd']:5.1f}% WR={r['wr']:5.0f}% T={r['trades']:3d} PF={r['pf']:.2f}")

# Save
sweep_file = "/home/rinnen/binance_quant/.aether/tmp_tf_sweep.json"
out = {
    "sweep_date": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "symbol": "BTC/USDT", "timeframe": "1h",
    "data_range": f"{df.index[0]} → {df.index[-1]}", "data_bars": len(df),
    "total_combos": len(results),
    "baseline": {"ret": 38.45, "sr": 0.39, "dd": 28.3, "wr": 34, "trades": 50},
    "top_by_sr": results[:15],
    "candidates": candidates[:10],
}
with open(sweep_file, "w") as f:
    json.dump(out, f, indent=2, default=str)
print(f"\n✅ Results saved to {sweep_file}")
