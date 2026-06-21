"""
Aether 专员间请求系统 (Inter-Agent Request Protocol)

任何专员可向其他专员发起请求。被请求方在下次心跳时读取并处理。

用法:
  from .aether.requests import request, fulfill, get_my_requests

  # Athena 向 Oracle 请求数据
  request("oracle", {
      "type": "fetch_data",
      "symbol": "BTC/USDT",
      "timeframe": "1h",
      "days": 90,
      "reason": "1h backtest needs 300+ warmup bars"
  })

  # Oracle 处理后标记完成
  fulfill(request_id, {"status": "done", "rows": 2160})

  # 专员读取自己的待处理请求
  pending = get_my_requests("oracle")
"""
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

_REQUESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".aether", "requests")
_REQUESTS_FILE = os.path.join(_REQUESTS_DIR, "requests.json")


def _load() -> List[Dict]:
    os.makedirs(_REQUESTS_DIR, exist_ok=True)
    if not os.path.exists(_REQUESTS_FILE):
        return []
    with open(_REQUESTS_FILE) as f:
        return json.load(f)


def _save(requests: List[Dict]):
    os.makedirs(_REQUESTS_DIR, exist_ok=True)
    with open(_REQUESTS_FILE, "w") as f:
        json.dump(requests, f, indent=2, ensure_ascii=False)


def request(target: str, data: Dict) -> int:
    """向目标专员发起请求。返回请求ID。"""
    reqs = _load()
    req_id = len(reqs) + 1
    reqs.append({
        "id": req_id,
        "target": target,
        "from": data.get("_from", "unknown"),
        "type": data.get("type", "unknown"),
        "data": data,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fulfilled_at": None,
    })
    _save(reqs)
    return req_id


def fulfill(req_id: int, result: Dict):
    """标记请求为已完成。"""
    reqs = _load()
    for r in reqs:
        if r["id"] == req_id:
            r["status"] = "fulfilled"
            r["fulfilled_at"] = datetime.now(timezone.utc).isoformat()
            r["result"] = result
            break
    _save(reqs)


def get_my_requests(agent: str) -> List[Dict]:
    """获取指定专员的待处理请求。"""
    return [r for r in _load() if r["target"] == agent and r["status"] == "pending"]


def get_all_pending() -> List[Dict]:
    return [r for r in _load() if r["status"] == "pending"]


def get_summary() -> str:
    """生成请求摘要。"""
    reqs = _load()
    pending = [r for r in reqs if r["status"] == "pending"]
    if not pending:
        return "无待处理请求"
    lines = []
    for r in pending:
        lines.append(f"  #{r['id']} {r['from']}→{r['target']}: {r['type']} ({r['data'].get('reason','')[:40]})")
    return "\n".join(lines)


if __name__ == "__main__":
    # Test
    request("oracle", {
        "type": "fetch_data",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "days": 90,
        "reason": "Athena: 1h backtest needs 300+ warmup bars",
        "_from": "athena",
    })
    print("Pending:", get_my_requests("oracle"))
    print("Summary:", get_summary())
