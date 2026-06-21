#!/usr/bin/env python3
"""扩展数据采集: 订单簿 + 资金费率 + 持仓量 + 多空比"""
import os, sys, json, sqlite3, time, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DATA+] %(message)s")
logger = logging.getLogger("data_ext")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
INTERVAL = 300

def init_db():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp REAL NOT NULL,
            best_bid REAL, best_ask REAL,
            bid_vol_5 REAL, ask_vol_5 REAL,
            spread_pct REAL,
            imbalance REAL,
            created_at REAL DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS funding_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            funding_time REAL NOT NULL,
            funding_rate REAL NOT NULL,
            mark_price REAL,
            UNIQUE(symbol, funding_time)
        );
        CREATE TABLE IF NOT EXISTS open_interest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp REAL NOT NULL,
            open_interest REAL NOT NULL,
            UNIQUE(symbol, timestamp)
        );
        CREATE INDEX IF NOT EXISTS idx_ob_symbol_time ON orderbook_snapshots(symbol, timestamp);
        CREATE INDEX IF NOT EXISTS idx_fr_symbol_time ON funding_rates(symbol, funding_time);
        CREATE INDEX IF NOT EXISTS idx_oi_symbol_time ON open_interest(symbol, timestamp);
    """)
    conn.commit()
    conn.close()

def fetch_and_store():
    import ccxt
    from config.settings import get_config
    cfg = get_config()
    
    exchange = ccxt.binanceusdm({
        "apiKey": cfg.api_key, "secret": cfg.api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "future", "fetchCurrencies": False, "fetchLeverageBrackets": False},
    })
    # Testnet URLs
    base = "https://testnet.binancefuture.com" if cfg.testnet else "https://fapi.binance.com"
    api = exchange.urls.get("api", {})
    for key in list(api.keys()):
        if key.startswith("fapi"): api[key] = base + "/fapi/v1"
    exchange.fetch_leverage_brackets = lambda *a, **kw: {}

    conn = sqlite3.connect(DB)
    now_ts = time.time()

    for sym in ["BTC/USDT", "ETH/USDT"]:
        bin_sym = sym.replace("/", "")
        try:
            # === 1. Order Book ===
            ob = exchange.fetch_order_book(sym, 20)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else 0
            bid_vol_5 = sum(b[1] for b in bids[:5])
            ask_vol_5 = sum(a[1] for a in asks[:5])
            spread = (best_ask - best_bid) / best_bid * 100 if best_bid else 0
            imbalance = bid_vol_5 / (bid_vol_5 + ask_vol_5) if (bid_vol_5 + ask_vol_5) > 0 else 0.5

            conn.execute(
                "INSERT INTO orderbook_snapshots(symbol,timestamp,best_bid,best_ask,bid_vol_5,ask_vol_5,spread_pct,imbalance) VALUES(?,?,?,?,?,?,?,?)",
                (bin_sym, now_ts, best_bid, best_ask, bid_vol_5, ask_vol_5, spread, imbalance)
            )
            logger.info("%s OB: bid=%.1f ask=%.1f spread=%.4f%% imb=%.3f", sym, best_bid, best_ask, spread, imbalance)

            # === 2. Funding Rate ===
            fr_raw = exchange.fetch_funding_rate_history(sym, limit=10)
            for fr in fr_raw:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO funding_rates(symbol,funding_time,funding_rate,mark_price) VALUES(?,?,?,?)",
                        (bin_sym, fr["timestamp"], fr["fundingRate"], fr.get("markPrice", 0))
                    )
                except: pass
            if fr_raw:
                logger.info("%s FR: latest=%.6f%%", sym, fr_raw[-1]["fundingRate"] * 100)

            # === 3. Open Interest ===
            oi = exchange.fetch_open_interest(sym)
            conn.execute(
                "INSERT OR IGNORE INTO open_interest(symbol,timestamp,open_interest) VALUES(?,?,?)",
                (bin_sym, now_ts, oi.get("openInterestAmount", 0))
            )
            logger.info("%s OI: %.0f", sym, oi.get("openInterestAmount", 0))

        except Exception as e:
            logger.error("%s: %s", sym, str(e)[:80])

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    logger.info("Extended data collector started — interval %ds", INTERVAL)
    while True:
        try:
            fetch_and_store()
            logger.info("Cycle complete")
        except Exception as e:
            logger.error("Cycle error: %s", e)
        time.sleep(INTERVAL)
