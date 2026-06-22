# Run from project root with: python3 .aether/tmp_bb_debug.py
import sys, os
# Add project root to path AFTER stdlib, to avoid shadowing
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
import pandas as pd
import numpy as np
from strategy.examples.bband_rsi import BBandMeanReversion
from strategy.base import SignalType

# Get 1h data
db = sqlite3.connect('data/market.db')
df = pd.read_sql_query("""
    SELECT open_time, open, high, low, close, volume 
    FROM klines 
    WHERE symbol = 'BTC/USDT' AND timeframe = '1h'
    ORDER BY open_time
""", db)
db.close()

df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)
print(f'Loaded {len(df)} bars, {df.index[0]} to {df.index[-1]}')

strat = BBandMeanReversion(
    name='BB_MR_BTC',
    symbols=['BTC/USDT'],
    timeframes=['1h'],
    bb_period=20, bb_std=2.0,
    rsi_period=14, rsi_oversold=35, rsi_overbought=65,
    stop_loss_pct=0.02, take_profit_pct=0.05,
    cooldown_bars=3,
)

# Debug last bars
for i in range(len(df)-5, len(df)):
    window = df.iloc[:i+1].copy()
    strat._preprocess('BTC/USDT', '1h', window)
    signal = strat.generate_signal('BTC/USDT')
    price = float(df['close'].iloc[i])
    key = ('BTC/USDT', '1h')
    ind = strat._indicators.get(key)
    latest = ind.iloc[-1]
    print(f'Bar {i}: price={price:.1f} rsi={latest["rsi"]:.1f} '
          f'sma={latest["sma"]:.1f} upper={latest["upper"]:.1f} lower={latest["lower"]:.1f} '
          f'touch_l={latest["touch_lower"]} touch_u={latest["touch_upper"]} '
          f'signal={signal.type.value} reason={signal.reason}')

# Full scan
ind_full = strat._indicators.get(key)
touch_lower_count = int(ind_full['touch_lower'].sum())
touch_upper_count = int(ind_full['touch_upper'].sum())
rsi_below_35 = int((ind_full['rsi'] < 35).sum())
rsi_above_65 = int((ind_full['rsi'] > 65).sum())
both_lower = int(((ind_full['touch_lower']) & (ind_full['rsi'] < 35)).sum())
both_upper = int(((ind_full['touch_upper']) & (ind_full['rsi'] > 65)).sum())
print(f'\nFull scan:')
print(f'  touch_lower: {touch_lower_count}, touch_upper: {touch_upper_count}')
print(f'  rsi<35: {rsi_below_35}, rsi>65: {rsi_above_65}')
print(f'  lower+rsi<35: {both_lower}, upper+rsi>65: {both_upper}')
