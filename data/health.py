#!/usr/bin/env python3
"""Oracle 数据健康检查模块 — 统一数据质量诊断入口"""
import sqlite3, time, os, sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "market.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def check_klines_latency(symbols: Optional[List[str]] = None,
                         timeframes: Optional[List[str]] = None) -> Dict:
    """检查每个 symbol/tf 的最新 K 线延迟（分钟）。
    阈值按时间框架自适应：15m/warn>30/crit>60, 1h/warn>120/crit>240,
    4h/warn>300/crit>480, 1d/warn>1500/crit>1800"""
    if symbols is None:
        symbols = ["BTC/USDT", "ETH/USDT"]
    if timeframes is None:
        timeframes = ["15m", "1h", "4h", "1d"]

    TF_THRESHOLDS = {
        "15m": (30, 60),
        "1h": (120, 240),
        "4h": (300, 480),
        "1d": (1500, 1800),
    }

    conn = get_conn()
    now = int(time.time() * 1000)
    result = {}
    try:
        for sym in symbols:
            for tf in timeframes:
                row = conn.execute(
                    "SELECT open_time, close FROM klines WHERE symbol=? AND timeframe=? ORDER BY open_time DESC LIMIT 1",
                    (sym, tf)
                ).fetchone()
                if row:
                    delay = (now - row[0]) / 60000.0
                    warn_th, crit_th = TF_THRESHOLDS.get(tf, (120, 360))
                    if delay > crit_th:
                        status = "critical"
                    elif delay > warn_th:
                        status = "warn"
                    else:
                        status = "ok"
                    result[f"{sym}/{tf}"] = {
                        "open_time_ms": row[0],
                        "open_time_utc": datetime.fromtimestamp(row[0]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        "close": row[1],
                        "delay_min": round(delay, 1),
                        "status": status
                    }
                else:
                    result[f"{sym}/{tf}"] = {"status": "missing", "delay_min": None}
    finally:
        conn.close()
    return result


def check_table_counts() -> Dict[str, int]:
    """返回所有表的行数"""
    conn = get_conn()
    counts = {}
    try:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for (name,) in tables:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            counts[name] = cnt
    finally:
        conn.close()
    return counts


def check_duplicates() -> List[Tuple]:
    """检查 klines 表重复数据"""
    conn = get_conn()
    dupes = []
    try:
        dupes = conn.execute("""
            SELECT symbol, timeframe, open_time, COUNT(*) as cnt
            FROM klines
            GROUP BY symbol, timeframe, open_time
            HAVING cnt > 1
            ORDER BY cnt DESC
        """).fetchall()
    finally:
        conn.close()
    return dupes


def check_gaps(symbols: Optional[List[str]] = None,
               timeframes: Optional[List[str]] = None) -> Dict:
    """检测K线时间序列缺口（缺失的连续蜡烛）。

    对每个 symbol/tf，检查相邻 open_time 之间的间隔是否超过预期。
    返回 {f'{sym}/{tf}': {'total_gaps': N, 'max_gap_bars': M, 'gaps': [...]}}
    """
    if symbols is None:
        symbols = ["BTC/USDT", "ETH/USDT"]
    if timeframes is None:
        timeframes = ["15m", "1h", "4h", "1d"]

    TF_MS = {"15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}

    conn = get_conn()
    result = {}
    try:
        for sym in symbols:
            for tf in timeframes:
                rows = conn.execute(
                    "SELECT open_time FROM klines WHERE symbol=? AND timeframe=? ORDER BY open_time ASC",
                    (sym, tf)
                ).fetchall()
                if not rows:
                    result[f"{sym}/{tf}"] = {"total_gaps": 0, "max_gap_bars": 0, "gaps": [], "note": "empty"}
                    continue

                expected_step = TF_MS.get(tf, 3600000)
                gaps = []
                max_gap = 0
                for i in range(1, len(rows)):
                    diff = rows[i][0] - rows[i-1][0]
                    if diff > expected_step * 1.1:  # 10% tolerance
                        missed = round(diff / expected_step) - 1
                        gaps.append({
                            "from": rows[i-1][0],
                            "to": rows[i][0],
                            "missed_bars": missed
                        })
                        if missed > max_gap:
                            max_gap = missed

                result[f"{sym}/{tf}"] = {
                    "total_gaps": len(gaps),
                    "max_gap_bars": max_gap,
                    "gaps": gaps[:20]  # 只保留前20个缺口详情
                }
    finally:
        conn.close()
    return result


def check_db_size() -> Dict:
    """DB 大小和 WAL 状态"""
    conn = get_conn()
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    wal_size = os.path.getsize(DB_PATH + "-wal") if os.path.exists(DB_PATH + "-wal") else 0
    shm_size = os.path.getsize(DB_PATH + "-shm") if os.path.exists(DB_PATH + "-shm") else 0
    return {
        "db_mb": round(size / 1024 / 1024, 1),
        "wal_mb": round(wal_size / 1024 / 1024, 2),
        "shm_mb": round(shm_size / 1024 / 1024, 2),
        "journal_mode": journal
    }


def full_report() -> str:
    """生成完整健康报告文本"""
    lines = []
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"=== Oracle 数据健康报告 [{now_utc}] ===")

    # 1. K线延迟
    lat = check_klines_latency()
    for key, info in sorted(lat.items()):
        if info.get("status") == "missing":
            lines.append(f"  {key}: ❌ 无数据")
        else:
            flag = "⚠️" if info["status"] == "warn" else ("🚨" if info["status"] == "critical" else "✓")
            lines.append(f"  {key}: {flag} {info['open_time_utc']} close={info['close']} 延迟{info['delay_min']}min")

    # 2. 表行数
    lines.append("\n--- 表统计 ---")
    counts = check_table_counts()
    # testnet 不支持的表：预期为空
    TESTNET_EXPECTED_EMPTY = {"long_short_ratio", "taker_volume"}
    empty_tables = []
    for tbl, cnt in sorted(counts.items()):
        if cnt == 0 and tbl not in TESTNET_EXPECTED_EMPTY:
            flag = " ⚠️ 空表"
            empty_tables.append(tbl)
        elif cnt == 0:
            flag = " (testnet 预期空)"
        else:
            flag = ""
        lines.append(f"  {tbl}: {cnt:,} rows{flag}")

    # 3. 重复检查
    dupes = check_duplicates()
    if dupes:
        lines.append(f"\n⚠️ 重复K线: {len(dupes)} 组")
        for d in dupes[:5]:
            lines.append(f"  {d[0]} {d[1]} open_time={d[2]} x{d[3]}")
    else:
        lines.append("\n✓ 无重复K线")

    # 3b. 缺口检查
    all_gaps = check_gaps()
    gap_found = False
    for key, info in sorted(all_gaps.items()):
        if info.get("total_gaps", 0) > 0:
            if not gap_found:
                lines.append("\n--- K线缺口 ---")
                gap_found = True
            lines.append(f"  {key}: {info['total_gaps']}处缺口, 最大缺失{info['max_gap_bars']}根")
    if not gap_found:
        lines.append("✓ 无K线缺口")

    # 4. DB 大小
    dbs = check_db_size()
    lines.append(f"\n--- 存储 ---")
    lines.append(f"  market.db: {dbs['db_mb']}MB | WAL: {dbs['wal_mb']}MB | SHM: {dbs['shm_mb']}MB | journal: {dbs['journal_mode']}")

    # 5. 总结：只标记超出自身 TF 告警阈值的延迟
    issues = []
    TF_WARN = {"15m": 30, "1h": 120, "4h": 300, "1d": 1500}
    for key, info in lat.items():
        if info.get("status") == "missing":
            issues.append(f"{key} 无数据")
        elif info.get("status") in ("warn", "critical"):
            issues.append(f"{key} 延迟{info['delay_min']}min>{TF_WARN.get(key.split('/')[-1], 120)}min阈值")
    if empty_tables:
        issues.append(f"异常空表: {empty_tables}")
    if dupes:
        issues.append(f"{len(dupes)} 组重复数据")

    if issues:
        lines.append(f"\n⚠️ 发现问题: {'; '.join(issues)}")
    else:
        lines.append(f"\n✅ 数据健康: 全部正常")

    return "\n".join(lines)


# One-liner heartbeat (for cron/feed)
def heartbeat_line() -> str:
    """返回一行心跳摘要字符串，按时间框架自适应延迟阈值"""
    lat = check_klines_latency()
    counts = check_table_counts()
    dbs = check_db_size()
    dupes = check_duplicates()

    # 按 TF 自带阈值判断：只报告真正超阈值的
    TF_WARN = {"15m": 30, "1h": 120, "4h": 300, "1d": 1500}
    worst = None
    for key, info in lat.items():
        if info.get("status") in ("warn", "critical"):
            d = info.get("delay_min", 0) or 0
            tf = key.split("/")[-1]
            if worst is None or (d / TF_WARN.get(tf, 120)) > worst[1]:
                worst = (key, d / TF_WARN.get(tf, 120), d)

    klines_total = counts.get("klines", 0)
    dup_flag = " DUP!" if dupes else ""
    
    # 缺口检测
    all_gaps = check_gaps()
    gap_count = sum(info.get("total_gaps", 0) for info in all_gaps.values())
    gap_flag = f" GAPS:{gap_count}" if gap_count > 0 else ""
    
    empty_flag = ""
    for tbl in ("orderbook_snapshots", "open_interest", "funding_rates"):
        if counts.get(tbl, 0) == 0:
            empty_flag += f" {tbl}=EMPTY"

    latency_part = ""
    if worst:
        latency_part = f" | {worst[0]} 延迟{worst[2]:.0f}min(超标{worst[1]:.1f}x)"
    else:
        latency_part = " | 延迟正常"

    return (f"market.db {dbs['db_mb']}MB/{klines_total}klines/{dbs['journal_mode']}{dup_flag}{gap_flag}"
            f"{latency_part}{empty_flag}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "beat":
        print(heartbeat_line())
    else:
        print(full_report())
