#!/usr/bin/env python3
"""
Aether 交易执行层 — 永不重启。独立进程，只做交易。

架构:
  engine.py → backtest_results.json + trade_signals.json (可随时重启)
  trade_executor.py → 读 signals → 执行交易 (永不重启)
  risk_monitor.py → 实时风控 (永不重启)

数据流:
  engine.py ──→ .aether/trade_signals.json ──→ trade_executor.py ──→ 币安测试网
                                                  │
  engine.py ──→ .aether/backtest_results.json   riskt_monitor.py
"""
import json, os, sys, time, sqlite3, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TRADE] %(message)s")
logger = logging.getLogger("trade_executor")

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, ".aether", "state")
SIGNAL_FILE = os.path.join(STATE_DIR, "trade_signals.json")
STATE_FILE = os.path.join(STATE_DIR, "executor_state.json")
DB = os.path.join(BASE, "data", "market.db")
INTERVAL = 60  # Check for signals every minute

os.makedirs(STATE_DIR, exist_ok=True)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"active_positions": [], "last_trade_ts": None, "daily_pnl": 0.0, "executed_signals": []}
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except: return {"active_positions": [], "last_trade_ts": None, "daily_pnl": 0.0, "executed_signals": []}


def save_state(state):
    state["_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2, ensure_ascii=False)


def get_client():
    from execution.client import BinanceFuturesClient
    from config.settings import get_config
    cfg = get_config()
    return BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)


def sync_positions(client, state):
    """Sync live exchange positions with state tracking."""
    try:
        live_positions = client.get_positions()
        live_symbols = {p["symbol"]: p for p in live_positions}
        
        # Detect closed positions
        tracked = {p["symbol"]: p for p in state["active_positions"]}
        for sym, tp in tracked.items():
            if sym not in live_symbols:
                # Position was closed (manually or by SL/TP)
                logger.info("Position CLOSED: %s %s @ %s", sym, tp.get("side"), tp.get("entry_price"))
                state["active_positions"] = [p for p in state["active_positions"] if p["symbol"] != sym]
        
        # Update existing positions
        for sym, lp in live_symbols.items():
            existing = tracked.get(sym)
            if existing:
                existing.update(lp)
            else:
                logger.info("New position detected: %s %s x%s @ %s", sym, lp.get("side"), lp.get("contracts"), lp.get("entry_price"))
                state["active_positions"].append(lp)
        
        save_state(state)
    except Exception as e:
        logger.error("Position sync error: %s", e)


def cancel_all_orders(client, symbol):
    """Cancel all open orders for a symbol before placing new ones."""
    try:
        orders = client.get_open_orders()
        cancelled = 0
        for o in orders:
            if o.get("symbol", "").replace("/", "") == symbol.replace("/", ""):
                client.cancel_order(o["id"], symbol)
                cancelled += 1
        if cancelled:
            logger.info("Cancelled %d open orders for %s", cancelled, symbol)
    except Exception as e:
        logger.error("Cancel orders error: %s", e)


def execute_signal(client, state, signal):
    """Execute one trading signal."""
    sym = signal.get("symbol", "BTC/USDT:USDT")
    side = signal.get("signal", "LONG")
    qty = signal.get("quantity", 0.001)
    lev = signal.get("leverage", 3)
    strategy = signal.get("strategy", "?")
    sl_pct = signal.get("sl_pct", 0.02)
    tp_pct = signal.get("tp_pct", 0.04)
    price = signal.get("price", 0)
    
    # Check if already have position
    existing = [p for p in state["active_positions"] if p["symbol"] == sym]
    if existing:
        curr_side = existing[0].get("side", "")
        if (side == "LONG" and curr_side == "long") or (side == "SHORT" and curr_side == "short"):
            logger.info("Signal %s %s — already holding, skip", sym, side)
            return None
        # Close existing opposite position
        cancel_all_orders(client, sym)
        try:
            client.close_position(sym)
            logger.info("Closed existing %s position for new %s signal", curr_side, side)
            time.sleep(1)
        except Exception as e:
            logger.error("Close position error: %s", e)
            return None
    
    # Cancel any existing orders
    cancel_all_orders(client, sym)
    
    # Set leverage
    try:
        client.set_leverage(sym, lev)
    except Exception as e:
        logger.warning("Set leverage: %s", e)
    
    # Place order
    try:
        result = client.place_order(sym, side, qty, order_type="MARKET")
        order_id = result.get("order", {}).get("id", result.get("id", "?")) if result else "?"
        fill_price = result.get("price", price) if result else price
        
        # Calculate SL/TP prices
        sl_price = fill_price * (1 - sl_pct) if side == "LONG" else fill_price * (1 + sl_pct)
        tp_price = fill_price * (1 + tp_pct) if side == "LONG" else fill_price * (1 - tp_pct)
        
        # Place SL/TP orders
        try:
            sl_side = "SELL" if side == "LONG" else "BUY"
            tp_side = "SELL" if side == "LONG" else "BUY"
            client.place_order(sym, sl_side, qty, order_type="STOP_MARKET", stop_price=sl_price, reduce_only=True)
            client.place_order(sym, tp_side, qty, order_type="TAKE_PROFIT_MARKET", stop_price=tp_price, reduce_only=True)
        except Exception as e:
            logger.warning("SL/TP placement: %s", e)
        
        # Log trade
        card = {
            "策略": strategy,
            "标的": sym,
            "方向": side,
            "数量": qty,
            "入场价": fill_price,
            "止损": round(sl_price, 1),
            "止盈": round(tp_price, 1),
            "杠杆": f"{lev}x",
            "订单ID": order_id,
            "时间": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
        
        logger.info("TRADED: %s %s x%s @ %s SL=%s TP=%s ID=%s", 
                    sym, side, qty, fill_price, card["止损"], card["止盈"], order_id)
        
        # Record in DB
        try:
            conn = sqlite3.connect(DB)
            conn.execute("""INSERT INTO trades_log(symbol,side,entry_time,entry_price,quantity,strategy_name,reason,status)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (sym.replace("/", "").replace(":USDT", ""), side, time.time(), fill_price, qty, strategy,
                        f"Signal: {signal.get('reason','')}", "OPEN"))
            conn.commit(); conn.close()
        except: pass
        
        # Update state
        state["last_trade_ts"] = time.time()
        state["executed_signals"].append(signal.get("id", time.time()))
        save_state(state)
        
        return card
        
    except Exception as e:
        logger.error("Order error: %s", e)
        return None


def process_signals(client, state):
    """Read signals from engine and execute."""
    if not os.path.exists(SIGNAL_FILE):
        return
    
    try:
        with open(SIGNAL_FILE) as f: data = json.load(f)
    except: return
    
    signals = data.get("signals", {})
    executed = set(state.get("executed_signals", []))
    
    for sig_id, signal in signals.items():
        if sig_id in executed:
            continue
        
        logger.info("New signal: %s %s @ %s", signal.get("symbol"), signal.get("signal"), signal.get("price"))
        result = execute_signal(client, state, signal)
        
        if result:
            # Post to feed
            import subprocess
            subprocess.run(["python3", os.path.join(BASE, ".aether", "feed.py"), "post", "mercury", "trade",
                          f"开仓 {signal['symbol']} {signal['signal']} x{signal.get('quantity',0.001)} @ {signal.get('price',0)}"],
                         capture_output=True)
    
    sync_positions(client, state)


def main():
    logger.info("Trade Executor started — interval=%ds", INTERVAL)
    logger.info("Signal file: %s", SIGNAL_FILE)
    
    state = load_state()
    client = get_client()
    
    while True:
        try:
            sync_positions(client, state)
            process_signals(client, state)
        except Exception as e:
            logger.error("Loop error: %s", e)
        
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
