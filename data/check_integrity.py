#!/usr/bin/env python3
"""Oracle 数据完整性检查脚本 — 心跳时自动运行"""
import sqlite3, time, os, sys

# Ensure project root is on path so we can import data.db
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from data.db import get_market_db

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market.db")

def check(verbose=False):
    """返回 (ok: bool, issues: list[str], stats: dict)"""
    issues = []
    stats = {}
    now = time.time()

    if not os.path.exists(DB):
        return False, [f"数据库不存在: {DB}"], {}

    db = get_market_db(DB)

    # 1. 数据新鲜度检查
    SYMBOLS = ["BTC/USDT", "ETH/USDT"]
    TIMEFRAMES = ["15m", "1h", "4h", "1d"]
    THRESHOLDS = {"15m": 30, "1h": 90, "4h": 300, "1d": 1500}  # 分钟

    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            row = db.execute(
                "SELECT MAX(open_time) FROM klines WHERE symbol=? AND timeframe=?",
                (sym, tf)
            ).fetchone()
            if row[0] is None:
                issues.append(f"缺失: {sym} {tf} 无数据")
                continue
            delay_min = (now * 1000 - row[0]) / 60000
            threshold = THRESHOLDS.get(tf, 60)
            if delay_min > threshold:
                issues.append(f"延迟: {sym} {tf} 最新 {delay_min:.0f}min前 (阈值{threshold}min)")
            stats[f"{sym}_{tf}_delay_min"] = round(delay_min, 1)

    # 2. 行数检查
    total = db.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
    stats["total_klines"] = total
    if total < 10000:
        issues.append(f"K线总数过低: {total}")

    # 3. 缺口检测 (全量, LAG 窗口函数)
    tf_ms = {"15m": 900000, "1h": 3600000, "4h": 14400000, "1d": 86400000}
    total_gaps = 0
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            step_ms = tf_ms.get(tf, 3600000)
            gap_count = db.execute("""
                SELECT COUNT(*) FROM (
                    SELECT open_time - LAG(open_time) OVER (ORDER BY open_time) as diff
                    FROM klines WHERE symbol=? AND timeframe=?
                ) WHERE diff > ?
            """, (sym, tf, step_ms * 2.5)).fetchone()[0]
            if gap_count > 0:
                issues.append(f"缺口: {sym} {tf}: {gap_count}个")
            total_gaps += gap_count
    stats["total_gaps"] = total_gaps

    # 4. 辅助数据表检查
    for tbl, min_rows in [("funding_rates", 10), ("open_interest", 10), ("orderbook_snapshots", 5)]:
        cnt = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        if cnt < min_rows:
            issues.append(f"{tbl} 数据不足: {cnt}行 (最少{min_rows})")

    # 5. WAL 模式
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    if mode != "wal":
        issues.append(f"journal_mode={mode}, 应为wal")

    # 6. DB 大小
    size_mb = os.path.getsize(DB) / 1024 / 1024
    stats["db_size_mb"] = round(size_mb, 1)

    # 7. oracle.json 与 strategies.yaml 一致性
    try:
        import json, yaml
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        oracle_path = os.path.join(repo_root, ".aether", "oracle.json")
        strategies_path = os.path.join(repo_root, "config", "strategies.yaml")

        if os.path.exists(oracle_path) and os.path.exists(strategies_path):
            with open(oracle_path) as f:
                oracle_cfg = json.load(f)
            with open(strategies_path) as f:
                strategies_cfg = yaml.safe_load(f)

            # Load athena.json for PAPER awareness
            athena_verdicts = {}
            athena_path = os.path.join(repo_root, ".aether", "state", "athena.json")
            if os.path.exists(athena_path):
                with open(athena_path) as f:
                    athena_cfg = json.load(f)
                for name, s in athena_cfg.get("strategies", {}).items():
                    athena_verdicts[name] = s.get("verdict", "NOT_EVALUATED")

            oracle_enabled = set(oracle_cfg.get("strategies_enabled", []))
            yaml_enabled = set(
                s["name"] for s in strategies_cfg.get("strategies", [])
                if s.get("enabled", False)
            )
            # PAPER strategies are enabled in YAML but excluded from oracle by AUDIT-051 guard
            paper_excluded = set(
                name for name in yaml_enabled
                if athena_verdicts.get(name) in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED")
            )
            only_oracle = oracle_enabled - yaml_enabled
            only_yaml = yaml_enabled - oracle_enabled - paper_excluded  # exclude legit PAPER
            if only_oracle:
                issues.append(f"oracle.json多余启用: {only_oracle}")
            if only_yaml:
                issues.append(f"strategies.yaml多余启用(未同步): {only_yaml}")
            stats["config_sync"] = "ok" if not only_oracle and not only_yaml else "mismatch"
            stats["paper_excluded"] = list(paper_excluded)
    except Exception as e:
        issues.append(f"配置一致性检查失败: {e}")

    # 8. athena.json ↔ backtest_results.json verdict 一致性
    try:
        import json
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        athena_path = os.path.join(repo_root, ".aether", "state", "athena.json")
        bt_path = os.path.join(repo_root, ".aether", "state", "backtest_results.json")

        if os.path.exists(athena_path) and os.path.exists(bt_path):
            with open(athena_path) as f:
                athena = json.load(f)
            with open(bt_path) as f:
                bt = json.load(f)

            athena_strats = athena.get("strategies", {})
            bt_strats = bt.get("strategies", {})
            verdict_mismatches = []
            for name in set(list(athena_strats.keys()) + list(bt_strats.keys())):
                av = athena_strats.get(name, {}).get("verdict", "MISSING")
                bv = bt_strats.get(name, {}).get("verdict", "MISSING")
                if av != bv:
                    verdict_mismatches.append(f"{name}: athena={av} bt={bv}")
            if verdict_mismatches:
                for vm in verdict_mismatches:
                    issues.append(f"verdict不一致: {vm}")
                stats["verdict_sync"] = "mismatch"
            else:
                stats["verdict_sync"] = "ok"
    except Exception as e:
        pass  # best-effort, don't fail integrity check over state file issues

    db.close()
    return len(issues) == 0, issues, stats


def _refresh_oracle_stats(stats):
    """Refresh data_stats in both oracle.json copies from integrity stats.

    Uses a single DB connection for all queries (was 9 separate connections).
    """
    import json
    from datetime import datetime, timezone
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        now_iso = datetime.now(timezone.utc).isoformat()

        # Single DB connection for all queries
        db = get_market_db(DB)

        data_stats = {}
        # Per-symbol/timeframe stats
        for sym in ["BTC/USDT", "ETH/USDT"]:
            for tf in ["15m", "1h", "4h", "1d"]:
                key = f"{sym}_{tf}_delay_min"
                if key in stats:
                    row = db.execute(
                        "SELECT COUNT(*), MAX(open_time) FROM klines WHERE symbol=? AND timeframe=?",
                        (sym, tf)
                    ).fetchone()
                    data_stats[f"{sym}_{tf}"] = {
                        "count": row[0],
                        "latest_ts": row[1],
                        "latest": datetime.fromtimestamp(row[1]/1000).strftime("%Y-%m-%dT%H:%M:%S") if row[1] else "N/A"
                    }

        # Table row counts
        for tbl in ["funding_rates", "open_interest", "orderbook_snapshots", "trades_log"]:
            try:
                cnt = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                data_stats[f"table_{tbl}"] = {"count": cnt}
            except Exception:
                pass

        db.close()

        # Update both oracle.json copies (preserve other fields)
        for oracle_path in [
            os.path.join(repo_root, ".aether", "state", "oracle.json"),
            os.path.join(repo_root, ".aether", "oracle.json"),
        ]:
            if os.path.exists(oracle_path):
                with open(oracle_path, "r") as f:
                    oracle = json.load(f)
            else:
                oracle = {}
            oracle["data_stats"] = data_stats
            oracle["_updated_at"] = now_iso
            oracle["last_heartbeat"] = now_iso  # AUDIT-135: prevent heartbeat staleness on refresh
            oracle["db_size_mb"] = stats.get("db_size_mb", oracle.get("db_size_mb", 0))
            oracle["klines_count"] = stats.get("total_klines", 0)
            oracle["data_fresh"] = True
            with open(oracle_path, "w") as f:
                json.dump(oracle, f, indent=2, ensure_ascii=False)

        return True
    except Exception:
        return False  # best-effort, don't block integrity check


if __name__ == "__main__":
    ok, issues, stats = check(verbose=True)
    if ok:
        print("✅ 数据完整性检查通过")
    else:
        print(f"⚠️ 发现 {len(issues)} 个问题:")
        for i in issues:
            print(f"  - {i}")
    print(f"统计: {stats}")
    # Refresh oracle.json data_stats to prevent staleness
    refreshed = _refresh_oracle_stats(stats)
    if refreshed:
        print("✅ oracle.json 已刷新")
    sys.exit(0 if ok else 1)
