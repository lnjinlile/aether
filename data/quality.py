#!/usr/bin/env python3
"""
数据质量自动检测模块。
检查 klines 数据的新鲜度、连续性、完整性。
可供 pipeline.py 或独立脚本调用。
"""
import sqlite3
import time
import logging
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger("data.quality")

# 各 timeframe 的 K 线持续时间（分钟）和允许的额外延迟
TIMEFRAME_DURATION_MIN = {
    "1m": 1,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}
# 蜡烛闭合后允许的最大额外延迟（分钟）
MAX_EXTRA_DELAY_MIN = {
    "1m": 3,
    "15m": 8,
    "1h": 15,
    "4h": 30,
    "1d": 120,
}

# 各 timeframe 每个 symbol 的最小预期 bar 数（365天）
MIN_BARS = {
    "15m": 28000,
    "1h": 7000,
    "4h": 1750,
    "1d": 290,
}


class DataQualityCheck:
    """对 market.db 中的数据执行一系列质量检查。"""

    def __init__(self, db_path: str = "data/market.db"):
        self.db_path = db_path
        self.issues: List[Dict] = []

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    def run_all(self, symbols: List[str] = None, timeframes: List[str] = None) -> Dict:
        """执行全部质量检查，返回结果字典。"""
        if symbols is None:
            symbols = ["BTC/USDT", "ETH/USDT"]
        if timeframes is None:
            timeframes = ["15m", "1h", "4h", "1d"]

        self.issues = []
        results = {
            "timestamp": time.time(),
            "freshness": self._check_freshness(symbols, timeframes),
            "completeness": self._check_completeness(symbols, timeframes),
            "gaps": self._check_gaps(symbols, timeframes),
            "data_ext": self._check_data_ext_sources(),
            "health": "ok" if len(self.issues) == 0 else "degraded",
            "issues": self.issues,
        }
        return results

    def _check_freshness(self, symbols: List[str], timeframes: List[str]) -> Dict:
        """检查各 timeframe 最新数据是否在允许延迟内。"""
        conn = self._conn()
        results = {}
        try:
            now = time.time()
            for sym in symbols:
                for tf in timeframes:
                    row = conn.execute(
                        "SELECT MAX(open_time) as latest FROM klines WHERE symbol=? AND timeframe=?",
                        (sym, tf),
                    ).fetchone()
                    if row and row["latest"]:
                        age_min = (now - row["latest"] / 1000) / 60
                        # 允许延迟 = K线持续时间 + 额外闭合延迟（未闭合蜡烛不算延迟）
                        max_delay = TIMEFRAME_DURATION_MIN.get(tf, 15) + MAX_EXTRA_DELAY_MIN.get(tf, 15)
                        ok = age_min <= max_delay
                        results[f"{sym}_{tf}"] = {
                            "latest_open_time": row["latest"],
                            "age_minutes": round(age_min, 1),
                            "max_delay_minutes": max_delay,
                            "ok": ok,
                        }
                        if not ok:
                            msg = f"{sym} {tf}: 延迟 {age_min:.1f}min > {max_delay}min"
                            self.issues.append({"type": "freshness", "msg": msg})
                            logger.warning(msg)
                    else:
                        results[f"{sym}_{tf}"] = {"latest_open_time": None, "age_minutes": None, "ok": False}
                        msg = f"{sym} {tf}: 无数据"
                        self.issues.append({"type": "freshness", "msg": msg})
                        logger.warning(msg)
        finally:
            conn.close()
        return results

    def _check_completeness(self, symbols: List[str], timeframes: List[str]) -> Dict:
        """检查各 timeframe 数据量是否满足最低要求。"""
        conn = self._conn()
        results = {}
        try:
            for sym in symbols:
                for tf in timeframes:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM klines WHERE symbol=? AND timeframe=?",
                        (sym, tf),
                    ).fetchone()
                    count = row["cnt"] if row else 0
                    min_expected = MIN_BARS.get(tf, 100)
                    ok = count >= min_expected
                    results[f"{sym}_{tf}"] = {
                        "count": count,
                        "min_expected": min_expected,
                        "ok": ok,
                    }
                    if not ok:
                        msg = f"{sym} {tf}: 仅有 {count} bars, 需要 >= {min_expected}"
                        self.issues.append({"type": "completeness", "msg": msg})
                        logger.warning(msg)
        finally:
            conn.close()
        return results

    def _check_data_ext_sources(self) -> Dict:
        """检查 data_ext.py 采集的数据源（orderbook_snapshots, funding_rates, open_interest）
        以及写入速率（检测僵尸进程：进程存活但无数据产出）。"""
        conn = self._conn()
        results = {}
        now_s = time.time()
        now_ms = int(now_s * 1000)
        # data_ext 使用无斜杠的 symbol 格式: BTCUSDT, ETHUSDT
        data_ext_symbols = ["BTCUSDT", "ETHUSDT"]

        try:
            # ── orderbook_snapshots (秒级时间戳, 期望 < 10 分钟) ──
            for sym in data_ext_symbols:
                row = conn.execute(
                    "SELECT MAX(timestamp) as latest FROM orderbook_snapshots WHERE symbol=?",
                    (sym,),
                ).fetchone()
                key = f"ob_{sym}"
                if row and row["latest"]:
                    delay_min = (now_s - row["latest"]) / 60
                    ok = delay_min < 10
                    results[key] = {"latest": row["latest"], "delay_min": round(delay_min, 1), "ok": ok}
                    if not ok:
                        msg = f"orderbook {sym}: 延迟 {delay_min:.1f}min > 10min"
                        self.issues.append({"type": "freshness_data_ext", "msg": msg})
                        logger.warning(msg)
                else:
                    results[key] = {"latest": None, "delay_min": None, "ok": False}
                    msg = f"orderbook {sym}: 无数据"
                    self.issues.append({"type": "freshness_data_ext", "msg": msg})
                    logger.warning(msg)

            # ── open_interest (秒级时间戳, 期望 < 10 分钟) ──
            for sym in data_ext_symbols:
                row = conn.execute(
                    "SELECT MAX(timestamp) as latest FROM open_interest WHERE symbol=?",
                    (sym,),
                ).fetchone()
                key = f"oi_{sym}"
                if row and row["latest"]:
                    delay_min = (now_s - row["latest"]) / 60
                    ok = delay_min < 10
                    results[key] = {"latest": row["latest"], "delay_min": round(delay_min, 1), "ok": ok}
                    if not ok:
                        msg = f"open_interest {sym}: 延迟 {delay_min:.1f}min > 10min"
                        self.issues.append({"type": "freshness_data_ext", "msg": msg})
                        logger.warning(msg)
                else:
                    results[key] = {"latest": None, "delay_min": None, "ok": False}
                    msg = f"open_interest {sym}: 无数据"
                    self.issues.append({"type": "freshness_data_ext", "msg": msg})
                    logger.warning(msg)

            # ── funding_rates (毫秒时间戳, 每8h更新, 期望 < 12h) ──
            # funding_rates 表同时包含秒级和毫秒级时间戳，需要判断
            for sym in data_ext_symbols:
                row = conn.execute(
                    "SELECT MAX(funding_time) as latest FROM funding_rates WHERE symbol=?",
                    (sym,),
                ).fetchone()
                key = f"fr_{sym}"
                if row and row["latest"]:
                    ts_raw = row["latest"]
                    # 判断是毫秒 (>1e12) 还是秒
                    if ts_raw > 1e12:
                        delay_min = (now_ms - ts_raw) / 60000
                    else:
                        delay_min = (now_s - ts_raw) / 60
                    ok = delay_min < 720  # 12小时
                    results[key] = {"latest": ts_raw, "delay_min": round(delay_min, 1), "ok": ok}
                    if not ok:
                        msg = f"funding_rate {sym}: 延迟 {delay_min:.1f}min > 12h"
                        self.issues.append({"type": "freshness_data_ext", "msg": msg})
                        logger.warning(msg)
                else:
                    results[key] = {"latest": None, "delay_min": None, "ok": False}
                    msg = f"funding_rate {sym}: 无数据"
                    self.issues.append({"type": "freshness_data_ext", "msg": msg})
                    logger.warning(msg)

            # ── 写入速率检查 (过去1小时内记录数, 检测僵尸进程) ──
            write_rate_checks = {
                "orderbook_snapshots": ("ob", 300, 8),    # ~5min间隔, 期望≥8条/h
                "open_interest": ("oi", 300, 8),           # ~5min间隔, 期望≥8条/h
            }
            for tbl, (prefix, interval_s, min_per_hour) in write_rate_checks.items():
                for sym in data_ext_symbols:
                    key = f"{prefix}_rate_{sym}"
                    # 兼容秒级和毫秒级 created_at
                    col = "created_at" if tbl == "orderbook_snapshots" else "timestamp"
                    cutoff_s = now_s - 3600
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE symbol=? AND {col} >= ?",
                        (sym, cutoff_s),
                    ).fetchone()[0]
                    ok = count >= min_per_hour
                    results[key] = {"records_last_hour": count, "min_expected": min_per_hour, "ok": ok}
                    if not ok:
                        msg = f"{tbl} {sym}: 过去1h仅{count}条记录 (期望≥{min_per_hour}) — 可能僵尸进程"
                        self.issues.append({"type": "write_rate", "msg": msg})
                        logger.warning(msg)

        finally:
            conn.close()
        return results

    def _check_gaps(self, symbols: List[str], timeframes: List[str]) -> Dict:
        """检查是否有大的数据缺口（连续缺失超过 2 根 K 线）。"""
        conn = self._conn()
        results = {}
        tf_ms = {"15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
        try:
            for sym in symbols:
                for tf in timeframes:
                    step_ms = tf_ms.get(tf, 3600000)
                    # 用窗口函数找缺口
                    rows = conn.execute(
                        """
                        SELECT open_time,
                               LEAD(open_time) OVER (ORDER BY open_time) as next_time
                        FROM klines
                        WHERE symbol=? AND timeframe=?
                        ORDER BY open_time
                        """,
                        (sym, tf),
                    ).fetchall()

                    gaps = []
                    for r in rows:
                        if r["next_time"] and (r["next_time"] - r["open_time"]) > step_ms * 2.5:
                            missing = int((r["next_time"] - r["open_time"]) / step_ms) - 1
                            gaps.append({
                                "from": r["open_time"],
                                "to": r["next_time"],
                                "missing_bars": missing,
                            })

                    results[f"{sym}_{tf}"] = {
                        "gap_count": len(gaps),
                        "gaps": gaps[:10],  # 只报前10个
                        "ok": len(gaps) == 0,
                    }

                    if gaps:
                        total_missing = sum(g["missing_bars"] for g in gaps)
                        msg = f"{sym} {tf}: {len(gaps)} 个缺口, 共缺失 {total_missing} bars"
                        self.issues.append({"type": "gap", "msg": msg, "gaps": gaps[:5]})
                        logger.warning(msg)
        finally:
            conn.close()
        return results


# ── CLI ──
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [QUALITY] %(message)s")

    checker = DataQualityCheck()
    results = checker.run_all()

    print(f"\n{'='*50}")
    print(f"数据质量检查 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"健康状态: {results['health'].upper()}")
    print(f"{'='*50}")

    # 新鲜度
    print("\n── 新鲜度 ──")
    for key, v in results["freshness"].items():
        if v["age_minutes"] is not None:
            status = "OK" if v["ok"] else "DELAY"
            print(f"  {key}: {v['age_minutes']:.1f}min ago [{status}]")
        else:
            print(f"  {key}: NO DATA [FAIL]")

    # 完整性
    print("\n── 完整性 ──")
    for key, v in results["completeness"].items():
        status = "OK" if v["ok"] else "LOW"
        print(f"  {key}: {v['count']} bars (min {v['min_expected']}) [{status}]")

    # 缺口
    print("\n── 缺口 ──")
    for key, v in results["gaps"].items():
        status = "OK" if v["ok"] else f"{v['gap_count']} gaps"
        print(f"  {key}: [{status}]")

    # data_ext 数据源
    print("\n── data_ext 数据源 ──")
    for key, v in results.get("data_ext", {}).items():
        if key.startswith(("ob_rate_", "oi_rate_")):
            continue  # skip rate checks, show separately
        if v["delay_min"] is not None:
            status = "OK" if v["ok"] else "DELAY"
            print(f"  {key}: {v['delay_min']:.1f}min ago [{status}]")
        else:
            print(f"  {key}: NO DATA [FAIL]")

    # 写入速率
    print("\n── 写入速率 (过去1h) ──")
    for key, v in results.get("data_ext", {}).items():
        if key.startswith(("ob_rate_", "oi_rate_")):
            status = "OK" if v["ok"] else "LOW"
            print(f"  {key}: {v['records_last_hour']}条/h (min {v['min_expected']}) [{status}]")

    if results["issues"]:
        print(f"\n── 共 {len(results['issues'])} 个问题 ──")
        for i, iss in enumerate(results["issues"]):
            print(f"  [{i+1}] {iss['type']}: {iss['msg']}")

    sys.exit(0 if results["health"] == "ok" else 1)
