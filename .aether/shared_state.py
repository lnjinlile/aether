"""
Aether 共享状态看板 — 专员之间信息共享基础设施

每个专员通过此模块读取其他专员的最新输出，
并将自己的工作报告写入共享看板。
"""
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

_STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".aether", "state")


def _state_file(agent: str) -> str:
    return os.path.join(_STATE_DIR, f"{agent}.json")


def read_state(agent: str) -> Optional[Dict]:
    """读取指定专员的最新状态。"""
    path = _state_file(agent)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def write_state(agent: str, data: Dict):
    """写入专员状态 (合并更新)。"""
    current = read_state(agent) or {}
    current.update(data)
    current["_updated_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(_state_file(agent), "w") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)


def read_all_states() -> Dict[str, Dict]:
    """读取所有专员的最新状态。"""
    result = {}
    for agent in ["oracle", "athena", "guardian", "mercury"]:
        state = read_state(agent)
        if state:
            result[agent] = state
    return result


def get_bulletin() -> str:
    """读取共享公告板 (最近20条)。"""
    path = os.path.join(_STATE_DIR, "..", "bulletin.md")
    if not os.path.exists(path):
        return "📋 公告板为空，等待专员首次报告..."
    with open(path) as f:
        lines = f.readlines()
    return "".join(lines[-40:])  # 最近约20条


def post_bulletin(entry: str):
    """向共享公告板发布一条消息。"""
    path = os.path.join(_STATE_DIR, "..", "bulletin.md")
    timestamp = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(f"\n---\n### {timestamp} — {entry}\n")


def get_context_for(agent: str) -> str:
    """
    生成给指定专员的上下文快照。
    包含：公告板最近消息 + 所有其他专员状态。
    """
    bulletin = get_bulletin()
    states = read_all_states()

    ctx = "📋 共享看板最近动态:\n" + bulletin + "\n\n"
    ctx += "👥 其他专员最新状态:\n"
    for name, state in states.items():
        if name == agent:
            continue
        # 提取关键字段
        summary = {}
        for k, v in state.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str) and len(v) > 100:
                v = v[:100] + "..."
            summary[k] = v
        ctx += f"  [{name.upper()}]: {json.dumps(summary, ensure_ascii=False, default=str)}\n"

    return ctx


# ---- initialize ----
if __name__ == "__main__":
    os.makedirs(_STATE_DIR, exist_ok=True)
    # 初始化公告板
    post_bulletin("🚀 Aether 运营团队启动 — Oracle/Athena/Guardian/Mercury 就位")
    print("✅ 共享看板初始化完成")
    print(get_bulletin())
