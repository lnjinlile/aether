#!/usr/bin/env python3
"""Mercury auto-execution script — runs every 15m via cron."""

import sys, os
# Fix: .aether/platform.py shadows stdlib 'platform' module (pandas needs it).
# Remove .aether/ from sys.path[0] when running from within .aether/ dir.
_script_dir = os.path.dirname(os.path.abspath(__file__))
if sys.path[0] == _script_dir or sys.path[0] == '':
    # Pop the script dir so stdlib 'platform' resolves correctly
    if sys.path[0] in (_script_dir, ''):
        sys.path.pop(0)
    # Ensure project root is first for our package imports
    _project_root = os.path.dirname(_script_dir)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

import json
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from config.settings import get_config
from execution.client import BinanceFuturesClient
from data.collector import BinanceDataCollector
from strategy.manager import StrategyManager
from strategy.base import SignalType

print('=== Mercury Init ===')
cfg = get_config()
client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)
collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
mgr = StrategyManager.load_from_yaml('config/strategies.yaml')
print(f'Strategies: {mgr.get_active_strategies()}')
print(f'Testnet: {cfg.testnet}')
print()

# ---- Step 1: Get current positions ----
print('=== Current Positions ===')
def normalize_pos_symbol(raw: str) -> str:
    """Normalize position symbol: 'BTC/USDT:USDT' -> 'BTCUSDT'"""
    # Strip ccxt settlement suffix (':USDT')
    if ':' in raw:
        raw = raw.split(':')[0]
    return raw.replace('/', '')

positions = client.get_positions()
pos_map = {}
for p in positions:
    sym = normalize_pos_symbol(p.get('symbol', ''))
    pos_map[sym] = p
    print(f"  {sym}: {p['side']} {p['contracts']} @ {p['entry_price']:.1f} | uPNL: {p['unrealized_pnl']:.4f} | lev: {p['leverage']}x")
if not positions:
    print('  No positions')

balance = client.get_balance()
print(f"Balance: {balance['balance']:.2f} USDT | Available: {balance['available']:.2f}")
print()

# ---- Step 2: Fetch data and generate signals ----
print('=== Signal Generation ===')
all_signals = {}
for sym in ['BTC/USDT', 'ETH/USDT']:
    try:
        df = collector.fetch_current_klines(sym, '15m', 500)
        print(f'{sym}: {len(df)} 15m candles, close={df.iloc[-1]["close"]:.1f}')
        mgr.feed_data_only(sym, '15m', df)
    except Exception as e:
        print(f'{sym}: data fetch failed - {e}')
        continue

for sym in ['BTC/USDT', 'ETH/USDT']:
    signals = mgr.generate_all_signals(sym)
    all_signals[sym] = signals
    for name, sig in signals.items():
        print(f'  {sym} {name}: {sig.type.value} @ {sig.price:.1f} -- {sig.reason}')
print()

# ---- Step 3: Check for exit signals ----
print('=== Position Management ===')
exit_actions = []
for sym_ccxt in ['BTC/USDT', 'ETH/USDT']:
    bin_sym = sym_ccxt.replace('/', '')
    pos = pos_map.get(bin_sym)
    if not pos:
        continue

    sym_signals = all_signals.get(sym_ccxt, {})
    has_exit = False
    exit_reason = ''

    for name, sig in sym_signals.items():
        if pos['side'] == 'long' and sig.type == SignalType.CLOSE_LONG:
            has_exit = True
            exit_reason = sig.reason
        elif pos['side'] == 'short' and sig.type == SignalType.CLOSE_SHORT:
            has_exit = True
            exit_reason = sig.reason

    if has_exit:
        df = collector.fetch_current_klines(sym_ccxt, '15m', 1)
        price = float(df.iloc[-1]['close'])
        qty = float(pos['contracts'])
        side = 'BUY' if pos['side'] == 'short' else 'SELL'

        try:
            order = client.place_order(bin_sym, side.lower(), 'market', qty, reduce_only=True)
            entry = float(pos['entry_price'])
            if pos['side'] == 'long':
                pnl = (price - entry) * qty
            else:
                pnl = (entry - price) * qty
            pnl_pct = pnl / (entry * qty / float(pos.get('leverage', 3))) * 100

            print('+------------------------------------+')
            print(f'|  Aether CLOSE {pos["side"].upper():>4s} {sym_ccxt:<10s} |')
            print(f'|  Exit:   {price:>10.1f} USDT         |')
            print(f'|  Entry:  {entry:>10.1f} USDT         |')
            print(f'|  Qty:    {qty:>10.4f}                |')
            print(f'|  PnL:    {pnl:>+10.4f} USDT         |')
            print(f'|  PnL%:   {pnl_pct:>+10.2f}%               |')
            print(f'|  Reason: {exit_reason:<20s}   |')
            print(f'|  Order:  {str(order.get("id","?")):<20s}   |')
            print('+------------------------------------+')

            exit_actions.append({
                'symbol': bin_sym, 'action': f'CLOSE_{pos["side"].upper()}',
                'price': price, 'qty': qty, 'pnl': pnl, 'pnl_pct': pnl_pct,
                'reason': exit_reason, 'order_id': str(order.get('id', '?'))
            })
            del pos_map[bin_sym]
        except Exception as e:
            print(f'  !! Close failed {bin_sym}: {e}')
    else:
        print(f'  {bin_sym}: holding {pos["side"]} {pos["contracts"]} -- no exit signal')

# ---- Step 4: Open new positions ----
print()
print('=== Entry Execution ===')
new_trades = []
for sym_ccxt in ['BTC/USDT', 'ETH/USDT']:
    bin_sym = sym_ccxt.replace('/', '')
    sym_signals = all_signals.get(sym_ccxt, {})
    existing = pos_map.get(bin_sym, {})

    for name, sig in sym_signals.items():
        if sig.type == SignalType.HOLD:
            continue
        if sig.type not in (SignalType.LONG, SignalType.SHORT):
            continue

        # Check same-direction
        if (sig.type == SignalType.LONG and existing.get('side') == 'long') or \
           (sig.type == SignalType.SHORT and existing.get('side') == 'short'):
            print(f'  {sym_ccxt}: same-direction position exists, skip {sig.type.value}')
            continue

        # Reverse position exists
        if existing and existing.get('side') in ('long', 'short'):
            print(f'  {sym_ccxt}: reverse position {existing["side"]}, skip entry')
            continue

        df = collector.fetch_current_klines(sym_ccxt, '15m', 1)
        price = float(df.iloc[-1]['close'])
        qty = 0.001
        lev = 3
        notional = qty * price
        margin = notional / lev

        sl = sig.stop_loss if not np.isnan(float(sig.stop_loss)) else price * (0.98 if sig.type == SignalType.LONG else 1.02)
        tp = sig.take_profit if not np.isnan(float(sig.take_profit)) else price * (1.04 if sig.type == SignalType.LONG else 0.96)
        mmr = 0.005
        liq = price * (1 - 1/lev + mmr) if sig.type == SignalType.LONG else price * (1 + 1/lev - mmr)
        liq_dist = abs(liq - price) / price * 100

        side = 'BUY' if sig.type == SignalType.LONG else 'SELL'

        try:
            client.set_leverage(bin_sym, lev)
            limit_px = round(price * (0.999 if sig.type == SignalType.LONG else 1.001), 1)
            order = client.place_order(bin_sym, side.lower(), 'limit', qty, limit_px)

            print('+------------------------------------+')
            print(f'|  Aether {sig.type.value:>4s} {sym_ccxt:<10s}       |')
            print(f'|  Price:  {price:>10.1f} USDT         |')
            print(f'|  Limit:  {limit_px:>10.1f} USDT         |')
            print(f'|  Qty:    {qty:>10.4f}                |')
            print(f'|  Notion: {notional:>10.2f} USDT         |')
            print(f'|  Margin: {margin:>10.2f} ({lev}x)       |')
            print(f'|  SL:     {sl:>10.1f}                |')
            print(f'|  TP:     {tp:>10.1f}                |')
            print(f'|  Liq:    {liq:>10.1f} ({liq_dist:.1f}%)       |')
            print(f'|  Strat:  {name:<20s}   |')
            print(f'|  Reason: {sig.reason:<20s}   |')
            print(f'|  Order:  {str(order.get("id","?")):<20s}   |')
            print('+------------------------------------+')

            new_trades.append({
                'symbol': bin_sym, 'action': sig.type.value, 'side': side.lower(),
                'price': price, 'limit_px': limit_px, 'qty': qty, 'leverage': lev,
                'notional': notional, 'margin': margin, 'sl': sl, 'tp': tp,
                'liq': liq, 'liq_dist': liq_dist, 'strategy': name,
                'reason': sig.reason, 'order_id': str(order.get('id', '?'))
            })
        except Exception as e:
            print(f'  !! Entry failed {sym_ccxt} {sig.type.value}: {e}')

# ---- Step 5: Write state ----
print()
print('=== State Update ===')
final_positions = client.get_positions()
final_balance = client.get_balance()

# Get BTC price
btc_price = None
try:
    df_btc = collector.fetch_current_klines('BTC/USDT', '15m', 1)
    btc_price = float(df_btc.iloc[-1]['close'])
except:
    pass

mercury_state = {
    'status': 'ok',
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'btc_price': btc_price,
    'balance': final_balance['balance'],
    'available': final_balance['available'],
    'positions': len(final_positions),
    'open_orders': len(new_trades),
    'action': 'EXECUTED' if (exit_actions or new_trades) else 'MONITORING',
    'signals': {},
    'executed': new_trades,
    'exits': exit_actions,
    'current_positions': final_positions,
}

# Record signals
for sym, sigs in all_signals.items():
    for name, sig in sigs.items():
        if sig.type.value != 'HOLD':
            key = f'{sym}/{name}'
            mercury_state['signals'][key] = {
                'type': sig.type.value,
                'price': sig.price,
                'reason': sig.reason,
                'confidence': sig.confidence,
                'leverage': sig.leverage,
                'quantity': sig.quantity,
                'stop_loss': sig.stop_loss,
                'take_profit': sig.take_profit,
            }

with open('.aether/mercury.json', 'w') as f:
    json.dump(mercury_state, f, indent=2, default=str)
print('mercury.json updated')

# ---- Step 6: Append bulletin ----
now_utc = datetime.now(timezone.utc)
ts = now_utc.strftime('%m-%d %H:%M')

lines = []
if exit_actions:
    for e in exit_actions:
        lines.append(f'### {ts} -- Mercury: {e["action"]} {e["symbol"]} @ {e["price"]:.1f} | PnL={e["pnl"]:+.4f} ({e["pnl_pct"]:+.2f}%)')
if new_trades:
    for t in new_trades:
        lines.append(f'### {ts} -- Mercury: {t["action"]} {t["symbol"]} @ {t["price"]:.1f} ({t["reason"]})')

if not lines:
    lines.append(f'### {ts} -- Mercury: heartbeat -- monitoring | positions:{len(final_positions)} | BTC~{btc_price or "?"}')

for line in lines:
    with open('.aether/bulletin.md', 'a') as f:
        f.write(line + ' \n')
    print(f'bulletin: {line}')

print()
print('=== Mercury Complete ===')
print(f'Actions: {len(exit_actions)} exits | {len(new_trades)} entries | {len(final_positions)} holding')
