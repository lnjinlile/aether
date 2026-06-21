#!/usr/bin/env python3
"""Oracle pulse — fetch 1h klines for BTC+ETH, store DB, write oracle.json, append bulletin."""

import sys, os, json, time
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.collector import BinanceDataCollector
from data.storage import MarketStorage

SYMBOLS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAME = "1h"
LOOKBACK_BARS = 200
ORACLE_PATH = os.path.join(os.path.dirname(__file__), "oracle.json")
BULLETIN_PATH = os.path.join(os.path.dirname(__file__), "bulletin.md")

def main():
    collector = BinanceDataCollector()
    storage = MarketStorage()

    prices = {}
    kline_counts = {}
    errors = []

    for sym in SYMBOLS:
        try:
            df = collector.fetch_current_klines(
                symbol=sym, timeframe=TIMEFRAME, lookback_bars=LOOKBACK_BARS
            )
            if df.empty:
                errors.append(f"{sym}: empty response")
                prices[sym] = None
                kline_counts[sym] = 0
                continue

            storage.save_klines(df, symbol=sym, timeframe=TIMEFRAME)

            # Get latest close
            latest = df.iloc[-1]
            prices[sym] = float(latest["close"])
            kline_counts[sym] = len(df)
            print(f"  {sym}: {len(df)} bars saved, close={prices[sym]}")

        except Exception as e:
            errors.append(f"{sym}: {e}")
            prices[sym] = None
            kline_counts[sym] = 0
            print(f"  {sym}: ERROR — {e}")

    # DB stats
    db_stats = storage.get_db_stats()

    # Build oracle.json
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    oracle = {
        "timestamp": ts,
        "heartbeat": {
            "name": "Oracle",
            "role": "Aether 数据脉搏",
            "frequency": "15min",
        },
        "prices": {
            "BTC/USDT": prices.get("BTC/USDT"),
            "ETH/USDT": prices.get("ETH/USDT"),
        },
        "kline_counts": {
            "BTC/USDT": kline_counts.get("BTC/USDT", 0),
            "ETH/USDT": kline_counts.get("ETH/USDT", 0),
        },
        "db_stats": db_stats,
        "errors": errors if errors else None,
    }

    with open(ORACLE_PATH, "w") as f:
        json.dump(oracle, f, indent=2, ensure_ascii=False)
    print(f"  oracle.json written ({os.path.getsize(ORACLE_PATH)} bytes)")

    # Build bulletin line
    btc_str = f"{prices['BTC/USDT']:,.1f}" if prices["BTC/USDT"] else "N/A"
    eth_str = f"{prices['ETH/USDT']:,.1f}" if prices["ETH/USDT"] else "N/A"
    btc_bars = kline_counts["BTC/USDT"]
    eth_bars = kline_counts["ETH/USDT"]

    date_str = now_utc.strftime("%m-%d %H:%M")
    line = f"### {date_str} — 🔵 Oracle 心跳 — BTC={btc_str} | ETH={eth_str} | K线({btc_bars}/{eth_bars})"

    if errors:
        warnings = "; ".join(errors)
        line += f"  ⚠️ {warnings}"

    line += "\n"

    with open(BULLETIN_PATH, "a") as f:
        f.write("\n---\n")
        f.write(line)

    print(f"  Bulletin appended: {line.strip()}")

    # Report
    print(f"\nOracle pulse complete at {ts}")
    if errors:
        print(f"ERRORS: {errors}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
