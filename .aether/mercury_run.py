#!/usr/bin/env python3
"""Mercury trading executor — runs on Binance testnet."""
import json, time, os, sys
from datetime import datetime, timezone

from config.settings import get_config
from execution.client import BinanceFuturesClient
from data.collector import BinanceDataCollector
from data.storage import MarketStorage
from strategy.manager import StrategyManager
from strategy.base import SignalType

cfg = get_config()
client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)
collector = BinanceDataCollector()
storage = MarketStorage()

print("=== Mercury Heartbeat ===")
print(f"Testnet: {cfg.testnet}")
print(f"Time: {datetime.now(timezone.utc).strftime('%m-%d %H:%M UTC')}")

# Step 1: Pull latest ticker prices
print("\n--- Step 1: Fetch tickers ---")
btc_ticker = client.get_ticker("BTCUSDT")
eth_ticker = client.get_ticker("ETHUSDT")
btc_last = float(btc_ticker['last'])
eth_last = float(eth_ticker['last'])
print(f"BTC/USDT: {btc_last}")
print(f"ETH/USDT: {eth_last}")

# Step 2: Fetch 15m klines (strategy timeframe)
print("\n--- Step 2: Fetch 15m klines ---")
btc_df = collector.fetch_current_klines("BTC/USDT", "15m", lookback_bars=200)
eth_df = collector.fetch_current_klines("ETH/USDT", "15m", lookback_bars=200)
print(f"BTC 15m klines: {len(btc_df)}")
print(f"ETH 15m klines: {len(eth_df)}")
storage.save_klines(btc_df, "BTC/USDT", "15m")
storage.save_klines(eth_df, "ETH/USDT", "15m")
print("Klines saved to DB")

# Step 3: Check exchange positions
print("\n--- Step 3: Check positions ---")
positions = client.get_positions()
print(f"Positions: {len(positions)}")
pos_map = {}
for p in positions:
    sym = p.get('symbol', '')
    pos_map[sym] = p
    print(f"  {sym}: side={p['side']}, qty={p['contracts']}, entry={p['entry_price']}")

open_orders_btc = client.get_open_orders("BTCUSDT")
open_orders_eth = client.get_open_orders("ETHUSDT")
all_open_orders = (open_orders_btc if isinstance(open_orders_btc, list) else []) + \
                  (open_orders_eth if isinstance(open_orders_eth, list) else [])
print(f"Open orders: {len(all_open_orders)}")

# Step 4: Load strategy and generate signals
print("\n--- Step 4: Load strategy, generate signals ---")
mgr = StrategyManager.load_from_yaml("config/strategies.yaml")
print(f"Active strategies: {mgr.get_active_strategies()}")

mgr.feed_data_only("BTC/USDT", "15m", btc_df)
mgr.feed_data_only("ETH/USDT", "15m", eth_df)

signals_btc = mgr.generate_all_signals("BTC/USDT")
signals_eth = mgr.generate_all_signals("ETH/USDT")

results = {}
for sym, sigs in [("BTC/USDT", signals_btc), ("ETH/USDT", signals_eth)]:
    for name, sig in sigs.items():
        key = f"{sym}/{name}"
        results[key] = {
            "type": sig.type.value,
            "price": sig.price,
            "reason": sig.reason,
            "confidence": sig.confidence,
            "leverage": sig.leverage,
            "quantity": sig.quantity,
            "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit,
        }
        print(f"  {key}: {sig.type.value} | {sig.reason} | price={sig.price:.2f}")

# Step 5: Execute trades
print("\n--- Step 5: Execute trades ---")
executed = []
for sym, sigs in [("BTC/USDT", signals_btc), ("ETH/USDT", signals_eth)]:
    for name, sig in sigs.items():
        if sig.type == SignalType.HOLD:
            continue

        bin_sym = sym.replace("/", "")
        current_pos = pos_map.get(bin_sym)

        # Handle CLOSE signals
        if sig.type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            if current_pos:
                print(f"  {sym}: {sig.type.value} - closing (market)")
                try:
                    close_side = "sell" if current_pos['side'] == 'long' else "buy"
                    qty = abs(float(current_pos.get('contracts', 0)))
                    order = client.place_order(bin_sym, close_side, "market", qty)
                    print(f"    Order: {order.get('id', '?')}")
                    executed.append({
                        "symbol": bin_sym,
                        "action": sig.type.value,
                        "side": close_side,
                        "qty": qty,
                        "order": str(order.get('id', '?')),
                    })
                except Exception as e:
                    print(f"    FAILED: {e}")
            else:
                print(f"  {sym}: {sig.type.value} - no position, skip")
            continue

        # Handle LONG/SHORT open signals
        if current_pos:
            existing_side = current_pos['side']
            new_side = "long" if sig.type == SignalType.LONG else "short"
            if existing_side == new_side:
                print(f"  {sym}: {sig.type.value} - same direction position exists, skip")
                continue

        qty = min(sig.quantity, 0.001) if sym == "BTC/USDT" else min(sig.quantity, 0.01)
        last_price = btc_last if sym == "BTC/USDT" else eth_last
        limit_price = round(last_price * 0.98, 1)
        side = "buy" if sig.type == SignalType.LONG else "sell"

        print(f"  {sym}: {sig.type.value} - open {side} {qty} @ {limit_price}")
        try:
            client.set_leverage(bin_sym, sig.leverage)
            order = client.place_order(bin_sym, side, "limit", qty, limit_price)
            print(f"    Order: {order.get('id', '?')} | status={order.get('status', '?')}")
            executed.append({
                "symbol": bin_sym,
                "action": sig.type.value,
                "side": side,
                "qty": qty,
                "price": limit_price,
                "leverage": sig.leverage,
                "strategy": name,
                "reason": sig.reason,
                "order_id": str(order.get('id', '?')),
                "order_status": order.get('status', '?'),
            })
            storage.log_trade({
                "symbol": bin_sym,
                "side": sig.type.value,
                "entry_price": limit_price,
                "quantity": qty,
                "strategy_name": name,
                "reason": sig.reason,
                "entry_time": time.time(),
            })
            print(f"    Logged to trades_log")
        except Exception as e:
            print(f"    FAILED: {e}")

# Step 6: Write mercury.json
print("\n--- Step 6: Write mercury.json ---")
mercury_state = {
    "status": "ok",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "btc_price": btc_last,
    "eth_price": eth_last,
    "positions": len(positions),
    "open_orders": len(all_open_orders),
    "signals": results,
    "executed": executed,
    "strategy": mgr.get_active_strategies(),
}
os.makedirs(".aether", exist_ok=True)
with open(".aether/mercury.json", "w") as f:
    json.dump(mercury_state, f, indent=2, default=str)
print("mercury.json written")

# Step 7: Generate bulletin
print("\n--- Bulletin ---")
open_trades = storage.get_open_trades()
print(f"Open trades: {len(open_trades)}")

if executed:
    status_icon = "GREEN"
    action_text = "; ".join([f"{e['symbol']} {e['action']}" for e in executed])
elif len(open_trades) > 0:
    status_icon = "BLUE"
    action_text = f"Holding ({len(open_trades)} open)"
else:
    status_icon = "BLUE"
    action_text = "HOLD"

signals_text = "; ".join([f"{k}:{v['type']}" for k, v in results.items() if v['type'] != 'HOLD'])
if not signals_text:
    signals_text = "All HOLD"

bulletin_line = (
    f"### {datetime.now(timezone.utc).strftime('%m-%d %H:%M')} — "
    f"[{status_icon}] Mercury: {action_text} | "
    f"BTC={btc_last:.1f} | ETH={eth_last:.1f} | "
    f"Signals: {signals_text} | "
    f"Orders: {len(all_open_orders)}"
)
print(bulletin_line)

# Append to bulletin.md
with open(".aether/bulletin.md", "a") as f:
    f.write(f"\n---\n{bulletin_line}\n")

print("Done.")
print(f"BULLETIN_LINE: {bulletin_line}")
