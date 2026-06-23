#!/usr/bin/env python3
"""Sync BandMR backtest results to live state files."""
import json
from datetime import datetime, timezone

# 1. Sync athena.json
athena_path = '.aether/athena.json'
state_athena_path = '.aether/state/athena.json'

with open(athena_path) as f:
    live = json.load(f)
with open(state_athena_path) as f:
    state = json.load(f)

strategies_list = live.get('strategies', [])
existing_names = {s['name'] for s in strategies_list}
print(f'Existing strategies ({len(existing_names)}): {sorted(existing_names)}')

state_strats = state.get('strategies', {})
added = 0
for name in ['BandMR_BTC', 'BandMR_ETH']:
    if name not in state_strats:
        print(f'{name} NOT in state!')
        continue
    if name in existing_names:
        print(f'{name} already in live! Updating...')
        # Find and update
        for s in strategies_list:
            if s['name'] == name:
                ss = state_strats[name]
                s['metrics'] = {
                    'total_return_pct': ss['return_pct'],
                    'sharpe_ratio': ss['sharpe'],
                    'max_drawdown_pct': ss['max_dd'],
                    'win_rate': ss['win_rate'],
                    'profit_factor': 0.0,
                    'total_trades': ss['trades'],
                    'avg_win_pct': 0.0,
                    'avg_loss_pct': 0.0
                }
                s['flags'] = []
                if ss['verdict'] == 'PAPER':
                    if ss['sharpe'] < 0.5:
                        s['flags'].append(f'SHARPE < 0.5 ({ss["sharpe"]:.3f})')
                    if ss['max_dd'] > 20:
                        s['flags'].append(f'DD > 20% ({ss["max_dd"]:.1f}%)')
                break
        continue
    
    ss = state_strats[name]
    entry = {
        'name': name,
        'enabled': False,
        'symbol': 'BTC/USDT' if 'BTC' in name else 'ETH/USDT',
        'tf': '1h',
        'status': 'OK',
        'metrics': {
            'total_return_pct': ss['return_pct'],
            'sharpe_ratio': ss['sharpe'],
            'max_drawdown_pct': ss['max_dd'],
            'win_rate': ss['win_rate'],
            'profit_factor': 0.0,
            'total_trades': ss['trades'],
            'avg_win_pct': 0.0,
            'avg_loss_pct': 0.0
        },
        'flags': [],
        'bars': 8761
    }
    if ss['verdict'] == 'PAPER':
        if ss['sharpe'] < 0.5:
            entry['flags'].append(f'SHARPE < 0.5 ({ss["sharpe"]:.3f})')
        if ss['max_dd'] > 20:
            entry['flags'].append(f'DD > 20% ({ss["max_dd"]:.1f}%)')
    
    strategies_list.append(entry)
    added += 1
    print(f'Added {name}: verdict={ss["verdict"]}, SR={ss["sharpe"]}, DD={ss["max_dd"]}%, T={ss["trades"]}')

live['strategies'] = strategies_list
live['_updated_at'] = datetime.now(timezone.utc).isoformat()
live['_bandmr_synced'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

with open(athena_path, 'w') as f:
    json.dump(live, f, indent=2, ensure_ascii=False)
print(f'athena.json synced: {len(strategies_list)} strategies ({added} added)')

# 2. Sync prometheus.json - add BandMR sweep to _history.sweeps
prom_path = '.aether/prometheus.json'
state_prom_path = '.aether/state/prometheus.json'

with open(prom_path) as f:
    live_prom = json.load(f)
with open(state_prom_path) as f:
    state_prom = json.load(f)

# Copy BandMR sweep data from state
if 'active_research' in state_prom and 'PERF-058' in state_prom['active_research']:
    perf = state_prom['active_research']['PERF-058']
    
    if '_history' not in live_prom:
        live_prom['_history'] = {}
    if 'sweeps' not in live_prom['_history']:
        live_prom['_history']['sweeps'] = {}
    
    # Get detailed results from backtest_results.json
    with open('.aether/state/backtest_results.json') as f:
        bt = json.load(f)
    
    live_prom['_history']['sweeps']['band_mr_sweep'] = {
        'date': perf.get('deployed', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')),
        'runs': perf.get('total_runs', 1944),
        'time_sec': perf.get('elapsed_s', 21.7),
        'request': '#175',
        'param_grid': 'DP[10,15,20,25] x OS[25,30,35] x SL[0.5,1.0,1.5]% x TP[2,2.5,3]% x CD[5,8,10]',
        'volume_filter': 1.2,
        'verdicts': {
            'BandMR_BTC': 'PAPER',
            'BandMR_ETH': 'PAPER'
        },
        'conclusion': 'BTC SR={} < 0.5 PAPER; ETH SR={} but DD={} > 20% PAPER. Relaxing RSI from 20 to 30 does not improve Sharpe.'.format(
            bt['BandMR_BTC']['365d']['sharpe'],
            bt['BandMR_ETH']['365d']['sharpe'],
            bt['BandMR_ETH']['365d']['max_dd']
        )
    }
    
    live_prom['_updated_at'] = datetime.now(timezone.utc).isoformat()
    
    with open(prom_path, 'w') as f:
        json.dump(live_prom, f, indent=2, ensure_ascii=False)
    print(f'prometheus.json synced: band_mr_sweep added to _history.sweeps')

print('Done.')
