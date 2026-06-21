#!/usr/bin/env python3
"""Aether 数据管道 — 后台自动运行，无需专员干预"""
import sys, os, time, json, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DATA] %(message)s")
logger = logging.getLogger("pipeline")

SYMBOLS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAMES = ["15m", "1h"]
INTERVAL = 300  # 5 minutes

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state", "pipeline.json")

def run():
    from data.collector import BinanceDataCollector
    from data.storage import MarketStorage
    from config.settings import get_config

    cfg = get_config()
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
    storage = MarketStorage()

    logger.info("Data pipeline started — %s %s every %ds", SYMBOLS, TIMEFRAMES, INTERVAL)

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

            if errors:
                logger.warning("Tick: %s — %d feed(s) failed", stats, len(errors))
            else:
                logger.info("Tick: %s", stats)
        except Exception as e:
            logger.error("Pipeline error: %s", e)

        time.sleep(INTERVAL)

if __name__ == "__main__":
    run()
