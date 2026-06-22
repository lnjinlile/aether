#!/usr/bin/env python3
"""爬取 Coinglass / CoinGecko 公开链上数据"""
import subprocess, json, sqlite3, time, os, sys
from datetime import datetime, timezone

PROXY = "socks5h://127.0.0.1:7890"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
INTERVAL = 300  # 5 minutes

def curl(url, timeout=15):
    r = subprocess.run(["curl", "-s", "--max-time", str(timeout), "--proxy", PROXY,
                        "-H", "User-Agent: Mozilla/5.0", url],
                       capture_output=True, text=True)
    if r.returncode != 0: return None
    try: return json.loads(r.stdout)
    except: return None

def collect_coinglass_oi():
    """Coinglass open interest — use public page API"""
    # Try their public snapshot endpoint
    url = "https://open-api.coinglass.com/public/v2/open_interest?symbol=BTC&interval=h1&limit=500"
    data = curl(url)
    if not data or data.get("code") != "0":
        return 0
    
    records = data.get("data", [])
    conn = sqlite3.connect(DB)
    saved = 0
    for r in records:
        try:
            ts = r.get("t", 0) // 1000 if r.get("t", 0) > 1e12 else r.get("t", 0)
            conn.execute(
                "INSERT OR IGNORE INTO open_interest(symbol,timestamp,open_interest) VALUES(?,?,?)",
                ("BTCUSDT", ts, float(r.get("v", 0)))
            )
            saved += 1
        except: pass
    conn.commit()
    conn.close()
    return saved

def collect_coinglass_funding():
    """Coinglass funding rate history"""
    url = "https://open-api.coinglass.com/public/v2/funding_rate?symbol=BTC&interval=h8&limit=500"
    data = curl(url)
    if not data or data.get("code") != "0":
        return 0
    
    records = data.get("data", [])
    conn = sqlite3.connect(DB)
    saved = 0
    for r in records:
        try:
            ts = r.get("t", 0) // 1000 if r.get("t", 0) > 1e12 else r.get("t", 0)
            conn.execute(
                "INSERT OR IGNORE INTO funding_rates(symbol,funding_time,funding_rate,mark_price) VALUES(?,?,?,?)",
                ("BTCUSDT", ts, float(r.get("v", 0)), 0)
            )
            saved += 1
        except: pass
    conn.commit()
    conn.close()
    return saved

def collect_binance_oi_live():
    """Binance live OI via proxy (works!)"""
    url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
    data = curl(url)
    if not data: return 0
    oi = float(data.get("openInterest", 0))
    ts = time.time()
    
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT OR IGNORE INTO open_interest(symbol,timestamp,open_interest) VALUES(?,?,?)",
        ("BTCUSDT", ts, oi)
    )
    conn.commit()
    conn.close()
    return 1

def main():
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS open_interest (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, timestamp REAL, open_interest REAL, UNIQUE(symbol,timestamp))")
    conn.commit()
    conn.close()
    
    print(f"On-chain scraper started — interval={INTERVAL}s")
    while True:
        try:
            oi1 = collect_binance_oi_live()
            oi2 = collect_coinglass_oi()
            fr = collect_coinglass_funding()
            total = oi1 + oi2
            print(f"[{datetime.now().strftime('%H:%M:%S')}] OI: Binance={oi1} Coinglass={oi2} FR={fr}")
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
