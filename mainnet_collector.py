#!/usr/bin/env python3
"""Binance主网历史数据采集(通过加拿大节点代理)"""
import sqlite3, json, time, subprocess, os, sys
from datetime import datetime, timezone

PROXY = "socks5h://127.0.0.1:7890"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
BASE = "https://fapi.binance.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
TIMEFRAMES = {"15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}

def curl(url, timeout=30):
    result = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), "--proxy", PROXY, url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  curl error: {result.stderr[:100]}"); return None
    try: return json.loads(result.stdout)
    except: print(f"  JSON parse fail: {result.stdout[:80]}"); return None

def backfill_klines():
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    
    for sym in SYMBOLS:
        for tf, expected in TIMEFRAMES.items():
            # Check existing
            cnt = conn.execute(
                "SELECT COUNT(*) FROM klines WHERE symbol=? AND timeframe=?",
                (sym, tf)
            ).fetchone()[0]
            if cnt >= expected * 0.9:
                print(f"  {sym} {tf}: {cnt} bars ✓ (skip)")
                continue

            print(f"  {sym} {tf}: fetching...")
            interval = tf.replace('m','').replace('h','').replace('d','')
            if 'm' in tf: interval = tf
            all_bars = []
            end_time = int(time.time() * 1000)

            for page in range(40):
                url = f"{BASE}/fapi/v1/klines?symbol={sym}&interval={tf}&limit=1500"
                if all_bars:
                    url += f"&endTime={end_time}"
                data = curl(url)
                if not data or not isinstance(data, list) or len(data) == 0:
                    break
                all_bars.extend(data)
                end_time = data[0][0] - 1
                if len(data) < 1500: break
                if len(all_bars) >= expected: break

            saved = 0
            for bar in all_bars:
                try:
                    conn.execute("""
                        INSERT OR IGNORE INTO klines(symbol,timeframe,open_time,open,high,low,close,volume)
                        VALUES(?,?,?,?,?,?,?,?)
                    """, (sym, tf, bar[0]/1000, float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])))
                    saved += 1
                except: pass
            conn.commit()
            print(f"  {sym} {tf}: {saved} saved (total {cnt+saved})")

    conn.close()
    print("✅ Klines done")

def backfill_oi():
    """Open Interest history (public endpoint, no auth needed!)"""
    conn = sqlite3.connect(DB)
    for sym in SYMBOLS:
        all_data = []
        end_time = int(time.time() * 1000)
        for _ in range(20):
            url = f"{BASE}/fapi/v1/openInterestHist?symbol={sym}&period=1h&limit=500"
            if all_data:
                url += f"&endTime={end_time}"
            data = curl(url)
            if not data or not isinstance(data, list): break
            all_data.extend(data)
            end_time = data[0]["timestamp"] - 3600000
            if len(data) < 500: break

        saved = 0
        for d in all_data:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO open_interest(symbol,timestamp,open_interest) VALUES(?,?,?)",
                    (sym, d["timestamp"]/1000, float(d["sumOpenInterest"]))
                ); saved += 1
            except: pass
        conn.commit()
        print(f"  {sym} OI: {saved} records")
    conn.close()
    print("✅ OI done")

def backfill_funding():
    conn = sqlite3.connect(DB)
    for sym in SYMBOLS:
        url = f"{BASE}/fapi/v1/fundingRate?symbol={sym}&limit=1000"
        data = curl(url, timeout=15)
        if not data or not isinstance(data, list): continue
        saved = 0
        for d in data:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO funding_rates(symbol,funding_time,funding_rate,mark_price) VALUES(?,?,?,?)",
                    (sym, d["fundingTime"]/1000, float(d["fundingRate"]), float(d.get("markPrice",0)))
                ); saved += 1
            except: pass
        conn.commit()
        print(f"  {sym} Funding: {saved} records")
    conn.close()
    print("✅ Funding done")

def backfill_ls_ratio():
    """Long/Short ratio (public endpoint)"""
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS long_short_ratio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, timestamp REAL,
            long_ratio REAL, short_ratio REAL,
            UNIQUE(symbol, timestamp)
        )
    """)
    conn.commit()
    for sym in SYMBOLS:
        all_data = []
        end_time = int(time.time() * 1000)
        for _ in range(15):
            url = f"{BASE}/fapi/v1/globalLongShortAccountRatio?symbol={sym}&period=1h&limit=500"
            if all_data: url += f"&endTime={end_time}"
            data = curl(url)
            if not data or not isinstance(data, list): break
            all_data.extend(data)
            end_time = data[0]["timestamp"] - 3600000
            if len(data) < 500: break
        saved = 0
        for d in all_data:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO long_short_ratio(symbol,timestamp,long_ratio,short_ratio) VALUES(?,?,?,?)",
                    (sym, d["timestamp"]/1000, float(d["longAccount"]), float(d["shortAccount"]))
                ); saved += 1
            except: pass
        conn.commit()
        print(f"  {sym} L/S: {saved} records")
    conn.close()
    print("✅ L/S ratio done")

if __name__ == "__main__":
    print("=== Pulling Binance MAINNET data via 🇨🇦 proxy ===")
    print("KLines..."); backfill_klines()
    print("Open Interest..."); backfill_oi()
    print("Funding Rates..."); backfill_funding()
    print("Long/Short Ratio..."); backfill_ls_ratio()
    print("=== All done ===")
