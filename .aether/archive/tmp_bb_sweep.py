import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3, itertools, json
import pandas as pd
import numpy as np
from strategy.examples.bband_rsi import BBandMeanReversion
from strategy.base import SignalType

db = sqlite3.connect('data/market.db')
df = pd.read_sql_query("""
    SELECT open_time, open, high, low, close, volume 
    FROM klines WHERE symbol = 'BTC/USDT' AND timeframe = '1h'
    ORDER BY open_time
""", db)
db.close()
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)

params = {
    'bb_period': [10, 20, 30],
    'bb_std': [1.5, 2.0, 2.5],
    'tp_pct': [0.03, 0.05, 0.07],
    'sl_pct': [0.01, 0.02, 0.03],
    'mode': ['both', 'long_only'],
}

results = []
for bb_period, bb_std, tp_pct, sl_pct, mode in itertools.product(*params.values()):
    strat = BBandMeanReversion(
        name='BB_MR_BTC',
        symbols=['BTC/USDT'],
        timeframes=['1h'],
        bb_period=bb_period, bb_std=bb_std,
        rsi_period=14, rsi_oversold=35, rsi_overbought=65,
        stop_loss_pct=sl_pct, take_profit_pct=tp_pct,
        cooldown_bars=3,
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
                in_position = {'side': 'LONG', 'entry': price, 'entry_bar': i}
                strat._positions['BTC/USDT'] = {'side': 'LONG', 'entry_price': price}
            elif sig_type == SignalType.SHORT and mode == 'both':
                in_position = {'side': 'SHORT', 'entry': price, 'entry_bar': i}
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
    
    if not trades:
        continue
    
    total_ret = sum(trades)
    wr = 100 * sum(1 for t in trades if t > 0) / len(trades)
    sharpe = np.mean(trades) / max(np.std(trades), 0.01)
    cum = np.cumsum(trades)
    max_dd = abs(min(cum - np.maximum.accumulate(cum)))
    avg_bars = 0  # simplified
    
    results.append({
        'bb_period': bb_period, 'bb_std': bb_std, 'tp_pct': tp_pct, 'sl_pct': sl_pct,
        'mode': mode, 'trades': len(trades), 'return': total_ret,
        'wr': wr, 'sharpe': sharpe, 'maxdd': max_dd,
    })

results.sort(key=lambda x: x['sharpe'], reverse=True)
print(f'Top 10 parameter combos (sorted by Sharpe):')
print(f'{"bb_p":>4} {"bb_s":>4} {"tp%":>5} {"sl%":>5} {"mode":>9} {"trades":>6} {"ret%":>8} {"WR%":>6} {"Sharpe":>7} {"MaxDD%":>7}')
print('-' * 80)
for r in results[:15]:
    print(f'{r["bb_period"]:>4} {r["bb_std"]:>4.1f} {r["tp_pct"]*100:>4.0f}% {r["sl_pct"]*100:>4.0f}% '
          f'{r["mode"]:>9} {r["trades"]:>6} {r["return"]:>+8.2f} {r["wr"]:>5.1f}% {r["sharpe"]:>+7.2f} {r["maxdd"]:>7.2f}')
