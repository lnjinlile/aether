#!/usr/bin/env python3
"""Oracle 数据质量检测工具 — 供 cron 和 pipeline 调用"""
import time, os, sys, json
from collections import defaultdict
from datetime import datetime, timezone

# Ensure project root is on path so we can import data.db
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from data.db import get_market_db  # PERF-013: shared WAL + busy_timeout helper

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")
AETHER_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aether.db")

# 各时间框架的可接受延迟（分钟）
# 允许最多丢失 1 根 K 线：即预期间隔 * 2
# 15m: 30min, 1h: 120min, 4h: 480min, 1d: 2880min(48h)
ACCEPTABLE_DELAY = {
    "15m": 30,
    "1h": 120,
    "4h": 480,
    "1d": 2880,
}

def check_freshness(conn, now_ts=None):
    """检查各 symbol/timeframe 最新数据延迟，返回延迟超标列表"""
    if now_ts is None:
        now_ts = int(time.time() * 1000)
    issues = []
    for symbol in ["BTC/USDT", "ETH/USDT"]:
        for tf, max_delay in ACCEPTABLE_DELAY.items():
            row = conn.execute(
                "SELECT MAX(open_time) FROM klines WHERE symbol=? AND timeframe=?",
                (symbol, tf)
            ).fetchone()
            if not row[0]:
                issues.append(f"{symbol} {tf}: 无数据")
                continue
            delay = (now_ts - row[0]) / 60000
            if delay > max_delay:
                issues.append(f"{symbol} {tf}: 延迟 {delay:.1f}min > {max_delay}min 阈值")
    return issues

def check_gaps(conn, symbol="BTC/USDT", tf="15m", limit=100):
    """检查 K 线缺口"""
    rows = conn.execute(
        "SELECT open_time FROM klines WHERE symbol=? AND timeframe=? ORDER BY open_time DESC LIMIT ?",
        (symbol, tf, limit)
    ).fetchall()
    if len(rows) < 2:
        return []
    timestamps = [r[0] for r in rows]
    # 计算预期间隔
    tf_map = {"1m": 60000, "15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
    expected = tf_map.get(tf, 60000)
    gaps = []
    for i in range(len(timestamps) - 1):
        diff = timestamps[i] - timestamps[i+1]
        if diff != expected:
            gaps.append((timestamps[i+1], timestamps[i], diff, expected))
    return gaps

def check_duplicates(conn):
    """检查重复 K 线"""
    dups = conn.execute(
        "SELECT symbol, timeframe, open_time, COUNT(*) as c FROM klines "
        "GROUP BY symbol, timeframe, open_time HAVING c > 1"
    ).fetchall()
    return dups

def check_db_size(conn, warn_mb=500):
    """检查数据库大小"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > warn_mb:
        return f"DB size {size_mb:.0f}MB > {warn_mb}MB 警告阈值"
    return None


# ── Auxiliary data freshness (orderbook, OI, funding_rates) ──
# These tables are populated by data_ext.py. If they go stale, it means
# data_ext.py is failing silently or the Binance testnet API is down.

AUX_FRESHNESS_WARN = {
    "orderbook_snapshots": 15,   # minutes — collected every ~5min, 3 cycles
    "open_interest": 15,          # minutes — collected every ~5min
    "funding_rates": 480,         # minutes (8h) — funding settles every 8h
    "order_flow": 10,             # minutes — collected every ~5min, 2 cycles (critical for regime detection)
}

AUX_FRESHNESS_CRIT = {
    "orderbook_snapshots": 30,
    "open_interest": 30,
    "funding_rates": 1440,        # 24h — funding should update at least daily
    "order_flow": 20,             # 20min — Mercury regime detection needs fresh order flow
}


def check_aux_freshness(conn, now_ts=None):
    """Check freshness of auxiliary data tables populated by data_ext.py and pipeline.py.

    Returns list of issue strings. Empty list = all healthy.
    """
    if now_ts is None:
        now_ts = int(time.time() * 1000)
    # Map table name → column holding epoch timestamp (seconds or ms)
    TS_COLS = {
        "orderbook_snapshots": "timestamp",   # float seconds
        "open_interest": "timestamp",          # float seconds
        "funding_rates": "funding_time",        # int ms
        "order_flow": "window_start",           # int ms — critical for Mercury regime detection
    }
    issues = []
    for table, ts_col in TS_COLS.items():
        try:
            row = conn.execute(
                f"SELECT MAX({ts_col}) FROM \"{table}\""
            ).fetchone()
            if not row or row[0] is None:
                issues.append(f"{table}: 无数据 (data_ext.py 可能未运行)")
                continue
            latest = row[0]
            # Normalise to milliseconds
            if ts_col == "timestamp":
                latest_ms = int(float(latest) * 1000)
            else:
                latest_ms = int(latest)
            delay_min = (now_ts - latest_ms) / 60000.0
            warn_th = AUX_FRESHNESS_WARN.get(table, 30)
            crit_th = AUX_FRESHNESS_CRIT.get(table, 60)
            if delay_min > crit_th:
                issues.append(
                    f"{table}: 延迟 {delay_min:.0f}min > {crit_th}min CRITICAL"
                )
            elif delay_min > warn_th:
                issues.append(
                    f"{table}: 延迟 {delay_min:.0f}min > {warn_th}min WARNING"
                )
        except Exception as e:
            issues.append(f"{table}: 检查失败 ({e})")
    return issues


def full_check():
    """全量数据质量检查，返回 (status, issues, stats)"""
    conn = get_market_db()  # PERF-013: WAL + busy_timeout=10s
    now_ts = int(time.time() * 1000)
    issues = []

    # 1. 数据新鲜度
    issues.extend(check_freshness(conn, now_ts))

    # 2. 缺口检查（BTC/ETH 15m 最近 100 条）
    for symbol in ["BTC/USDT", "ETH/USDT"]:
        gaps = check_gaps(conn, symbol, "15m", 100)
        for g in gaps:
            missing = (g[2] // (g[3])) - 1
            if missing > 0:
                issues.append(f"{symbol} 15m: {missing} 条缺口 @ {g[0]} -> {g[1]}")

    # 3. 重复检查
    dups = check_duplicates(conn)
    for d in dups:
        issues.append(f"{d[0]}/{d[1]} @ {d[2]}: {d[3]} 条重复")

    # 4. DB 大小
    size_issue = check_db_size(conn)
    if size_issue:
        issues.append(size_issue)

    # 5. 辅助数据新鲜度 (orderbook, OI, funding_rates — 由 data_ext.py 采集)
    issues.extend(check_aux_freshness(conn, now_ts))

    # 统计
    stats = {}
    for symbol in ["BTC/USDT", "ETH/USDT"]:
        for tf in ["15m", "1h", "4h", "1d"]:
            row = conn.execute(
                "SELECT COUNT(*), MAX(open_time) FROM klines WHERE symbol=? AND timeframe=?",
                (symbol, tf)
            ).fetchone()
            key = f"{symbol}_{tf}"
            stats[key] = {"count": row[0], "latest": row[1]}
            if row[1]:
                stats[key]["delay_min"] = round((now_ts - row[1]) / 60000, 2)

    conn.close()

    if not issues:
        status = "healthy"
    elif any("无数据" in i for i in issues):
        status = "critical"
    elif len(issues) >= 3:
        status = "warning"
    else:
        status = "healthy"  # 少量延迟属于正常

    return status, issues, stats

if __name__ == "__main__":
    status, issues, stats = full_check()
    print(f"Status: {status}")
    print(f"Issues ({len(issues)}):")
    for i in issues:
        print(f"  - {i}")
    print(f"Stats: {json.dumps(stats, indent=2)}")
