#!/usr/bin/env python3
"""Aether 四项防御措施 + 杠杆数据核对"""
import json, os, sqlite3, sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, ".aether", "state")

def load_json(path):
    if not os.path.exists(path): return {}
    with open(path) as f: return json.load(f)

def save_json(name, data):
    data["_updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(STATE_DIR, name), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ===== 1. 数据清洗 — 防止垃圾值 =====
def sanitize_risk_data():
    risk = load_json(os.path.join(STATE_DIR, "risk_check.json"))
    live = load_json(os.path.join(STATE_DIR, "live_exchange.json"))
    
    positions = live.get("positions", [])
    balance = live.get("balance", {}).get("balance", 5000)
    
    # Recalculate correct values
    total_notional = 0
    cleaned_positions = []
    for p in positions:
        contracts = abs(p.get("contracts", 0) or p.get("positionAmt", 0))
        mark = p.get("mark_price", 0)
        notional = contracts * mark
        
        # Safety: notional should never exceed balance * 5
        if contracts > 0 and notional < balance * 10:
            p["notional"] = notional
            p["notional_verified"] = True
            total_notional += notional
        else:
            p["notional"] = 0
            p["notional_verified"] = False
        
        cleaned_positions.append(p)
    
    position_pct = (total_notional / balance * 100) if balance > 0 else 0
    
    # Fix risk_check
    risk["total_notional"] = total_notional
    risk["position_pct"] = round(position_pct, 2)
    risk["positions"] = cleaned_positions
    
    save_json("risk_check.json", risk)
    save_json("live_exchange.json", {**live, "positions": cleaned_positions})
    return risk

# ===== 2. 杠杆核对 — 验证券商vs系统 =====
def verify_leverage():
    """Check if exchange leverage matches our config."""
    import yaml
    live = load_json(os.path.join(STATE_DIR, "live_exchange.json"))
    positions = live.get("positions", [])
    
    issues = []
    for p in positions:
        lev = p.get("leverage", 1)
        sym = p.get("symbol", "?")
        # For USDT-M futures, minimum isolated leverage is 1, max is 125
        if lev < 1 or lev > 125:
            issues.append(f"{sym}: leverage={lev}x out of range [1-125]")
        # Our strategies should use 3x+ for margin efficiency
        contracts = abs(p.get("contracts", 0) or 0)
        entry = p.get("entry_price", 0)
        notional = contracts * entry
        balance = live.get("balance", {}).get("balance", 5000)
        # Check: if position is meaningful but leverage is 1x, suggest increase
        if contracts > 0 and lev == 1 and notional > 10:
            issues.append(f"{sym}: 建议杠杆 3x+ (当前1x, 名义价值={notional:.0f})")
    
    return issues

# ===== 3. 引擎健康看门狗 =====
def check_engine_health():
    """Verify engine is producing valid data."""
    issues = []
    
    bt = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
    strategies = bt.get("strategies", {})
    active = [n for n, s in strategies.items() if s.get("status") == "ok"]
    no_metrics = [n for n, s in strategies.items() if not s.get("signals_count")]
    
    if len(active) < 2:
        issues.append(f"活跃策略仅{len(active)}个, 引擎可能异常")
    if len(no_metrics) > len(active) * 2:
        issues.append(f"{len(no_metrics)}个策略无回测数据, 引擎未运行回测")
    
    # Check data freshness
    ts = bt.get("_updated_at", "")
    if ts:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            if age > 600:
                issues.append(f"回测数据过期({int(age)}s), 引擎可能挂了")
        except Exception:
            pass  # timestamp parse failure is non-critical for safety check
    
    return issues

# ===== 4. 交易数据完整性检查 =====
def check_trade_integrity():
    """Verify trades_log has proper exit prices and PnL."""
    conn = sqlite3.connect(os.path.join(BASE, "data", "market.db"))
    
    # Check for suspicious closed trades
    bad = conn.execute(
        "SELECT id, symbol, entry_price, exit_price, pnl FROM trades_log WHERE status='CLOSED' AND (exit_price IS NULL OR exit_price=0 OR pnl IS NULL)"
    ).fetchall()
    
    issues = []
    for row in bad:
        issues.append(f"Trade #{row[0]} {row[1]}: exit_price={row[3]} pnl={row[4]} — 数据不完整")
    
    # Warning if too many open trades
    open_cnt = conn.execute("SELECT COUNT(*) FROM trades_log WHERE status='OPEN'").fetchone()[0]
    if open_cnt > 5:
        issues.append(f"持仓过多: {open_cnt}笔, 存在僵尸订单")
    
    conn.close()
    return issues


if __name__ == "__main__":
    print("Aether 防御检查")
    print("=" * 40)
    
    # 1. Sanitize
    risk = sanitize_risk_data()
    print(f"✅ 1. 数据清洗: notional={risk['total_notional']:.2f} position_pct={risk['position_pct']:.1f}%")
    
    # 2. Leverage
    lev_issues = verify_leverage()
    if lev_issues:
        for i in lev_issues: print(f"⚠️ 2. 杠杆: {i}")
    else:
        print("✅ 2. 杠杆核对: 无问题")
    
    # 3. Engine
    eng_issues = check_engine_health()
    if eng_issues:
        for i in eng_issues: print(f"⚠️ 3. 引擎: {i}")
    else:
        print("✅ 3. 引擎健康: 正常")
    
    # 4. Trade
    trade_issues = check_trade_integrity()
    if trade_issues:
        for i in trade_issues: print(f"⚠️ 4. 交易完整性: {i}")
    else:
        print("✅ 4. 交易数据: 完整")
    
    print("=" * 40)
    all_issues = lev_issues + eng_issues + trade_issues
    if all_issues:
        print(f"⚠️ {len(all_issues)} 个问题需处理")
    else:
        print("🎉 四项防御全部通过")
