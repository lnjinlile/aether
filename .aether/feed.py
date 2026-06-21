#!/usr/bin/env python3
"""
Aether 统一信息流 — 七个成员共享的唯一真相源

每行一条 JSON，append-only。
所有专员(含 Hermes)启动时读最新30条，结束时追加一条。
"""
import json, os, sys
from datetime import datetime, timezone

FEED = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "feed.jsonl")
os.makedirs(os.path.dirname(FEED), exist_ok=True)


def post(agent: str, msg_type: str, msg: str, status: str = "ok", details: dict = None):
    """追加一条消息到信息流。"""
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%m-%d %H:%M"),
        "agent": agent,
        "type": msg_type,
        "msg": msg,
        "status": status,
    }
    if details:
        entry["details"] = details
    with open(FEED, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(n: int = 30) -> list:
    """读取最近 N 条消息。"""
    if not os.path.exists(FEED):
        return []
    with open(FEED) as f:
        lines = f.readlines()
    entries = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except:
            continue
    return entries


def summary() -> str:
    """生成人类可读的最近摘要。"""
    entries = read(20)
    if not entries:
        return "📭 信息流为空"
    lines = []
    for e in entries:
        icon = {"report": "📋", "trade": "💹", "alert": "🚨", "audit": "👁️",
                "task": "📌", "decision": "⚡", "heartbeat": "💓"}.get(e.get("type", ""), "📎")
        lines.append(f"{icon} [{e['agent']}] {e['ts']} {e.get('status','')} {e['msg'][:100]}")
    return "\n".join(lines)


def since(agent: str = None, last_n: int = 10) -> list:
    """获取某专员上次发消息之后的其他专员消息。用于'别人发了什么'。"""
    entries = read(50)
    # 找到该专员的最后一条
    my_last = 0
    for i, e in enumerate(entries):
        if e.get("agent") == agent:
            my_last = i
    # 返回之后的所有消息
    return entries[my_last + 1:]


# ---- CLI ----
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: feed.py <cmd> [args]")
        print("  post <agent> <type> <msg> [status] [details_json]")
        print("  read [N]")
        print("  summary")
        print("  since <agent>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "post":
        agent = sys.argv[2]
        msg_type = sys.argv[3]
        msg = sys.argv[4]
        status = sys.argv[5] if len(sys.argv) > 5 else "ok"
        details = json.loads(sys.argv[6]) if len(sys.argv) > 6 else None
        post(agent, msg_type, msg, status, details)
        print("OK")
    elif cmd == "read":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        for e in read(n):
            print(json.dumps(e, ensure_ascii=False))
    elif cmd == "summary":
        print(summary())
    elif cmd == "since":
        agent = sys.argv[2]
        others = since(agent)
        if not others:
            print("无新消息")
        else:
            for e in others:
                print(f"[{e['agent']}] {e['ts']} {e['msg'][:100]}")
