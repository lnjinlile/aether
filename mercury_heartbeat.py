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
    sig_type_display = sig.get('type', sig.get('signal', 'UNKNOWN'))
    print(f"  {name}: {sig_type_display} {sig['symbol']} @ {sig['price']:.2f} "
          f"SL={sig.get('stop_loss','N/A')} TP={sig.get('take_profit','N/A')} "
          f"conf={sig.get('confidence',0):.1%}")

# ── 3. Place SL/TP for all open positions ─────────────────
def place_sltp_for_position(pos, client, results):
    """Place SL/TP orders for a single position. Handles both LONG and SHORT.
    Uses client.place_sl_order() which routes through ccxt algo endpoint
    (required for testnet; direct REST STOP_MARKET fails with -4120)."""
    symbol_raw = pos.get("symbol", "")
    entry = pos["entry_price"]
    contracts = pos["contracts"]
    side = pos.get("side", "").lower()
    
    # Determine exchange symbol and order sides
    if "BTC" in symbol_raw.upper():
        bin_sym = "BTCUSDT"
    elif "ETH" in symbol_raw.upper():
        bin_sym = "ETHUSDT"
    else:
        bin_sym = symbol_raw
    
    # Check existing orders
    existing = [o for o in open_orders if bin_sym.upper() in str(o.get("symbol","")).upper()]
    if existing:
        print(f"\n🛡️ {bin_sym} {side.upper()} already has {len(existing)} open order(s), skipping SL/TP")
        results["actions"].append({"type": "SKIP_SLTP", "symbol": bin_sym, "reason": "existing orders present"})
        return
    
    # Calculate SL/TP based on position direction
    sl_pct = 0.02   # 2% stop loss
    tp_pct = 0.03   # 3% take profit (for LONG) / 0.04 for mean-reversion
    
    if side == "long":
        sl_price = round(entry * (1 - sl_pct), 1)
        tp_price = round(entry * (1 + tp_pct), 1)
        close_side = "sell"
    else:  # short
        sl_price = round(entry * (1 + sl_pct), 1)
        tp_price = round(entry * (1 - tp_pct), 1)
        close_side = "buy"
    
    print(f"\n🛡️ Placing SL/TP for {bin_sym} {side.upper()}:")
    print(f"  Entry: {entry:.2f} | SL: {sl_price:.2f} | TP: {tp_price:.2f}")
    
    # Place TP (limit order, reduce-only)
    try:
        tp_order = client.place_order(
            symbol=bin_sym,
            side=close_side,
            order_type="limit",
            quantity=contracts,
            price=tp_price,
            reduce_only=True,
        )
        tp_id = tp_order.get("orderId", tp_order.get("id", "?"))
        print(f"  ✅ TP placed: orderId={tp_id} @ {tp_price}")
        results["actions"].append({
            "type": "PLACE_TP", "symbol": bin_sym, "price": tp_price,
            "order_id": str(tp_id), "status": "OK"
        })
    except Exception as e:
        print(f"  ❌ TP failed: {e}")
        results["actions"].append({"type": "PLACE_TP", "symbol": bin_sym, "error": str(e)})
    
    # Place SL via place_sl_order (testnet-safe, uses ccxt algo endpoint)
    try:
        sl_order = client.place_sl_order(
            symbol=bin_sym,
            side=close_side.upper(),
            quantity=contracts,
            stop_price=sl_price,
        )
        sl_id = sl_order.get("id", sl_order.get("orderId", "?"))
        print(f"  ✅ SL placed: orderId={sl_id} trigger={sl_price}")
        results["actions"].append({
            "type": "PLACE_SL", "symbol": bin_sym, "price": sl_price,
            "order_id": str(sl_id), "status": "OK"
        })
    except Exception as e:
        print(f"  ❌ SL failed: {e}")
        results["actions"].append({"type": "PLACE_SL", "symbol": bin_sym, "error": str(e)})

# Apply to all non-zero positions
for pos in positions:
    contracts = float(pos.get("contracts", 0))
    if abs(contracts) < 1e-8:
        continue
    place_sltp_for_position(pos, client, results)

# ── 4. Signal review ──────────────────────────────────────
print(f"\n📋 Signal review:")

for name, sig in signals.items():
    sym = sig["symbol"]
    sig_type = sig.get("type", sig.get("signal", "UNKNOWN"))
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
    
    leverage = int(sig.get("leverage", 3))
    signal_dict = {
        "type": sig_type,
        "symbol": sym,
        "quantity": 0.001 if "BTC" in sym.upper() else 0.01,
        "price": sig.get("price"),
        "leverage": leverage,
        "stop_loss": sig.get("stop_loss"),
        "take_profit": sig.get("take_profit"),
    }
    
    # Set leverage BEFORE opening position (Binance: leverage only applies to new positions)
    try:
        client.set_leverage(bin_sym, leverage)
        print(f"    Leverage set to {leverage}x for {bin_sym}")
    except Exception as e:
        print(f"    [WARN] set_leverage failed: {e}")
    
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
