#!/usr/bin/env python3
"""
Aether 请求系统 v2 — 完整生命周期追踪

状态流转: pending → acknowledged → processing → fulfilled / rejected
每个状态变更自动记录时间戳和处理人。
"""
import json, os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
REQ_FILE = os.path.join(BASE, "requests", "requests.json")
os.makedirs(os.path.dirname(REQ_FILE), exist_ok=True)


def _load():
    if not os.path.exists(REQ_FILE): return []
    with open(REQ_FILE) as f: return json.load(f)

def _save(data):
    with open(REQ_FILE, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)

def _now():
    return datetime.now(timezone.utc).strftime("%m-%d %H:%M")


# ---- public API ----
def request(target, from_agent, req_type, data):
    """发起请求。data 为 dict。"""
    reqs = _load()
    rid = len(reqs) + 1
    reqs.append({
        "id": rid,
        "target": target,
        "from": from_agent,
        "type": req_type,
        "data": data,
        "status": "pending",
        "timeline": [{"time": _now(), "status": "pending", "by": from_agent, "msg": "请求已发起"}],
        "result": None,
    })
    _save(reqs)
    return rid

def acknowledge(req_id, agent):
    """目标专员确认收到请求。"""
    reqs = _load()
    for r in reqs:
        if r["id"] == req_id:
            r["status"] = "acknowledged"
            r["timeline"].append({"time": _now(), "status": "acknowledged", "by": agent, "msg": f"{agent} 已接收请求"})
            break
    _save(reqs)

def start_processing(req_id, agent, msg=""):
    """标记开始处理。"""
    reqs = _load()
    for r in reqs:
        if r["id"] == req_id:
            r["status"] = "processing"
            r["timeline"].append({"time": _now(), "status": "processing", "by": agent, "msg": msg or f"{agent} 开始处理"})
            break
    _save(reqs)

def fulfill(req_id, agent, result):
    """标记完成并记录结果。"""
    reqs = _load()
    for r in reqs:
        if r["id"] == req_id:
            r["status"] = "fulfilled"
            r["result"] = result
            r["timeline"].append({"time": _now(), "status": "fulfilled", "by": agent, "msg": f"{agent} 已完成: {json.dumps(result, ensure_ascii=False)}"})
            break
    _save(reqs)

def reject(req_id, agent, reason):
    """拒绝请求。"""
    reqs = _load()
    for r in reqs:
        if r["id"] == req_id:
            r["status"] = "rejected"
            r["timeline"].append({"time": _now(), "status": "rejected", "by": agent, "msg": reason})
            break
    _save(reqs)

def get_pending(target_agent):
    """获取某专员的待处理请求。"""
    return [r for r in _load() if r["target"] == target_agent and r["status"] in ("pending", "acknowledged")]

def get_all():
    return _load()

def get_stats():
    reqs = _load()
    return {
        "total": len(reqs),
        "pending": sum(1 for r in reqs if r["status"] == "pending"),
        "acknowledged": sum(1 for r in reqs if r["status"] == "acknowledged"),
        "processing": sum(1 for r in reqs if r["status"] == "processing"),
        "fulfilled": sum(1 for r in reqs if r["status"] == "fulfilled"),
        "rejected": sum(1 for r in reqs if r["status"] == "rejected"),
    }


if __name__ == "__main__":
    # 把旧请求升级到 v2 格式
    reqs = _load()
    for r in reqs:
        if "timeline" not in r:
            r["timeline"] = [{"time": r.get("created_at","?")[:16], "status": r.get("status","pending"), "by": r.get("from","?"), "msg": "请求已发起"}]
            if r.get("fulfilled_at"):
                r["timeline"].append({"time": r["fulfilled_at"][:16], "status": "fulfilled", "by": r.get("target","?"), "msg": "已完成"})
                r["status"] = "fulfilled"
        if "result" not in r:
            r["result"] = r.get("result")
    _save(reqs)
    print("Upgraded. Stats:", get_stats())
