#!/usr/bin/env python3
"""
Aether 专员任务排期系统
每个专员维护自己的任务队列，同步到仪表盘。
"""
import json, os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, ".aether", "state")
os.makedirs(STATE_DIR, exist_ok=True)

TASK_STATUS = {
    "queued":     {"icon":"⏳","label":"排队中","color":"#6b7280"},
    "in_progress":{"icon":"🔄","label":"进行中","color":"#f59e0b"},
    "done":       {"icon":"✅","label":"已完成","color":"#22c55e"},
    "blocked":    {"icon":"🚫","label":"被阻塞","color":"#ef4444"},
    "cancelled":  {"icon":"❌","label":"已取消","color":"#6b7280"},
}

def _state_path(agent):
    return os.path.join(STATE_DIR, f"{agent}.json")

def _load(agent):
    p = _state_path(agent)
    if not os.path.exists(p): return {}
    with open(p) as f: return json.load(f)

def _save(agent, data):
    with open(_state_path(agent), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _now():
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M")

# ---- public API ----

def add_task(agent, title, priority=0):
    """添加任务到队列。"""
    state = _load(agent)
    tasks = state.get("tasks", [])
    tid = max([t["id"] for t in tasks], default=0) + 1
    tasks.append({
        "id": tid, "title": title, "status": "queued",
        "priority": priority,
        "created_at": _now(), "started_at": None, "done_at": None,
        "notes": "",
    })
    state["tasks"] = tasks
    _save(agent, state)
    return tid

def start_task(agent, task_id):
    """开始执行任务 (同时把其他 in_progress 的标记为 queued)。"""
    state = _load(agent)
    for t in state.get("tasks", []):
        if t["id"] == task_id:
            t["status"] = "in_progress"
            t["started_at"] = _now()
        elif t["status"] == "in_progress":
            t["status"] = "queued"
    _save(agent, state)

def done_task(agent, task_id, notes=""):
    """标记任务完成。"""
    state = _load(agent)
    for t in state.get("tasks", []):
        if t["id"] == task_id:
            t["status"] = "done"
            t["done_at"] = _now()
            t["notes"] = notes
            break
    _save(agent, state)

def block_task(agent, task_id, reason=""):
    state = _load(agent)
    for t in state.get("tasks", []):
        if t["id"] == task_id:
            t["status"] = "blocked"
            t["notes"] = reason
            break
    _save(agent, state)

def get_tasks(agent):
    return _load(agent).get("tasks", [])

def get_current(agent):
    """获取当前进行的任务。"""
    for t in get_tasks(agent):
        if t["status"] == "in_progress":
            return t
    return None

def get_summary(agent):
    tasks = get_tasks(agent)
    return {
        "total": len(tasks),
        "queued": sum(1 for t in tasks if t["status"]=="queued"),
        "in_progress": sum(1 for t in tasks if t["status"]=="in_progress"),
        "done": sum(1 for t in tasks if t["status"]=="done"),
        "blocked": sum(1 for t in tasks if t["status"]=="blocked"),
        "current": get_current(agent),
    }

# ---- CLI ----
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: task_system.py <agent> <cmd> [args]")
        sys.exit(1)
    agent = sys.argv[1]
    cmd = sys.argv[2]
    if cmd == "add": add_task(agent, sys.argv[3])
    elif cmd == "start": start_task(agent, int(sys.argv[3]))
    elif cmd == "done": done_task(agent, int(sys.argv[3]), sys.argv[4] if len(sys.argv)>4 else "")
    elif cmd == "block": block_task(agent, int(sys.argv[3]), sys.argv[4] if len(sys.argv)>4 else "")
    elif cmd == "list":
        for t in get_tasks(agent):
            s = TASK_STATUS[t["status"]]
            print(f'  {s["icon"]} #{t["id"]} [{t["status"]}] {t["title"]} ({t.get("created_at","?")})')
    elif cmd == "current":
        t = get_current(agent)
        if t: print(f'{TASK_STATUS[t["status"]]["icon"]} #{t["id"]} {t["title"]}')
        else: print("idle")
    print("OK")
