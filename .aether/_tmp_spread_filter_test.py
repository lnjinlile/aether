"""Quick comparison: Existing ETH MR entries vs spread-filtered entries.

Tests if adding BTC-ETH ratio z-score as an entry filter improves
existing ETH MR strategy performance.
"""
import sqlite3, numpy as np
from datetime import datetime, timezone

DB = 'data/market.db'

def load_aligned_data():
    db = sqlite3.connect(DB)
    
    # ETH 1h klines
    eth = db.execute(
        "SELECT open_time, open, high, low, close FROM klines "
        "WHERE symbol='ETH/USDT' AND timeframe='1h' ORDER BY open_time"
    ).fetchall()
    
    # BTC 1h klines
    btc = db.execute(
        "SELECT open_time, open, high, low, close FROM klines "
        "WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time"
    ).fetchall()
    
    db.close()
    
    btc_dict = {r[0]: r[4] for r in btc}
    eth_dict = {r[0]: r[4] for r in eth}
    
    common = sorted(set(btc_dict.keys()) & set(eth_dict.keys()))
    
    data = []
    for t in common:
        data.append({
            'time': t,
            'btc_c': btc_dict[t],
            'eth_c': eth_dict[t],
            'ratio': eth_dict[t] / btc_dict[t],
        })
    
    return data


def compute_rsi(closes, period=14):
    """Wilder RSI"""
    if len(closes) < period + 1:
        return np.full(len(closes), 50.0)
    
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.zeros(len(closes))
    avg_loss = np.zeros(len(closes))
    avg_gain[period] = np.mean(gains[:period])
    avg_loss[period] = np.mean(losses[:period])
    
    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i-1] * (period-1) + gains[i-1]) / period
        avg_loss[i] = (avg_loss[i-1] * (period-1) + losses[i-1]) / period
    
    rs = avg_gain / np.where(avg_loss == 0, 1e-10, avg_loss)
    rsi = 100 - (100 / (1 + rs))
    rsi[:period] = 50
    return rsi


def compute_donchian(highs, lows, period=10):
    """Donchian channel"""
    from collections import deque
    dc_high = np.zeros(len(highs))
    dc_low = np.zeros(len(lows))
    
    h_window = deque(maxlen=period)
    l_window = deque(maxlen=period)
    
    for i in range(len(highs)):
        h_window.append(highs[i])
        l_window.append(lows[i])
        dc_high[i] = max(h_window)
        dc_low[i] = min(l_window)
    
    return dc_low, dc_high  # lower, upper


def backtest_eth_mr(data, rsi_period=14, oversold=20, overbought=70,
                    exit_rsi=45, sl_pct=1.5, tp_pct=6.0, cd=3,
                    use_spread_filter=False, spread_z_entry=-1.5,
                    spread_lookback=200):
    """Backtest ETH RSI MR with optional spread filter."""
    closes = np.array([d['eth_c'] for d in data])
    highs = np.array([d['eth_c'] for d in data])  # simplified - use close
    lows = np.array([d['eth_c'] for d in data])
    ratios = np.array([d['ratio'] for d in data])
    
    rsi = compute_rsi(closes, rsi_period)
    
    # Spread z-score
    ratio_z = np.zeros(len(data))
    for i in range(spread_lookback, len(data)):
        window = ratios[max(0, i-spread_lookback):i]
        m = np.mean(window)
        s = np.std(window)
        if s > 0.0001:
            ratio_z[i] = (ratios[i] - m) / s
    
    equity = 1000.0
    eq_curve = [equity]
    trades = []
    
    position = 0
    entry_price = 0
    last_trade = -cd
    
    for i in range(max(rsi_period, spread_lookback), len(data)):
        p = data[i]
        
        if position == 0:
            if i - last_trade < cd:
                eq_curve.append(equity)
                continue
            
            # Entry conditions
            rsi_ok = rsi[i] < oversold
            spread_ok = (not use_spread_filter) or (ratio_z[i] < spread_z_entry)
            
            if rsi_ok and spread_ok:
                position = 1
                entry_price = p['eth_c']
        else:
            # Exit conditions
            sl_price = entry_price * (1 - sl_pct/100)
            tp_price = entry_price * (1 + tp_pct/100)
            
            exit_signal = (rsi[i] > exit_rsi) or (p['eth_c'] <= sl_price) or (p['eth_c'] >= tp_price)
            
            if exit_signal:
                pnl_pct = (p['eth_c'] - entry_price) / entry_price * 100
                equity *= (1 + pnl_pct / 100)
                
                trades.append({
                    'pnl_pct': pnl_pct,
                    'exit': 'rsi' if rsi[i] > exit_rsi else ('sl' if p['eth_c'] <= sl_price else 'tp'),
                })
                position = 0
                last_trade = i
        
        eq_curve.append(equity)
    
    # Close at end
    if position == 1:
        pnl = (data[-1]['eth_c'] - entry_price) / entry_price * 100
        equity *= (1 + pnl / 100)
        trades.append({'pnl_pct': pnl, 'exit': 'end'})
    
    eq_curve = np.array(eq_curve)
    
    if not trades:
        return {'sharpe': 0, 'return_pct': 0, 'max_dd': 0, 'win_rate': 0, 'trades': 0}
    
    rets = np.diff(eq_curve) / eq_curve[:-1]
    rets = rets[~np.isnan(rets)]
    
    ann_ret = (eq_curve[-1] / eq_curve[0] - 1) * 100
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(365*24)) if np.std(rets) > 0 else 0
    peak = np.maximum.accumulate(eq_curve)
    dd = (eq_curve - peak) / peak * 100
    max_dd = float(np.min(dd))
    wins = [t for t in trades if t['pnl_pct'] > 0]
    wr = len(wins) / len(trades) * 100
    
    return {
        'sharpe': round(sharpe, 4),
        'return_pct': round(ann_ret, 1),
        'max_dd': round(max_dd, 2),
        'win_rate': round(wr, 1),
        'trades': len(trades),
    }


if __name__ == '__main__':
    data = load_aligned_data()
    print(f"Data: {len(data)} bars")
    
    # RSI_MR_ETH baseline (current params)
    print("\n=== RSI_MR_ETH BASELINE (no spread filter) ===")
    base = backtest_eth_mr(data, oversold=20)
    print(f"  SR={base['sharpe']:.4f} Ret={base['return_pct']:.1f}% DD={base['max_dd']:.1f}% "
          f"WR={base['win_rate']:.1f}% T={base['trades']}")
    
    # With spread filter at various thresholds
    print("\n=== RSI_MR_ETH + SPREAD FILTER ===")
    for z in [-0.5, -0.75, -1.0, -1.25, -1.5, -1.75, -2.0]:
        r = backtest_eth_mr(data, oversold=20, use_spread_filter=True, spread_z_entry=z)
        print(f"  z_entry={z:+.1f}: SR={r['sharpe']:.4f} Ret={r['return_pct']:.1f}% "
              f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.1f}% T={r['trades']}")
    
    # BandMR_ETH style (higher oversold=30 + spread)
    print("\n=== BandMR STYLE (oversold=30) + SPREAD FILTER ===")
    for z in [-0.5, -0.75, -1.0, -1.25, -1.5]:
        r = backtest_eth_mr(data, oversold=30, exit_rsi=50, sl_pct=1.5, tp_pct=1.5, cd=3,
                           use_spread_filter=True, spread_z_entry=z)
        print(f"  z_entry={z:+.1f}: SR={r['sharpe']:.4f} Ret={r['return_pct']:.1f}% "
              f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.1f}% T={r['trades']}")
    
    # Baseline for BandMR style (no filter)
    print("\n  BASELINE (no filter):")
    r = backtest_eth_mr(data, oversold=30, exit_rsi=50, sl_pct=1.5, tp_pct=1.5, cd=3)
    print(f"  SR={r['sharpe']:.4f} Ret={r['return_pct']:.1f}% "
          f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.1f}% T={r['trades']}")
