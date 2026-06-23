#!/usr/bin/env python3
"""Fix state/athena.json BandMR entries with actual metrics from backtest_results.json."""
import json
from datetime import datetime, timezone

with open('.aether/state/backtest_results.json') as f:
    bt = json.load(f)

with open('.aether/state/athena.json') as f:
    athena = json.load(f)

for name in ['BandMR_BTC', 'BandMR_ETH']:
    if name not in bt:
        print(f'{name} not in backtest_results')
        continue
    d365 = bt[name]['365d']
    athena['strategies'][name] = {
        "signals": 0,
        "status": "disabled",
        "return_pct": d365['return_pct'],
        "sharpe": d365['sharpe'],
        "win_rate": d365['win_rate'],
        "trades": d365['trades'],
        "max_dd": d365['max_dd'],
        "verdict": bt[name]['verdict'],
        "best_params": bt[name].get('params', {})
    }
    print(f'Fixed {name}: SR={d365["sharpe"]}, DD={d365["max_dd"]}%, T={d365["trades"]}')

athena['_updated_at'] = datetime.now(timezone.utc).isoformat()
with open('.aether/state/athena.json', 'w') as f:
    json.dump(athena, f, indent=2, ensure_ascii=False)
print('state/athena.json fixed')

# Also sync to live athena.json for good measure
with open('.aether/athena.json') as f:
    live = json.load(f)

for s in live.get('strategies', []):
    if s['name'] in ['BandMR_BTC', 'BandMR_ETH']:
        d365 = bt[s['name']]['365d']
        s['metrics'] = {
            'total_return_pct': d365['return_pct'],
            'sharpe_ratio': d365['sharpe'],
            'max_drawdown_pct': d365['max_dd'],
            'win_rate': d365['win_rate'],
            'profit_factor': d365.get('profit_factor', 0.0),
            'total_trades': d365['trades'],
            'avg_win_pct': 0.0,
            'avg_loss_pct': 0.0
        }
        print(f'Live {s["name"]}: SR={d365["sharpe"]}, DD={d365["max_dd"]}%, T={d365["trades"]}')

live['_updated_at'] = datetime.now(timezone.utc).isoformat()
with open('.aether/athena.json', 'w') as f:
    json.dump(live, f, indent=2, ensure_ascii=False)
print('live athena.json fixed')
print('Done')
