#!/usr/bin/env python3
"""Aether 数据管道 — 后台自动运行，无需专员干预"""
import sys, os, time, json, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DATA] %(message)s")
logger = logging.getLogger("pipeline")

SYMBOLS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAMES = ["15m", "1h", "4h", "1d"]
INTERVAL = 300  # 5 minutes
HISTORICAL_DAYS = 365
# FUNDING_INTERVAL removed — funding rates now collected exclusively by data_ext.py

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state", "pipeline.json")


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
                    storage._get_conn().execute("ANALYZE")
                    logger.info("ANALYZE complete — query planner stats refreshed")
                except Exception:
                    pass

            # ── Periodic data quality check (every hour) ──
            if int(time.time()) % 3600 < INTERVAL:
                try:
                    from data.quality import DataQualityCheck
                    qc = DataQualityCheck()
                    results = qc.run_all(SYMBOLS, TIMEFRAMES)
                    if results["health"] != "ok":
                        logger.warning("Quality check DEGRADED: %d issues", len(results["issues"]))
                        for iss in results["issues"][:3]:
                            logger.warning("  [%s] %s", iss["type"], iss["msg"])
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
                # Compute latest kline timestamp + total count from DB
                _conn = storage._get_conn()
                try:
                    _row = _conn.execute(
                        "SELECT MAX(open_time), COUNT(*) FROM klines"
                    ).fetchone()
                    _last_klines_ts = _row[0]
                    _klines_count = _row[1]
                finally:
                    _conn.close()
                _my_pid = os.getpid()  # actual python process PID, not the bash wrapper
                # Update both state/oracle.json AND main oracle.json
                for oracle_path in [
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state", "oracle.json"),
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "oracle.json"),
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
                    oracle["_updated_at"] = _now_iso
                    with open(oracle_path, "w") as f:
                        json.dump(oracle, f, indent=2, ensure_ascii=False)
            except Exception:
                pass  # best-effort, don't block pipeline tick

            if errors:
                logger.warning("Tick: %s — %d feed(s) failed", stats, len(errors))
            else:
                logger.info("Tick: %s", stats)
        except Exception as e:
            logger.error("Pipeline error: %s", e)

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run()
