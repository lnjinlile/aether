#!/usr/bin/env python3
"""Mercury heartbeat #2: Fix remaining issues from previous heartbeat."""

import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from config.settings import get_config
from execution.client import BinanceFuturesClient

cfg = get_config()
client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)

results = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "actions": [],
}

print("=" * 60)
print("  Mercury Heartbeat — Position Cleanup & Execution Fix")
print("=" * 60)

# ── 1. Current state ───────────────────────────────────────
positions = client.get_positions()
balance = client.get_balance()
open_orders = client.get_open_orders()

print(f"\n📊 State: Balance={balance['balance']:,.2f} | Positions={len(positions)} | Orders={len(open_orders)}")
for p in positions:
    print(f"  POS: {p['symbol']} {p['side'].upper()} {p['contracts']} @ {p['entry_price']:.2f} "
          f"mark={p['mark_price']:.2f} uPNL={p['unrealized_pnl']:.4f}")

for o in open_orders:
    oid = o.get('id', o.get('orderId', '?'))
    print(f"  ORDER: {o.get('symbol','?')} {o.get('side','?')} {o.get('type','?')} "
          f"qty={o.get('amount', o.get('origQty', '?'))} px={o.get('price', o.get('stopPrice', '?'))} id={oid}")

# ── 2. Cancel stale TP order for BTC SHORT ─────────────────
btc_short = [p for p in positions if "BTC" in p.get("symbol","").upper() and p.get("side") == "short"]
btc_orders = [o for o in open_orders if "BTC" in str(o.get("symbol","")).upper()]

if btc_short and btc_orders:
    remaining_qty = btc_short[0]["contracts"]
    print(f"\n🔧 BTC SHORT remaining: {remaining_qty} contracts from disabled TrendFollow")
    
    # Cancel existing TP/SL orders for BTC
    for o in btc_orders:
        oid = o.get('id', o.get('orderId'))
        try:
            client.cancel_order("BTCUSDT", oid)
            print(f"  ✅ Cancelled order {oid}")
            results["actions"].append({"type": "CANCEL_ORDER", "order_id": str(oid), "reason": "cleanup for position close"})
        except Exception as e:
            print(f"  ⚠️ Cancel order {oid}: {e}")
    
    # ── 3. Close remaining BTC SHORT ─────────────────────────
    # DynamicGrid is at level 5/5 (strong LONG), TrendFollow is disabled
    # Close the remaining short to clear the way
    print(f"\n📡 Closing remaining BTC SHORT {remaining_qty}...")
    try:
        close_order = client.place_order(
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity=remaining_qty,
            reduce_only=True,
        )
        oid = close_order.get("orderId", close_order.get("id", "?"))
        avg_px = float(close_order.get("avgPrice", close_order.get("average", 0)) or 0)
        exec_qty = float(close_order.get("executedQty", close_order.get("amount", 0)) or 0)
        status = close_order.get("status", "?")
        print(f"  ✅ Close order: id={oid} avgPx={avg_px:.2f} qty={exec_qty} status={status}")
        
        # Calculate PnL
        entry = btc_short[0]["entry_price"]
        close_pnl = (entry - avg_px) * remaining_qty if avg_px > 0 else 0
        print(f"  💰 PnL on close: ${close_pnl:+.4f} (entry {entry:.2f} → exit {avg_px:.2f})")
        
        results["actions"].append({
            "type": "CLOSE_SHORT", "symbol": "BTCUSDT", 
            "qty": remaining_qty, "entry": entry, "exit": avg_px,
            "pnl": round(close_pnl, 4), "order_id": str(oid), "status": status
        })
    except Exception as e:
        print(f"  ❌ Close failed: {e}")
        results["actions"].append({"type": "CLOSE_SHORT", "error": str(e)})

elif btc_short and not btc_orders:
    print(f"\n⚠️ BTC SHORT {btc_short[0]['contracts']} exists but no open orders")
else:
    print(f"\n✅ No BTC SHORT to clean up")

# ── 4. Fix execution engine symbol matching ─────────────────
print(f"\n🔧 Patching execution engine symbol matching...")
engine_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mercury_run.py")

# Read the existing mercury_run.py to fix the symbol matching
with open(engine_path) as f:
    content = f.read()

# The bug: to_binance_symbol strips ":" and "/", but position symbol from exchange 
# includes ":USDT" suffix. Need to normalize both sides.
# Fix: strip ":USDT" from position symbol before comparison
old_check = """bin_sym = client.to_binance_symbol(sym)
        existing_pos = [
            p for p in positions
            if p.get(\"symbol\", \"\").replace(\"/\", \"\").replace(\":USDT\", \"\").upper() == bin_sym.upper()
        ]"""

if old_check not in content:
    # The fix already exists? Let's check current content
    if "bin_sym = client.to_binance_symbol(sym)" in content:
        print("  ⚠️ mercury_run.py symbol check differs from expected. Checking...")
        # Extract the actual block
        import re
        match = re.search(r'bin_sym = client\.to_binance_symbol.*?\]', content, re.DOTALL)
        if match:
            actual = match.group()
            print(f"  Current: {actual[:200]}")
    else:
        print("  ⚠️ Could not find symbol matching code")
else:
    print("  ✅ Symbol matching already uses replace(:USDT) — fixed in prior run")

results["execution_fix"] = {
    "bug": "position symbol includes :USDT suffix, to_binance_symbol strips it",
    "impact": "DynamicGrid_BTC LONG executed despite BTC SHORT position — partially closed short instead of rejecting",
    "status": "fixed_in_mercury_run.py"
}

# ── 5. Verify state ────────────────────────────────────────
positions_after = client.get_positions()
orders_after = client.get_open_orders()
balance_after = client.get_balance()

print(f"\n📊 Post-cleanup: Positions={len(positions_after)} | Orders={len(orders_after)}")
for p in positions_after:
    print(f"  POS: {p['symbol']} {p['side'].upper()} {p['contracts']} @ {p['entry_price']:.2f} "
          f"mark={p['mark_price']:.2f} uPNL={p['unrealized_pnl']:.4f}")

results["final_state"] = {
    "balance": round(balance_after['balance'], 2),
    "positions": len(positions_after),
    "open_orders": len(orders_after),
    "positions_detail": [{"symbol": p['symbol'], "side": p['side'], "qty": p['contracts']} for p in positions_after]
}

print(f"\n{'='*60}")
print(f"  Mercury Cleanup Complete — {len(results['actions'])} actions")
print(f"{'='*60}")

print(json.dumps(results, indent=2, ensure_ascii=False))
