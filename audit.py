import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/rinnen/binance_quant')
from config.settings import get_config
from data.collector import BinanceDataCollector
from strategy.examples.ma_cross import MACrossoverStrategy
from strategy.examples.rsi_mean_reversion import RSIMeanReversionStrategy
from strategy.base import SignalType

cfg = get_config()
collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)

def backtest_strategy(strategy, df, sym, min_bars=50):
    tf = strategy.timeframes[0]
    strategy.feed_data(sym, tf, df)
    trades = []
    pos, entry = None, 0
    for i in range(min_bars, len(df)):
        window = df.iloc[:i+1]
        strategy.feed_data(sym, tf, window)
        sig = strategy.generate_signal(sym)
        close = float(df.iloc[i]['close'])
        if sig.type == SignalType.LONG and pos is None:
            pos, entry = 'LONG', close
            trades.append({'side':'LONG','entry':close,'exit':None,'pnl':None})
        elif sig.type == SignalType.SHORT and pos is None:
            pos, entry = 'SHORT', close
            trades.append({'side':'SHORT','entry':close,'exit':None,'pnl':None})
        elif sig.type == SignalType.CLOSE_LONG and pos == 'LONG':
            trades[-1]['exit'] = close
            trades[-1]['pnl'] = (close - entry) / entry * 100
            pos = None
        elif sig.type == SignalType.CLOSE_SHORT and pos == 'SHORT':
            trades[-1]['exit'] = close
            trades[-1]['pnl'] = (entry - close) / entry * 100
            pos = None
    return trades

print('Aether Strategy Audit\n')

# Pull data once
df_1h = collector.fetch_current_klines('BTC/USDT', '1h', 500)
df_15m = collector.fetch_current_klines('BTC/USDT', '15m', 500)
trend = (float(df_1h.iloc[-1]['close'])/float(df_1h.iloc[0]['close'])-1)*100
print('BTC/USDT: 500 bars 1h, trend=%+.1f%%, latest=%.1f' % (trend, float(df_1h.iloc[-1]['close'])))
print()

# Track best
best_name = ''
best_net = -999

results = []

for fast, slow in [(3,15), (5,20), (7,25), (10,30)]:
    for tf_name, df in [('1h', df_1h), ('15m', df_15m)]:
        ma = MACrossoverStrategy(symbols=['BTC/USDT'], timeframes=[tf_name], fast_period=fast, slow_period=slow, cooldown_bars=2)
        trades = backtest_strategy(ma, df, 'BTC/USDT')
        closed = [t for t in trades if t.get('pnl') is not None]
        if closed:
            net = sum(t['pnl'] for t in closed) - len(closed)*0.08
            wr = sum(1 for t in closed if t['pnl']>0)/len(closed)*100
            name = 'MA(%d,%d) %s' % (fast, slow, tf_name)
            results.append((name, net, wr, len(closed)))
            if net > best_net:
                best_net = net
                best_name = name

for rsi_p, lo, hi in [(14,30,70), (14,25,75), (7,35,65), (21,20,80)]:
    for tf_name, df in [('1h', df_1h), ('15m', df_15m)]:
        rsi = RSIMeanReversionStrategy(symbols=['BTC/USDT'], timeframes=[tf_name], rsi_period=rsi_p, oversold=lo, overbought=hi, cooldown_bars=2)
        trades = backtest_strategy(rsi, df, 'BTC/USDT')
        closed = [t for t in trades if t.get('pnl') is not None]
        if closed:
            net = sum(t['pnl'] for t in closed) - len(closed)*0.08
            wr = sum(1 for t in closed if t['pnl']>0)/len(closed)*100
            name = 'RSI(%d,%d,%d) %s' % (rsi_p, lo, hi, tf_name)
            results.append((name, net, wr, len(closed)))
            if net > best_net:
                best_net = net
                best_name = name

results.sort(key=lambda x: x[1], reverse=True)

print('Strategy ranking (by net return, after 0.08%% fees):')
print('%-30s %8s %6s %5s' % ('Name', 'Net%', 'Win%', '#Trades'))
print('-' * 52)
for name, net, wr, nt in results:
    flag = ' << BEST' if name == best_name else ''
    print('%-30s %+7.1f%% %5.0f%% %5d%s' % (name, net, wr, nt, flag))

print()
print('=' * 52)
print('CONCLUSION:')
if best_net > 0:
    print('Best strategy: %s with net return %+.1f%%' % (best_name, best_net))
    print('This has positive edge and could be deployed on testnet.')
else:
    print('ALL strategies show NEGATIVE net return after fees.')
    print('Best was %s at %+.1f%% - still losing money.' % (best_name, best_net))
    print()
    print('NEXT STEPS:')
    print('1. Need more data (pull 90+ days of 1h candles)')
    print('2. Add volume/volatility filters to reduce false signals')
    print('3. Consider multi-timeframe confirmation')
    print('4. Add market regime detection (trending vs ranging)')
    print('5. Test funding-rate based strategies on futures')
