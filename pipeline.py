#!/usr/bin/env python3
"""Aether 数据管道 — 后台自动运行，无需专员干预"""
import sys, os, time, json, logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone

# PERF-009: Module-level base directory — eliminates 6 redundant
# os.path.dirname(os.path.abspath(__file__)) evaluations per cycle.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)
from dotenv import load_dotenv; load_dotenv(os.path.join(BASE_DIR, ".env"))

LOG_FILE = os.path.join(BASE_DIR, "logs", "pipeline.log")
def _setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s [DATA] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
_setup_logging()
logger = logging.getLogger("pipeline")

SYMBOLS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]
INTERVAL = 300  # 5 minutes
HISTORICAL_DAYS = 365
# FUNDING_INTERVAL removed — funding rates now collected exclusively by data_ext.py

STATE_FILE = os.path.join(BASE_DIR, ".aether", "state", "pipeline.json")


def _min_expected_bars(timeframe: str, days: int = HISTORICAL_DAYS) -> int:
    """Minimum expected bar count for a given timeframe and lookback period."""
    from data.collector import BinanceDataCollector
    ms = BinanceDataCollector._timeframe_to_ms(timeframe)
    total_ms = days * 24 * 60 * 60 * 1000
    return int(total_ms // ms * 0.80)  # 80% threshold to account for schedule gaps


def _needs_backfill(storage, symbol: str, timeframe: str) -> bool:
    """Check if a symbol/timeframe combo has enough data."""
    import sqlite3
    conn = storage._get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM klines WHERE symbol=? AND timeframe=?",
            (symbol, timeframe)
        ).fetchone()
        count = row[0] if row else 0
    finally:
        conn.close()

    min_expected = _min_expected_bars(timeframe)
    needed = count < min_expected
    if needed:
        logger.info(
            "  %s %s: %d bars in DB (need >=%d) → backfill required",
            symbol, timeframe, count, min_expected
        )
    else:
        logger.info(
            "  %s %s: %d bars in DB (>=%d) → OK",
            symbol, timeframe, count, min_expected
        )
    return needed


def _backfill_all(collector, storage):
    """Fetch 365 days of historical data for all symbol/timeframe combos that need it."""
    logger.info("=== Historical backfill check ===")
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            if _needs_backfill(storage, sym, tf):
                logger.info("Backfilling %s %s (%d days)...", sym, tf, HISTORICAL_DAYS)
                try:
                    df = collector.fetch_historical(sym, tf, days=HISTORICAL_DAYS)
                    storage.save_klines(df, sym, tf)
                    logger.info("Backfill complete: %s %s — %d bars saved", sym, tf, len(df))
                except Exception as e:
                    logger.error("Backfill failed %s %s: %s", sym, tf, e)
    logger.info("=== Historical backfill done ===")


def run():
    from data.collector import BinanceDataCollector
    from data.storage import MarketStorage
    from config.settings import get_config

    cfg = get_config()
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
    storage = MarketStorage()

    logger.info("Data pipeline started — %s %s every %ds", SYMBOLS, TIMEFRAMES, INTERVAL)

    # ── First boot: backfill 365 days of historical data ──
    _backfill_all(collector, storage)

    _prev_cross_warnings = None  # dedup AUDIT-051 guard logging
    _last_cross_warning_ts = 0   # rate-limit: max 1 warning burst per 30 min

    while True:
        try:
            stats = {}
            errors = []
            for sym in SYMBOLS:
                for tf in TIMEFRAMES:
                    for attempt in range(3):  # retry transient API failures
                        try:
                            df = collector.fetch_current_klines(sym, tf, 300)
                            storage.save_klines(df, sym, tf)
                            stats[f"{sym}_{tf}"] = len(df)
                            break
                        except Exception as e:
                            if attempt < 2:
                                logger.warning("%s %s attempt %d failed, retrying: %s", sym, tf, attempt+1, e)
                                time.sleep(5)
                            else:
                                stats[f"{sym}_{tf}"] = 0
                                errors.append({"feed": f"{sym}_{tf}", "error": str(e)[:200]})
                                logger.error("%s %s: %s", sym, tf, e)

            # ── Orderbook / Funding rates: handled by data_ext.py (single source of truth) ──
            # REMOVED: orderbook + funding_rate now collected exclusively by data_ext.py
            # This avoids dual-write to funding_rates table and dead orderbook table.
            # data_ext.py uses "BTCUSDT" symbol format, consistent with ml_alpha consumers.

            # ── Periodic ANALYZE (every 6 hours) ──
            if int(time.time()) % 21600 < INTERVAL:
                try:
                    _maint_conn = storage._get_conn()
                    _maint_conn.execute("ANALYZE")
                    logger.info("ANALYZE complete — query planner stats refreshed")
                    # WAL checkpoint (truncate to keep WAL file small)
                    _maint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    logger.info("WAL checkpoint complete")
                    _maint_conn.close()
                    # Cleanup old auxiliary data (30d retention, with vacuum if fragmented)
                    from data.db import cleanup_old_data
                    _cleanup_stats = cleanup_old_data(retention_days=30)
                    _deleted = sum(v for k, v in _cleanup_stats.items() if k != "vacuum")
                    if _deleted > 0:
                        logger.info("Data cleanup: %d rows pruned across orderbook/OI/order_flow. %s",
                                   _deleted, _cleanup_stats.get("vacuum", ""))
                except Exception:
                    pass

            # ── Periodic data quality check (every hour) ──
            if int(time.time()) % 3600 < INTERVAL:
                try:
                    from data.quality import full_check
                    qstatus, qissues, qstats = full_check()
                    if qstatus != "healthy":
                        logger.warning("Quality check DEGRADED: %d issues", len(qissues))
                        for iss in qissues[:3]:
                            logger.warning("  %s", iss)
                except Exception as e:
                    logger.warning("Quality check failed: %s", e)

            # Write health status
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "status": "running",
                    "last_run": datetime.now(timezone.utc).isoformat(),
                    "symbols": SYMBOLS,
                    "timeframes": TIMEFRAMES,
                    "interval_sec": INTERVAL,
                    "latest": stats,
                    "errors": errors,
                }, f, indent=2)

            # ── Touch oracle.json freshness (prevents AUDIT-005 stale state regressions) ──
            try:
                _now_iso = datetime.now(timezone.utc).isoformat()
                _data_stats = {}  # safety default
                # Compute latest kline timestamp + total count from DB
                _conn = storage._get_conn()
                try:
                    _row = _conn.execute(
                        "SELECT MAX(open_time), COUNT(*) FROM klines"
                    ).fetchone()
                    _last_klines_ts = _row[0]
                    _klines_count = _row[1]
                    # ── Refresh data_stats (per-symbol/timeframe) ──
                    for _sym in SYMBOLS:
                        for _tf in TIMEFRAMES:
                            _sr = _conn.execute(
                                "SELECT COUNT(*), MAX(open_time) FROM klines WHERE symbol=? AND timeframe=?",
                                (_sym, _tf)
                            ).fetchone()
                            _data_stats[f"{_sym}_{_tf}"] = {
                                "count": _sr[0],
                                "latest_ts": _sr[1],
                                "latest": datetime.fromtimestamp(_sr[1]/1000).strftime("%Y-%m-%dT%H:%M:%S") if _sr[1] else "N/A"
                            }
                    # Add auxiliary table row counts
                    for _tbl in ["funding_rates", "open_interest", "orderbook_snapshots", "order_flow", "trades_log"]:
                        try:
                            _cnt = _conn.execute(f"SELECT COUNT(*) FROM {_tbl}").fetchone()[0]
                            _data_stats[f"table_{_tbl}"] = {"count": _cnt}
                        except Exception:
                            pass
                finally:
                    _conn.close()
                _my_pid = os.getpid()  # actual python process PID, not the bash wrapper
                # ── Find data_ext PID (same approach as engine._find_pid) ──
                _data_ext_pid = None
                try:
                    import subprocess as _sp
                    _r = _sp.run(["pgrep", "-f", "python3 data_ext.py"], capture_output=True, text=True, timeout=5)
                    _pids = [int(x) for x in _r.stdout.strip().split("\n") if x]
                    for _pid in _pids:
                        try:
                            _comm = open(f"/proc/{_pid}/comm").read().strip()
                            if _comm != "python3":
                                continue
                            _cl = open(f"/proc/{_pid}/cmdline").read()
                            _parts = _cl.replace("\x00", " ").strip().split()
                            if len(_parts) >= 2 and _parts[1].endswith("data_ext.py"):
                                _data_ext_pid = _pid
                                break
                        except Exception:
                            pass
                except Exception:
                    pass
                # Pre-compute strategies_enabled from strategies.yaml (AUDIT-047 guard)
                # Prevents drift between .aether/oracle.json and .aether/state/oracle.json
                _synced_enabled = None
                _synced_disabled = None
                _synced_live = None   # AUDIT-098: strategies_live field
                _synced_paper = None  # AUDIT-098: strategies_paper field
                _cross_file_warnings = []  # AUDIT-051: track PAPER→enabled violations
                try:
                    import yaml
                    _yaml_path = os.path.join(BASE_DIR, "config", "strategies.yaml")
                    with open(_yaml_path) as _yf:
                        _yaml_cfg = yaml.safe_load(_yf)
                    _strats = _yaml_cfg.get("strategies", [])
                    _synced_enabled = [s["name"] for s in _strats if s.get("enabled", False)]
                    _synced_disabled = len([s for s in _strats if not s.get("enabled", False)])
                    # AUDIT-051 guard: cross-check strategies.yaml enabled vs athena.json verdicts
                    # PAPER/DO_NOT_ENABLE/RETIRED strategies must not be in strategies_enabled
                    try:
                        _athena_path = os.path.join(BASE_DIR, ".aether", "state", "athena.json")
                        with open(_athena_path) as _af:
                            _athena = json.load(_af)
                        _athena_strats = _athena.get("strategies", {})
                        # AUDIT-051-L2: also check backtest_results.json as secondary truth source
                        _bt_strats = {}
                        try:
                            _bt_path = os.path.join(BASE_DIR, ".aether", "state", "backtest_results.json")
                            with open(_bt_path) as _btf:
                                _bt = json.load(_btf)
                            _bt_strats = _bt.get("strategies", {})
                        except Exception:
                            pass
                        _filtered = []
                        for _name in _synced_enabled:
                            _as = _athena_strats.get(_name, {})
                            _a_verdict = _as.get("verdict", "NOT_EVALUATED")
                            _bs = _bt_strats.get(_name, {})
                            _b_verdict = _bs.get("verdict", _a_verdict)
                            # Use the more conservative verdict between athena and backtest_results
                            _conservative = _a_verdict
                            if _b_verdict in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED"):
                                _conservative = _b_verdict
                            if _a_verdict in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED"):
                                _conservative = _a_verdict
                            if _conservative in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED"):
                                _cross_file_warnings.append(f"{_name} enabled=True in YAML but verdict={_conservative} (athena={_a_verdict}, bt={_b_verdict}) → EXCLUDED")
                                _synced_disabled += 1
                            else:
                                _filtered.append(_name)
                        _synced_enabled = _filtered
                        # AUDIT-098 guard: compute strategies_live and strategies_paper
                        # from the cross-check results (prevents field regression)
                        _synced_live = list(_filtered)
                        _synced_paper = []
                        for _name in [s["name"] for s in _strats if s.get("enabled", False)]:
                            _as = _athena_strats.get(_name, {})
                            _a_verdict = _as.get("verdict", "NOT_EVALUATED")
                            _bs = _bt_strats.get(_name, {})
                            _b_verdict = _bs.get("verdict", _a_verdict)
                            _conservative = _a_verdict
                            if _b_verdict in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED"):
                                _conservative = _b_verdict
                            if _a_verdict in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED"):
                                _conservative = _a_verdict
                            if _conservative == "PAPER":
                                _synced_paper.append(_name)
                    except Exception as _e:
                        # AUDIT-051-L3: fail-safe — on guard failure, preserve previous state
                        # instead of falling through to raw YAML list
                        logger.warning("AUDIT-051 guard failed (cross-check error): %s — preserving previous strategies_enabled", _e)
                        _synced_enabled = None  # prevent raw YAML override; keep previous oracle.json state
                except Exception as _e:
                    logger.warning("AUDIT-051 guard failed (YAML/init error): %s — preserving previous strategies_enabled", _e)
                    _synced_enabled = None  # prevent raw YAML override; keep previous oracle.json state
                # Update both state/oracle.json AND main oracle.json
                for oracle_path in [
                    os.path.join(BASE_DIR, ".aether", "state", "oracle.json"),
                    os.path.join(BASE_DIR, ".aether", "oracle.json"),
                ]:
                    if os.path.exists(oracle_path):
                        with open(oracle_path, "r") as f:
                            oracle = json.load(f)
                    else:
                        oracle = {}
                    oracle["last_pipeline"] = _now_iso
                    oracle["data_fresh"] = True
                    oracle["last_klines_ts"] = _last_klines_ts
                    oracle["klines_count"] = _klines_count
                    oracle["pipeline_pid"] = _my_pid
                    if _data_ext_pid:
                        oracle["data_ext_pid"] = _data_ext_pid
                    # AUDIT-092: purge stale duplicate field (klines_total regressed from AUDIT-090)
                    oracle.pop("klines_total", None)
                    oracle["_updated_at"] = _now_iso
                    if _data_stats:
                        oracle["data_stats"] = _data_stats
                    if _synced_enabled is not None:
                        oracle["strategies_enabled"] = _synced_enabled
                        oracle["strategies_disabled"] = _synced_disabled
                        oracle["strategies_live"] = _synced_live if _synced_live is not None else _synced_enabled
                        oracle["strategies_paper"] = _synced_paper if _synced_paper is not None else []
                    # Always sync _cross_file_warnings — clear stale warnings when empty
                    oracle["_cross_file_warnings"] = _cross_file_warnings
                    with open(oracle_path, "w") as f:
                        json.dump(oracle, f, indent=2, ensure_ascii=False)
            except Exception:
                pass  # best-effort, don't block pipeline tick

            if _cross_file_warnings:
                # Rate-limit: log at most once per 30 minutes unless warnings change
                _now_ts = time.time()
                if _cross_file_warnings != _prev_cross_warnings or (_now_ts - _last_cross_warning_ts) > 1800:
                    for _w in _cross_file_warnings:
                        logger.warning("AUDIT-051 guard: %s", _w)
                    _prev_cross_warnings = list(_cross_file_warnings)
                    _last_cross_warning_ts = _now_ts
            elif _prev_cross_warnings:
                logger.info("AUDIT-051 guard: all PAPER violations cleared")
                _prev_cross_warnings = None
                _last_cross_warning_ts = 0
            if errors:
                logger.warning("Tick: %s — %d feed(s) failed", stats, len(errors))
            else:
                logger.info("Tick: %s", stats)
        except Exception as e:
            logger.error("Pipeline error: %s", e)

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run()
