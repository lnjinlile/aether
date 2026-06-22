"""BB_MR_BTC v2 sweep — TP/SL-only exits, wider bands, better R:R"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3, itertools, json, time
import pandas as pd
import numpy as np
from strategy.examples.bband_rsi import BBandMeanReversion
from strategy.base import SignalType

t0 = time.time()
db = sqlite3.connect('data/market.db')
df = pd.read_sql_query("""
    SELECT open_time, open, high, low, close, volume 
    FROM klines WHERE symbol = 'BTC/USDT' AND timeframe = '1h'
    ORDER BY open_time
""", db)
db.close()
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)
print(f"Data: {len(df)} bars, {df.index[0]} to {df.index[-1]}")

# v2: wider params, better R:R
params = {
    'bb_period': [20, 30, 40],
    'bb_std': [2.0, 2.5, 3.0],
    'tp_pct': [0.04, 0.05, 0.06, 0.07],
    'sl_pct': [0.015, 0.02, 0.025],
    'mode': ['both', 'long_only'],
    'rsi_oversold': [25, 30, 35],
}

total_combos = 1
for v in params.values():
    total_combos *= len(v)
print(f"Testing {total_combos} parameter combos...")

results = []
for bb_period, bb_std, tp_pct, sl_pct, mode, rsi_os in itertools.product(*params.values()):
    strat = BBandMeanReversion(
        name='BB_MR_BTC',
        symbols=['BTC/USDT'],
        timeframes=['1h'],
        bb_period=bb_period, bb_std=bb_std,
        rsi_period=14, rsi_oversold=rsi_os, rsi_overbought=100-rsi_os,
        stop_loss_pct=sl_pct, take_profit_pct=tp_pct,
        cooldown_bars=5,
    )
    
    trades = []
    in_position = None
    
    for i in range(200, len(df)):
        window = df.iloc[:i+1].copy()
        strat.feed_data('BTC/USDT', '1h', window)
        signal = strat.generate_signal('BTC/USDT')
        price = float(df['close'].iloc[i])
        sig_type = signal.type
        
        if in_position is None:
            if sig_type == SignalType.LONG:
                in_position = {'side': 'LONG', 'entry': price}
                strat._positions['BTC/USDT'] = {'side': 'LONG', 'entry_price': price}
            elif sig_type == SignalType.SHORT and mode == 'both':
                in_position = {'side': 'SHORT', 'entry': price}
                strat._positions['BTC/USDT'] = {'side': 'SHORT', 'entry_price': price}
        else:
            exit_reason = None
            pos = in_position
            
            if (pos['side'] == 'LONG' and sig_type == SignalType.CLOSE_LONG) or \
               (pos['side'] == 'SHORT' and sig_type == SignalType.CLOSE_SHORT):
                exit_reason = 'Signal'
            elif pos['side'] == 'LONG':
                if price >= pos['entry'] * (1 + tp_pct):
                    exit_reason = 'TP'
                elif price <= pos['entry'] * (1 - sl_pct):
                    exit_reason = 'SL'
            else:
                if price <= pos['entry'] * (1 - tp_pct):
                    exit_reason = 'TP'
                elif price >= pos['entry'] * (1 + sl_pct):
                    exit_reason = 'SL'
            
            if exit_reason:
                if pos['side'] == 'LONG':
                    pnl_pct = (price / pos['entry'] - 1) * 100 - 0.08
                else:
                    pnl_pct = (pos['entry'] / price - 1) * 100 - 0.08
                trades.append(pnl_pct)
                in_position = None
                strat._positions.pop('BTC/USDT', None)
    
    if len(trades) < 10:
        continue
    
    total_ret = sum(trades)
    wr = 100 * sum(1 for t in trades if t > 0) / len(trades)
    mean_r = np.mean(trades)
    std_r = max(np.std(trades), 0.01)
    sharpe = mean_r / std_r * np.sqrt(len(trades))
    cum = np.cumsum(trades)
    cummax = np.maximum.accumulate(cum)
    max_dd = abs(min(cum - cummax))
    
    # Profit factor
    wins = [t for t in trades if t > 0]
    losses = [abs(t) for t in trades if t < 0]
    pf = sum(wins)/sum(losses) if losses else 999
    
    results.append({
        'bb_period': bb_period, 'bb_std': bb_std, 'tp_pct': tp_pct, 'sl_pct': sl_pct,
        'mode': mode, 'rsi_os': rsi_os, 'trades': len(trades), 'return': total_ret,
        'wr': wr, 'sharpe': sharpe, 'maxdd': max_dd, 'pf': pf,
    })

results.sort(key=lambda x: x['sharpe'], reverse=True)
print(f"\nTop 15 parameter combos (sorted by Sharpe):")
print(f"{'bb_p':>4} {'bb_s':>4} {'tp%':>5} {'sl%':>5} {'rsi':>4} {'mode':>9} {'#tr':>6} {'ret%':>8} {'WR%':>6} {'Sharpe':>7} {'PF':>6} {'MaxDD%':>7}")
print('-' * 95)
for r in results[:15]:
    print(f'{r["bb_period"]:>4} {r["bb_std"]:>4.1f} {r["tp_pct"]*100:>4.0f}% {r["sl_pct"]*100:>4.1f}% '
          f'{r["rsi_os"]:>4} {r["mode"]:>9} {r["trades"]:>6} {r["return"]:>+8.2f} {r["wr"]:>5.1f}% '
          f'{r["sharpe"]:>+7.2f} {r["pf"]:>5.2f} {r["maxdd"]:>7.2f}')

# Save top 3 for inspection
print(f"\nTop 3 best combos:")
for i, r in enumerate(results[:3]):
    print(f"  #{i+1}: bb_period={r['bb_period']} bb_std={r['bb_std']} tp={r['tp_pct']*100:.0f}% sl={r['sl_pct']*100:.1f}% "
          f"rsi_os={r['rsi_os']} mode={r['mode']} | {r['trades']}tr {r['return']:+.2f}% WR={r['wr']:.1f}% "
          f"Sharpe={r['sharpe']:.2f} PF={r['pf']:.2f} DD={r['maxdd']:.1f}%")

print(f"\nSweep done in {time.time()-t0:.1f}s")
