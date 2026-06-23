"""BTC-ETH Spread Mean Reversion Backtest

Trade the ETH/BTC ratio — go long when ratio is low (ETH undervalued vs BTC),
short when ratio is high (ETH overvalued). Uncorrelated alpha source vs existing
single-symbol MR strategies.
"""
import sqlite3, numpy as np, json
from datetime import datetime, timezone

DB = 'data/market.db'

def load_spread_data():
    db = sqlite3.connect(DB)
    btc_rows = db.execute(
        "SELECT open_time, open, high, low, close FROM klines "
        "WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time"
    ).fetchall()
    eth_rows = db.execute(
        "SELECT open_time, open, high, low, close FROM klines "
        "WHERE symbol='ETH/USDT' AND timeframe='1h' ORDER BY open_time"
    ).fetchall()
    db.close()
    
    btc_dict = {r[0]: {'o': r[1], 'h': r[2], 'l': r[3], 'c': r[4]} for r in btc_rows}
    eth_dict = {r[0]: {'o': r[1], 'h': r[2], 'l': r[3], 'c': r[4]} for r in eth_rows}
    
    common_times = sorted(set(btc_dict.keys()) & set(eth_dict.keys()))
    
    data = []
    for t in common_times:
        b = btc_dict[t]
        e = eth_dict[t]
        ratio = e['c'] / b['c']
        data.append({
            'time': t,
            'btc_close': b['c'],
            'eth_close': e['c'],
            'ratio': ratio,
        })
    return data


def compute_metrics(equity_curve, trades, initial_capital=1000.0):
    """Compute standard strategy metrics."""
    if not trades:
        return {'sharpe': 0, 'return_pct': 0, 'max_dd': 0, 'win_rate': 0, 'trades': 0}
    
    returns = np.diff(equity_curve) / equity_curve[:-1]
    returns = returns[~np.isnan(returns)]
    
    n_years = len(equity_curve) / (365 * 24)  # hourly bars
    ann_ret = (equity_curve[-1] / equity_curve[0] - 1) * 100
    sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(365*24) if np.std(returns) > 0 else 0)
    
    # Max drawdown
    peak = np.maximum.accumulate(equity_curve)
    dd = (equity_curve - peak) / peak * 100
    max_dd = float(np.min(dd))
    
    wins = [t for t in trades if t['pnl_pct'] > 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    
    profit_factor = sum(t['pnl_pct'] for t in wins) / abs(sum(t['pnl_pct'] for t in trades if t['pnl_pct'] <= 0)) if any(t['pnl_pct'] <= 0 for t in trades) else float('inf')
    
    return {
        'return_pct': round(ann_ret, 2),
        'sharpe': round(sharpe, 4),
        'max_dd': round(max_dd, 2),
        'win_rate': round(win_rate, 1),
        'trades': len(trades),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 999,
    }


def backtest_spread_mr(data, z_entry=1.5, z_exit=0.3, cooldown=3, max_hold=48,
                       stop_z=3.0, commission=0.0004):
    """Backtest a simple spread z-score mean reversion.
    
    Enter when ratio z-score crosses threshold, exit when reverts.
    Trade: LONG ETH/BTC ratio (i.e., long ETH, short BTC in proportion).
    """
    ratios = np.array([d['ratio'] for d in data])
    lookback = 200  # rolling window for z-score
    
    # Rolling z-score
    equity = 1000.0
    equity_curve = [equity]
    trades = []
    
    position = 0  # 0=none, 1=long spread (long ETH/short BTC), -1=short spread
    entry_ratio = 0
    entry_idx = 0
    last_trade_idx = -cooldown
    
    for i in range(lookback, len(data)):
        window = ratios[max(0, i-lookback):i]
        mean_ratio = np.mean(window)
        std_ratio = np.std(window)
        
        if std_ratio < 0.0001:
            continue
        
        z = (ratios[i] - mean_ratio) / std_ratio
        
        t = data[i]
        
        # Exit logic
        if position != 0:
            hold_bars = i - entry_idx
            z_exit_signal = (position == 1 and z >= -z_exit) or (position == -1 and z <= z_exit)
            stop_signal = (position == 1 and z <= -stop_z) or (position == -1 and z >= stop_z)
            time_signal = hold_bars >= max_hold
            
            if z_exit_signal or stop_signal or time_signal:
                # Close position
                ratio_return = (ratios[i] - entry_ratio) / entry_ratio * position
                pnl_pct = ratio_return * 100 - commission * 2 * 100  # 1% per % ratio move
                
                equity *= (1 + pnl_pct / 100)
                equity_curve.append(equity)
                
                exit_reason = 'z_exit' if z_exit_signal else ('stop' if stop_signal else 'time')
                trades.append({
                    'entry_time': data[entry_idx]['time'],
                    'exit_time': t['time'],
                    'direction': 'LONG_SPREAD' if position == 1 else 'SHORT_SPREAD',
                    'entry_ratio': round(entry_ratio, 6),
                    'exit_ratio': round(ratios[i], 6),
                    'pnl_pct': round(pnl_pct, 4),
                    'hold_bars': hold_bars,
                    'exit_reason': exit_reason,
                })
                
                position = 0
                last_trade_idx = i
        else:
            # Entry logic (after cooldown)
            if i - last_trade_idx < cooldown:
                equity_curve.append(equity)
                continue
            
            if z <= -z_entry:  # ETH undervalued — go long spread
                position = 1
                entry_ratio = ratios[i]
                entry_idx = i
            elif z >= z_entry:  # ETH overvalued — go short spread
                position = -1
                entry_ratio = ratios[i]
                entry_idx = i
        
        equity_curve.append(equity)
    
    # Close any open position at end
    if position != 0:
        ratio_return = (ratios[-1] - entry_ratio) / entry_ratio * position
        pnl_pct = ratio_return * 100 - commission * 2 * 100
        equity *= (1 + pnl_pct / 100)
        equity_curve.append(equity)
        trades.append({
            'entry_time': data[entry_idx]['time'],
            'exit_time': data[-1]['time'],
            'direction': 'LONG_SPREAD' if position == 1 else 'SHORT_SPREAD',
            'entry_ratio': round(entry_ratio, 6),
            'exit_ratio': round(ratios[-1], 6),
            'pnl_pct': round(pnl_pct, 4),
            'hold_bars': len(data) - entry_idx - 1,
            'exit_reason': 'end',
        })
    
    equity_curve = np.array(equity_curve)
    metrics = compute_metrics(equity_curve, trades)
    
    return metrics, trades, equity_curve


def sweep():
    data = load_spread_data()
    print(f"Data: {len(data)} bars, {datetime.fromtimestamp(data[0]['time']/1000, tz=timezone.utc)} to {datetime.fromtimestamp(data[-1]['time']/1000, tz=timezone.utc)}")
    
    # Parameter sweep
    z_entries = [1.0, 1.25, 1.5, 1.75, 2.0]
    z_exits = [0.2, 0.3, 0.5]
    cooldowns = [1, 3, 5, 8]
    max_holds = [24, 48, 72]
    stop_zs = [2.5, 3.0, 3.5]
    
    total = len(z_entries) * len(z_exits) * len(cooldowns) * len(max_holds) * len(stop_zs)
    print(f"Sweeping {total} combos...")
    
    results = []
    count = 0
    for ze in z_entries:
        for zx in z_exits:
            for cd in cooldowns:
                for mh in max_holds:
                    for sz in stop_zs:
                        m, trades, eq = backtest_spread_mr(data, ze, zx, cd, mh, sz)
                        m['params'] = f'z_entry={ze} z_exit={zx} cd={cd} max_hold={mh} stop_z={sz}'
                        m['eq_final'] = round(float(eq[-1]), 2)
                        results.append(m)
                        count += 1
    
    # Sort by Sharpe
    results.sort(key=lambda x: x['sharpe'], reverse=True)
    
    print(f"\n=== TOP 10 by Sharpe ===")
    for i, r in enumerate(results[:10]):
        print(f"  {i+1}. SR={r['sharpe']:.4f} Ret={r['return_pct']:.1f}% DD={r['max_dd']:.1f}% "
              f"WR={r['win_rate']:.1f}% T={r['trades']} PF={r['profit_factor']} {r['params']}")
    
    # Filter: DD<20% AND SR>0.5 AND T>30
    strict_pass = [r for r in results if r['max_dd'] < 20 and r['sharpe'] > 0.5 and r['trades'] > 30]
    print(f"\n=== STRICT PASS (DD<20% SR>0.5 T>30): {len(strict_pass)} ===")
    for i, r in enumerate(strict_pass[:10]):
        print(f"  {i+1}. SR={r['sharpe']:.4f} Ret={r['return_pct']:.1f}% DD={r['max_dd']:.1f}% "
              f"WR={r['win_rate']:.1f}% T={r['trades']} {r['params']}")
    
    # Balanced pass: DD<25% SR>0.4 T>20
    balanced = [r for r in results if r['max_dd'] < 25 and r['sharpe'] > 0.4 and r['trades'] > 20]
    print(f"\n=== BALANCED PASS (DD<25% SR>0.4 T>20): {len(balanced)} ===")
    for i, r in enumerate(balanced[:10]):
        print(f"  {i+1}. SR={r['sharpe']:.4f} Ret={r['return_pct']:.1f}% DD={r['max_dd']:.1f}% "
              f"WR={r['win_rate']:.1f}% T={r['trades']} {r['params']}")
    
    return results


if __name__ == '__main__':
    results = sweep()
