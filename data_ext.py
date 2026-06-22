#!/usr/bin/env python3
"""扩展数据采集: 订单簿 + 资金费率 + 持仓量 + 多空比 + 主动买卖量"""
import os, sys, json, sqlite3, time, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [DATA+] %(message)s")
logger = logging.getLogger("data_ext")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
INTERVAL = 300
FUNDING_INTERVAL = 3600  # 1h — funding rates change every 8h, no need for 5min polling

def init_db():
    from data.db import get_market_db
    conn = get_market_db()
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
        CREATE TABLE IF NOT EXISTS long_short_ratio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp REAL NOT NULL,
            long_ratio REAL NOT NULL,
            short_ratio REAL NOT NULL,
            UNIQUE(symbol, timestamp)
        );
        CREATE TABLE IF NOT EXISTS taker_volume (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timestamp REAL NOT NULL,
            taker_buy_ratio REAL NOT NULL,
            taker_buy_vol REAL,
            taker_sell_vol REAL,
            UNIQUE(symbol, timestamp)
        );
        CREATE INDEX IF NOT EXISTS idx_ob_symbol_time ON orderbook_snapshots(symbol, timestamp);
        CREATE INDEX IF NOT EXISTS idx_fr_symbol_time ON funding_rates(symbol, funding_time);
        CREATE INDEX IF NOT EXISTS idx_oi_symbol_time ON open_interest(symbol, timestamp);
        CREATE INDEX IF NOT EXISTS idx_lsr_symbol_time ON long_short_ratio(symbol, timestamp);
        CREATE INDEX IF NOT EXISTS idx_tv_symbol_time ON taker_volume(symbol, timestamp);
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

    from data.db import get_market_db
    conn = get_market_db()
    now_ts = time.time()
    fetch_funding = (now_ts - _last_funding_fetch[0]) >= FUNDING_INTERVAL

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

            # === 2. Funding Rate (every 1h, not every 5min tick) ===
            if fetch_funding:
                fr_raw = exchange.fetch_funding_rate_history(sym, limit=10)
                for fr in fr_raw:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO funding_rates(symbol,funding_time,funding_rate,mark_price) VALUES(?,?,?,?)",
                            (bin_sym, fr["timestamp"], fr["fundingRate"], fr.get("markPrice", 0))
                        )
                    except Exception:
                        logger.debug("%s FR insert skipped (duplicate or schema mismatch)", sym)
                if fr_raw:
                    logger.info("%s FR: latest=%.6f%%", sym, fr_raw[-1]["fundingRate"] * 100)

            # === 3. Open Interest ===
            oi = exchange.fetch_open_interest(sym)
            conn.execute(
                "INSERT OR IGNORE INTO open_interest(symbol,timestamp,open_interest) VALUES(?,?,?)",
                (bin_sym, now_ts, oi.get("openInterestAmount", 0))
            )
            logger.info("%s OI: %.0f", sym, oi.get("openInterestAmount", 0))

            # === 4. Long/Short Ratio (disabled on testnet — endpoint unavailable) ===
            if not cfg.testnet:
                try:
                    lsr = exchange.fetch_long_short_ratio(sym, params={"period": "5m"})
                    lsr_log = 0.5
                    if isinstance(lsr, list):
                        for entry in lsr[-3:]:  # store last 3 periods
                            lsr_ts = entry.get("timestamp", now_ts * 1000) / 1000.0
                            conn.execute(
                                "INSERT OR IGNORE INTO long_short_ratio(symbol,timestamp,long_ratio,short_ratio) VALUES(?,?,?,?)",
                                (bin_sym, lsr_ts, entry.get("longShortRatio", 0.5), 1.0 - entry.get("longShortRatio", 0.5))
                            )
                        lsr_log = lsr[-1].get("longShortRatio", 0.5) if lsr else 0.5
                    elif isinstance(lsr, dict):
                        lsr_val = lsr.get("longShortRatio") or lsr.get("longAccount") or 0.5
                        if isinstance(lsr_val, (int, float)):
                            conn.execute(
                                "INSERT OR IGNORE INTO long_short_ratio(symbol,timestamp,long_ratio,short_ratio) VALUES(?,?,?,?)",
                                (bin_sym, now_ts, lsr_val, 1.0 - lsr_val)
                            )
                        lsr_log = lsr_val if isinstance(lsr_val, (int, float)) else 0.5
                    logger.info("%s L/S: %.3f/%.3f", sym, lsr_log, 1.0 - lsr_log)
                except Exception as e:
                    err_msg = str(e)
                    if "not supported" in err_msg.lower():
                        logger.debug("%s L/S ratio not supported (testnet limitation)", sym)
                    else:
                        logger.warning("%s L/S ratio fetch failed: %s", sym, err_msg[:80])

            # === 5. Taker Buy/Sell Volume (disabled on testnet — endpoint unavailable) ===
            if not cfg.testnet:
                try:
                    ratio = 0.5
                    taker = exchange.fetch_taker_volume(sym) if hasattr(exchange, "fetch_taker_volume") else None
                    if taker is None:
                        # Fallback: use Binance fapiDataGetTakerlongshortRatio
                        raw = exchange.fapiDataGetTakerlongshortRatio({"symbol": bin_sym, "period": "5m", "limit": 3})
                        for entry in (raw if isinstance(raw, list) else [raw]):
                            tv_ts = entry.get("timestamp", now_ts * 1000) / 1000.0
                            buy_vol = float(entry.get("buyVol", 0))
                            sell_vol = float(entry.get("sellVol", 0))
                            ratio = buy_vol / (buy_vol + sell_vol) if (buy_vol + sell_vol) > 0 else 0.5
                            conn.execute(
                                "INSERT OR IGNORE INTO taker_volume(symbol,timestamp,taker_buy_ratio,taker_buy_vol,taker_sell_vol) VALUES(?,?,?,?,?)",
                                (bin_sym, tv_ts, ratio, buy_vol, sell_vol)
                            )
                    logger.info("%s Taker: buy_ratio=%.3f", sym, ratio)
                except Exception as e:
                    err_msg = str(e)
                    if "invalid" in err_msg.lower() or "not supported" in err_msg.lower():
                        logger.debug("%s Taker volume not supported (testnet limitation)", sym)
                    else:
                        logger.warning("%s Taker volume fetch failed: %s", sym, err_msg[:80])

        except Exception as e:
            logger.error("%s: %s", sym, str(e)[:80])

    conn.commit()
    conn.close()
    if fetch_funding:
        _last_funding_fetch[0] = time.time()

if __name__ == "__main__":
    _last_funding_fetch = [0]  # mutable singleton to track across fetch_and_store() calls
    init_db()
    logger.info("Extended data collector started — interval %ds", INTERVAL)
    while True:
        try:
            fetch_and_store()
            logger.info("Cycle complete")
        except Exception as e:
            logger.error("Cycle error: %s", e)
        time.sleep(INTERVAL)
