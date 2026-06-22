import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sqlite3, time
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

def run_one(params):
    strat = BBandMeanReversion(
        name='BB_MR_BTC',
        symbols=['BTC/USDT'],
        timeframes=['1h'],
        **{k: v for k, v in params.items() if k != 'mode'},
    )
    
    trades = []
    in_position = None
    mode = params.get('mode', 'both')
    
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
            tp_pct = params['take_profit_pct']
            sl_pct = params['stop_loss_pct']
            
            if (pos['side'] == 'LONG' and sig_type in (SignalType.CLOSE_LONG,)) or \
               (pos['side'] == 'SHORT' and sig_type in (SignalType.CLOSE_SHORT,)):
                exit_reason = True
            elif pos['side'] == 'LONG':
                if price >= pos['entry'] * (1 + tp_pct) or price <= pos['entry'] * (1 - sl_pct):
                    exit_reason = True
            else:
                if price <= pos['entry'] * (1 - tp_pct) or price >= pos['entry'] * (1 + sl_pct):
                    exit_reason = True
            
            if exit_reason:
                if pos['side'] == 'LONG':
                    pnl_pct = (price / pos['entry'] - 1) * 100 - 0.08
                else:
                    pnl_pct = (pos['entry'] / price - 1) * 100 - 0.08
                trades.append(pnl_pct)
                in_position = None
                strat._positions.pop('BTC/USDT', None)
    
    if not trades:
        return None
    
    total_ret = sum(trades)
    wr = 100 * sum(1 for t in trades if t > 0) / len(trades)
    sharpe = np.mean(trades) / max(np.std(trades), 0.01)
    cum = np.cumsum(trades)
    max_dd = abs(min(cum - np.maximum.accumulate(cum)))
    
    return {'trades': len(trades), 'return': total_ret, 'wr': wr, 'sharpe': sharpe, 'maxdd': max_dd}

# Test key combos - long_only with different bb/std
combos = [
    {'bb_period': 10, 'bb_std': 1.5, 'rsi_period': 14, 'rsi_oversold': 35, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.04, 'cooldown_bars': 3, 'mode': 'long_only'},
    {'bb_period': 10, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_oversold': 35, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.04, 'cooldown_bars': 3, 'mode': 'long_only'},
    {'bb_period': 20, 'bb_std': 1.5, 'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.04, 'cooldown_bars': 3, 'mode': 'long_only'},
    {'bb_period': 20, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 65, 'stop_loss_pct': 0.01, 'take_profit_pct': 0.03, 'cooldown_bars': 3, 'mode': 'long_only'},
    {'bb_period': 20, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.04, 'cooldown_bars': 5, 'mode': 'long_only'},
    {'bb_period': 20, 'bb_std': 2.5, 'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.05, 'cooldown_bars': 3, 'mode': 'long_only'},
    {'bb_period': 30, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.04, 'cooldown_bars': 3, 'mode': 'long_only'},
    {'bb_period': 20, 'bb_std': 2.0, 'rsi_period': 14, 'rsi_oversold': 30, 'rsi_overbought': 65, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.05, 'cooldown_bars': 5, 'mode': 'long_only'},
]

print(f'Testing {len(combos)} combos...')
results = []
for c in combos:
    t0 = time.time()
    r = run_one(c)
    elapsed = time.time() - t0
    if r:
        results.append({**c, **r})
        print(f'  bb={c["bb_period"]}/{c["bb_std"]} rsi_os={c["rsi_oversold"]} sl={c["stop_loss_pct"]*100:.0f}% tp={c["take_profit_pct"]*100:.0f}% cd={c["cooldown_bars"]} -> '
              f'{r["trades"]}t ret={r["return"]:+.2f}% wr={r["wr"]:.1f}% sh={r["sharpe"]:+.2f} dd={r["maxdd"]:.1f}% [{elapsed:.1f}s]')

results.sort(key=lambda x: x['sharpe'], reverse=True)
print(f'\n=== Best by Sharpe ===')
for r in results[:5]:
    print(f'  bb={r["bb_period"]}/{r["bb_std"]} rsi_os={r["rsi_oversold"]} sl={r["stop_loss_pct"]*100:.0f}% tp={r["take_profit_pct"]*100:.0f}% cd={r["cooldown_bars"]} '
          f'-> {r["trades"]}t ret={r["return"]:+.2f}% wr={r["wr"]:.1f}% sh={r["sharpe"]:+.2f} dd={r["maxdd"]:.1f}%')
