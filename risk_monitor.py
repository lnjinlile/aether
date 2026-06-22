#!/usr/bin/env python3
"""
Aether 风控守护进程 — 永不重启。独立于引擎，持续监控。

职责:
  1. 监控强平距离 → 告警
  2. 监控日亏损 → 超过3%冻结交易
  3. 监控仓位集中度 → 告警
  4. 核对杠杆 → 异常告警
"""
import json, os, sys, time, logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [RISK] %(message)s")
logger = logging.getLogger("risk_monitor")

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, ".aether", "state", "risk_state.json")
FREEZE_FILE = os.path.join(BASE, ".aether", "state", "trade_freeze.lock")
INTERVAL = 30

DAILY_LOSS_LIMIT = 0.03  # 3%
LIQ_WARNING = 10.0  # 10% liq distance
LIQ_CRITICAL = 5.0   # 5% liq distance
MAX_POSITION_PCT = 50  # Max % of balance in one position


def get_client():
    from execution.client import BinanceFuturesClient
    from config.settings import get_config
    cfg = get_config()
    return BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)


def check():
    alerts = []
    
    try:
        client = get_client()
        bal = client.get_balance()
        balance = bal.get("balance", 0)
        positions = client.get_positions()
        upnl = bal.get("unrealized_pnl", 0)
        
        # 1. Liquidation distance check
        for p in positions:
            mark = p.get("mark_price", 0)
            liq = p.get("liquidation_price", 0)
            if mark > 0 and liq > 0:
                dist = abs(mark - liq) / mark * 100
                sym = p.get("symbol", "?")
                if dist < LIQ_CRITICAL:
                    alerts.append(f"🔴 {sym}: 强平距离{dist:.1f}%(临界)")
                elif dist < LIQ_WARNING:
                    alerts.append(f"🟡 {sym}: 强平距离{dist:.1f}%(警告)")
        
        # 2. Position concentration
        for p in positions:
            notional = abs(p.get("contracts", 0)) * p.get("mark_price", 0)
            pct = notional / balance * 100 if balance > 0 else 999
            if pct > MAX_POSITION_PCT:
                alerts.append(f"🔴 {p['symbol']}: 仓位{pct:.0f}%超限({MAX_POSITION_PCT}%)")
        
        # 3. Daily PnL check
        daily = load_daily_pnl()
        daily_pct = daily / (balance - upnl) * 100 if balance - upnl > 0 else 0
        if daily_pct < -DAILY_LOSS_LIMIT * 100:
            if not os.path.exists(FREEZE_FILE):
                with open(FREEZE_FILE, "w") as f:
                    f.write(json.dumps({"frozen_at": datetime.now().isoformat(), "reason": f"日亏损{daily_pct:.1f}%超限"}))
                alerts.append(f"🔴 交易冻结: 日亏损{daily_pct:.1f}% > {DAILY_LOSS_LIMIT*100}%")
        else:
            if os.path.exists(FREEZE_FILE):
                os.remove(FREEZE_FILE)
                logger.info("Freeze lifted: daily PnL within limits")
        
        # 4. Leverage check
        for p in positions:
            if p.get("leverage", 1) < 2 and abs(p.get("contracts", 0)) > 0.01:
                alerts.append(f"⚠️ {p['symbol']}: 杠杆{p['leverage']}x过低, 建议>=2x")
        
        # Log
        logger.info("Balance=%.2f positions=%d uPNL=%.2f risk=%s",
                   balance, len(positions), upnl, 
                   "ALERT" if any("🔴" in a for a in alerts) else "OK")
        
        if alerts:
            for a in alerts:
                logger.warning(a)
    
    except Exception as e:
        logger.error("Check error: %s", e)
    
    return alerts


def load_daily_pnl():
    conn = sqlite3.connect(os.path.join(BASE, "data", "market.db"))
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0).timestamp()
    pnl = conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM trades_log WHERE exit_time > ? AND pnl IS NOT NULL",
        (today,)
    ).fetchone()[0]
    conn.close()
    return pnl


if __name__ == "__main__":
    import sqlite3
    logger.info("Risk Monitor started — interval=%ds", INTERVAL)
    
    # Ensure fresh state
    if os.path.exists(FREEZE_FILE):
        os.remove(FREEZE_FILE)
    
    while True:
        try:
            check()
        except Exception as e:
            logger.error("Loop error: %s", e)
        time.sleep(INTERVAL)
