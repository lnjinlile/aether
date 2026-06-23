#!/usr/bin/env python3
"""
Aether 共享平台 — 专员信息交换枢纽

每个专员启动时调用:
  python .aether/platform.py check-requests <agent_name>
  python .aether/platform.py post-bulletin "<message>"
  python .aether/platform.py write-state <agent_name> '<json>'
"""
import sys, json, os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
REQ_FILE = os.path.join(BASE, "requests", "requests.json")
BULLETIN = os.path.join(BASE, "bulletin.md")
STATE_DIR = os.path.join(BASE, "state")


def load_json(path):
    if not os.path.exists(path): return []
    with open(path) as f: return json.load(f)

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)


def cmd_check_requests(agent):
    """检查并输出待处理请求。格式: ID|FROM|TYPE|REASON"""
    reqs = load_json(REQ_FILE)
    pending = [r for r in reqs if r.get("target", r.get("to", "")) == agent and r.get("status") == "pending"]
    if not pending:
        print("NO_PENDING_REQUESTS")
        return
    for r in pending:
        reason = r["data"].get("reason", "")[:80]
        from_agent = r.get("from", "unknown")
        req_type = r.get("type", "unknown")
        print(f"REQUEST|{r['id']}|{from_agent}|{req_type}|{reason}")

def cmd_fulfill(req_id, result_json):
    reqs = load_json(REQ_FILE)
    for r in reqs:
        if r["id"] == int(req_id):
            r["status"] = "fulfilled"
            r["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
            r["result"] = json.loads(result_json) if result_json else {}
            break
    save_json(REQ_FILE, reqs)
    print(f"FULFILLED:{req_id}")

def cmd_request(target, from_agent, req_type, data_json):
    reqs = load_json(REQ_FILE)
    rid = len(reqs) + 1
    reqs.append({
        "id": rid, "target": target, "from": from_agent,
        "type": req_type, "data": json.loads(data_json),
        "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
        "fulfilled_at": None,
    })
    save_json(REQ_FILE, reqs)
    print(f"REQUESTED:{rid}")

def cmd_post_bulletin(msg):
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
    with open(BULLETIN, "a") as f:
        f.write(f"\n---\n### {ts} — {msg}\n")
    print("POSTED")

def cmd_write_state(agent, state_json):
    os.makedirs(STATE_DIR, exist_ok=True)
    current = {}
    path = os.path.join(STATE_DIR, f"{agent}.json")
    if os.path.exists(path):
        with open(path) as f: current = json.load(f)
    current.update(json.loads(state_json))
    current["_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(path, current)
    print(f"STATE_WRITTEN:{agent}")

def cmd_read_state(agent):
    path = os.path.join(STATE_DIR, f"{agent}.json")
    if not os.path.exists(path):
        print("{}")
        return
    with open(path) as f: print(f.read())

def cmd_read_bulletin(lines):
    if not os.path.exists(BULLETIN):
        print("(empty)")
        return
    with open(BULLETIN) as f:
        all_lines = f.readlines()
    print("".join(all_lines[-int(lines):]))


def cmd_check_oracle_config():
    """检查 oracle.json 与 strategies.yaml 策略一致性。PAPER策略由pipeline.py排除属正常。「Unknown」修复 — PERF-020"""
    import yaml
    oracle_path = os.path.join(BASE, "oracle.json")
    yaml_path = os.path.join(BASE, "..", "config", "strategies.yaml")
    try:
        oracle = load_json(oracle_path)
        o_enabled = set(oracle.get("strategies_enabled", []))
    except Exception as e:
        print(f"ORACLE_JSON_ERROR: {e}")
        return
    try:
        with open(yaml_path) as f:
            yaml_cfg = yaml.safe_load(f)
        y_enabled = {s["name"] for s in yaml_cfg.get("strategies", []) if s.get("enabled", False)}
    except Exception as e:
        print(f"YAML_ERROR: {e}")
        return
    # Cross-check athena.json/backtest_results.json for PAPER strategies excluded by pipeline.py
    paper_excluded = set()
    try:
        athena_path = os.path.join(STATE_DIR, "athena.json")
        bt_path = os.path.join(STATE_DIR, "backtest_results.json")
        for _path in [athena_path, bt_path]:
            try:
                with open(_path) as _f:
                    _data = json.load(_f)
                _strats = _data.get("strategies", {})
                for _name in y_enabled:
                    _s = _strats.get(_name, {})
                    _v = _s.get("verdict", "")
                    if _v in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED"):
                        paper_excluded.add(_name)
            except Exception:
                pass
    except Exception:
        pass
    # Effective yaml enabled = y_enabled minus paper-excluded
    y_effective = y_enabled - paper_excluded
    if o_enabled == y_effective:
        if paper_excluded:
            print(f"CONFIG_CONSISTENT: {sorted(o_enabled)} (PAPER excluded: {sorted(paper_excluded)})")
        else:
            print(f"CONFIG_CONSISTENT: {sorted(o_enabled)}")
        return
    only_o = o_enabled - y_effective
    only_y = y_effective - o_enabled
    if only_o:
        print(f"ORACLE_ONLY: {sorted(only_o)}")
    if only_y:
        print(f"YAML_ONLY: {sorted(only_y)}")
    if paper_excluded:
        print(f"PAPER_EXCLUDED: {sorted(paper_excluded)}")
    print(f"CONFIG_MISMATCH: oracle={sorted(o_enabled)} yaml_effective={sorted(y_effective)}")


def cmd_check_positions():
    """检查 mercury.json 活跃仓位摘要。快速巡检用 — PERF-020"""
    mercury_path = os.path.join(STATE_DIR, "mercury.json")
    try:
        mercury = load_json(mercury_path)
    except Exception as e:
        print(f"MERCURY_STATE_ERROR: {e}")
        return
    positions = mercury.get("positions", 0)
    active = mercury.get("active_strategies", {})
    orders = mercury.get("orders", [])
    if isinstance(positions, int):
        pos_count = positions
    elif isinstance(positions, dict):
        pos_count = len(positions)
    elif isinstance(positions, list):
        pos_count = len(positions)
    else:
        pos_count = 0
    if pos_count == 0 and (not orders or (isinstance(orders, list) and len(orders) == 0)):
        print("NO_POSITIONS: 无持仓 无挂单")
    if isinstance(positions, dict):
        for sym, pos in positions.items():
            print(f"POSITION|{sym}|{pos.get('side','?')}|{pos.get('quantity',0)}|entry={pos.get('entry_price',0)}")
    elif isinstance(positions, list):
        for pos in positions:
            print(f"POSITION|{pos.get('symbol','?')}|{pos.get('side','?')}|{pos.get('quantity',0)}")
    for strat, state in active.items():
        sig = state.get("signal", "NONE") if isinstance(state, dict) else "?"
        status = state.get("status", "?") if isinstance(state, dict) else str(state)
        print(f"STRATEGY|{strat}|{status}|signal={sig}")
    if isinstance(orders, list) and len(orders) > 0:
        print(f"ORDERS_PENDING: {len(orders)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: platform.py <cmd> [args...]")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "check-requests": cmd_check_requests(sys.argv[2])
    elif cmd == "fulfill": cmd_fulfill(sys.argv[2], sys.argv[3] if len(sys.argv)>3 else "")
    elif cmd == "request": cmd_request(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "post-bulletin": cmd_post_bulletin(sys.argv[2])
    elif cmd == "write-state": cmd_write_state(sys.argv[2], sys.argv[3])
    elif cmd == "read-state": cmd_read_state(sys.argv[2])
    elif cmd == "read-bulletin": cmd_read_bulletin(sys.argv[2])
    elif cmd == "check-oracle-config": cmd_check_oracle_config()
    elif cmd == "check-positions": cmd_check_positions()
    else: print(f"Unknown: {cmd}")
