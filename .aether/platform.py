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
    pending = [r for r in reqs if r["target"] == agent and r["status"] == "pending"]
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
    else: print(f"Unknown: {cmd}")
