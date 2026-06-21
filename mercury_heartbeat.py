#!/usr/bin/env python3
"""Mercury heartbeat: place SL/TP on BTC SHORT, review signals, execute ETH LONG."""

import sys, os, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from config.settings import get_config
from execution.client import BinanceFuturesClient
from execution.engine import OrderExecutionEngine

cfg = get_config()
client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)
engine = OrderExecutionEngine(client, max_retries=3, retry_delay=1.5)

results = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "actions": [],
    "signals_reviewed": [],
}

# ── 1. Get current positions ────────────────────────────────
print("📊 Fetching positions...")
positions = client.get_positions()
balance = client.get_balance()
open_orders = client.get_open_orders()

print(f"  Balance: {balance['balance']:,.2f} | Available: {balance['available']:,.2f} | uPNL: {balance['unrealized_pnl']:,.4f}")
print(f"  Positions: {len(positions)} | Open orders: {len(open_orders)}")

for p in positions:
    print(f"  POS: {p['symbol']} {p['side'].upper()} {p['contracts']} @ {p['entry_price']:.2f} "
          f"mark={p['mark_price']:.2f} uPNL={p['unrealized_pnl']:.4f} lev={p['leverage']}x "
          f"liq={p['liquidation_price']:.2f}")

# ── 2. Load engine signals ─────────────────────────────────
sig_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state", "signals.json")
signals_data = {}
if os.path.exists(sig_path):
    with open(sig_path) as f:
        signals_data = json.load(f)

signals = signals_data.get("signals", {})
print(f"\n🎯 Engine signals: {len(signals)} active")
for name, sig in signals.items():
    print(f"  {name}: {sig['signal']} {sig['symbol']} @ {sig['price']:.2f} "
          f"SL={sig.get('stop_loss','N/A')} TP={sig.get('take_profit','N/A')} "
          f"conf={sig.get('confidence',0):.1%}")

# ── 3. Place SL/TP for BTC SHORT ──────────────────────────
btc_short = [p for p in positions if "BTC" in p.get("symbol","").upper() and p.get("side") == "short"]
if btc_short:
    pos = btc_short[0]
    entry = pos["entry_price"]
    contracts = pos["contracts"]
    
    # Check if SL/TP orders already exist for this symbol
    btc_bin = client.to_binance_symbol("BTCUSDT")
    existing_btc_orders = [o for o in open_orders if btc_bin in str(o.get("symbol","")).upper()]
    
    if not existing_btc_orders:
        # For SHORT: SL above entry (price rises), TP below entry (price falls)
        sl_price = round(entry * 1.015, 1)   # 1.5% above entry
        tp_price = round(entry * 0.97, 1)     # 3% below entry
        
        print(f"\n🛡️ Placing SL/TP for BTC SHORT:")
        print(f"  Entry: {entry:.2f} | SL: {sl_price:.2f} (+1.5%) | TP: {tp_price:.2f} (-3.0%)")
        
        # Place TP (limit order, reduce-only)
        try:
            tp_order = client.place_order(
                symbol="BTCUSDT",
                side="buy",          # buy to close short
                order_type="limit",
                quantity=contracts,
                price=tp_price,
                reduce_only=True,
            )
            tp_id = tp_order.get("orderId", tp_order.get("id", "?"))
            print(f"  ✅ TP placed: orderId={tp_id}")
            results["actions"].append({
                "type": "PLACE_TP", "symbol": "BTCUSDT", "price": tp_price, 
                "order_id": str(tp_id), "status": "OK"
            })
        except Exception as e:
            print(f"  ❌ TP failed: {e}")
            results["actions"].append({"type": "PLACE_TP", "symbol": "BTCUSDT", "error": str(e)})
        
        # Place SL (stop-market order, reduce-only)
        try:
            sl_order = client._rest_post("/fapi/v1/order", {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "STOP_MARKET",
                "quantity": contracts,
                "stopPrice": sl_price,
                "reduceOnly": "true",
                "workingType": "MARK_PRICE",
            })
            sl_id = sl_order.get("orderId", "?")
            print(f"  ✅ SL placed: orderId={sl_id}")
            results["actions"].append({
                "type": "PLACE_SL", "symbol": "BTCUSDT", "price": sl_price,
                "order_id": str(sl_id), "status": "OK"
            })
        except Exception as e:
            print(f"  ❌ SL failed: {e}")
            results["actions"].append({"type": "PLACE_SL", "symbol": "BTCUSDT", "error": str(e)})
    else:
        print(f"\n🛡️ BTC SHORT already has {len(existing_btc_orders)} open order(s), skipping SL/TP")
        results["actions"].append({"type": "SKIP_SLTP", "reason": "existing orders present"})
else:
    print("\n⚠️ No BTC SHORT position found")

# ── 4. Signal review ──────────────────────────────────────
print(f"\n📋 Signal review:")

for name, sig in signals.items():
    sym = sig["symbol"]
    sig_type = sig["signal"]
    bin_sym = client.to_binance_symbol(sym)
    
    # Check for existing position in same symbol
    existing = [p for p in positions 
                if bin_sym in p.get("symbol","").upper().replace(":USDT","")]
    
    if existing:
        pos_side = existing[0].get("side", "")
        conflict = (sig_type == "LONG" and pos_side == "short") or \
                   (sig_type == "SHORT" and pos_side == "long")
        if conflict:
            reason = f"REJECTED: 已有反向持仓 {existing[0]['symbol']} {pos_side.upper()} {existing[0]['contracts']} @ {existing[0]['entry_price']:.2f}"
            print(f"  🛑 {name} {sig_type} {sym}: {reason}")
            results["signals_reviewed"].append({
                "strategy": name, "symbol": sym, "signal": sig_type,
                "verdict": "REJECTED", "reason": reason
            })
            continue
        else:
            reason = f"SKIPPED: 已有同向持仓"
            print(f"  ⏭️ {name} {sig_type} {sym}: {reason}")
            results["signals_reviewed"].append({
                "strategy": name, "symbol": sym, "signal": sig_type,
                "verdict": "SKIPPED", "reason": reason
            })
            continue
    
    # No existing position — execute!
    print(f"  📡 {name} {sig_type} {sym} → executing...")
    
    signal_dict = {
        "type": sig_type,
        "symbol": sym,
        "quantity": 0.001 if "BTC" in sym.upper() else 0.01,
        "price": sig.get("price"),
        "leverage": 3,
        "stop_loss": sig.get("stop_loss"),
        "take_profit": sig.get("take_profit"),
    }
    
    try:
        result = engine.execute_signal(signal_dict)
        order = result.get("order", {})
        oid = order.get("id", order.get("orderId", "N/A"))
        avg_px = float(order.get("average", order.get("price", 0)) or 0)
        
        if result.get("success"):
            print(f"    ✅ FILLED: orderId={oid} price={avg_px:.2f}")
            results["signals_reviewed"].append({
                "strategy": name, "symbol": sym, "signal": sig_type,
                "verdict": "EXECUTED", "order_id": str(oid), "price": avg_px
            })
        else:
            err = result.get("error", "unknown")
            print(f"    ❌ FAILED: {err}")
            results["signals_reviewed"].append({
                "strategy": name, "symbol": sym, "signal": sig_type,
                "verdict": "FAILED", "reason": err
            })
    except Exception as e:
        print(f"    ❌ ERROR: {e}")
        results["signals_reviewed"].append({
            "strategy": name, "symbol": sym, "signal": sig_type,
            "verdict": "ERROR", "reason": str(e)
        })

# ── 5. Summary ────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"  Mercury Heartbeat Complete")
print(f"  Actions: {len(results['actions'])} | Signals reviewed: {len(results['signals_reviewed'])}")
print(f"{'='*60}")

# Write results
state_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "mercury_heartbeat.json")
with open(state_path, "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print(json.dumps(results, indent=2, ensure_ascii=False))
