"""
Oracle Heartbeat — Aether 数据脉搏
每15分钟心跳: 拉取 BTC+ETH 1h K线 → 存DB → 写 oracle.json → 追加 bulletin.md
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone

import pandas as pd

# Inject project root into path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from data.collector import BinanceDataCollector
from data.storage import MarketStorage


AETHER_DIR = os.path.join(PROJECT_ROOT, ".aether")
ORACLE_JSON = os.path.join(AETHER_DIR, "oracle.json")
BULLETIN_MD = os.path.join(AETHER_DIR, "bulletin.md")

SYMBOLS = ["BTC/USDT", "ETH/USDT"]
TIMEFRAME = "1h"
LOOKBACK = 200  # bars to fetch per run


def _green_heart() -> str:
    """Return a Markdown-formatted GREEN Oracle heartbeat."""
    return "### {ts} — 🟢 Oracle 心跳 — {btc} | {eth} | K线({btc_k}/{eth_k}) {extra}"


def _yellow_heart() -> str:
    """Return a Markdown-formatted YELLOW Oracle heartbeat (anomaly)."""
    return "### {ts} — 🟡 Oracle 心跳 ⚠️ — {btc} | {eth} | K线({btc_k}/{eth_k}) {extra}"


def _format_price(val: float, null_val: str = "N/A") -> str:
    """Format price; return null_val if val is None/NaN."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return null_val
    # Format: 63,995.7 style
    if val >= 1:
        return f"{val:,.1f}"
    return f"{val:.4f}"


def main():
    errors = []
    price_map = {}
    count_map = {}

    # --- 1. Connect to data layer ---
    collector = BinanceDataCollector()
    storage = MarketStorage()

    # --- 2. Fetch klines for each symbol ---
    for sym in SYMBOLS:
        try:
            df = collector.fetch_current_klines(
                symbol=sym,
                timeframe=TIMEFRAME,
                lookback_bars=LOOKBACK,
            )
        except Exception as e:
            errors.append(f"{sym}: {e}")
            price_map[sym] = None
            count_map[sym] = 0
            continue

        if df.empty:
            errors.append(f"{sym}: No data returned")
            price_map[sym] = None
            count_map[sym] = 0
            continue

        # Latest close is our current "price"
        try:
            price_map[sym] = float(df["close"].iloc[-1])
        except Exception:
            price_map[sym] = None

        count_map[sym] = len(df)

        # --- 3. Save to DB ---
        try:
            storage.save_klines(df, symbol=sym, timeframe=TIMEFRAME)
        except Exception as e:
            errors.append(f"{sym}/DB: {e}")

    # --- 4. DB stats ---
    try:
        db_stats = storage.get_db_stats()
    except Exception as e:
        db_stats = {"db_size_bytes": 0, "db_size_mb": 0, "tables": {}}
        errors.append(f"DB stats: {e}")

    # --- 5. Build oracle.json ---
    oracle = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "heartbeat": {
            "name": "Oracle",
            "role": "Aether 数据脉搏",
            "frequency": "15min",
        },
        "prices": {
            "BTC/USDT": price_map.get("BTC/USDT"),
            "ETH/USDT": price_map.get("ETH/USDT"),
        },
        "kline_counts": {
            "BTC/USDT": count_map.get("BTC/USDT", 0),
            "ETH/USDT": count_map.get("ETH/USDT", 0),
        },
        "db_stats": db_stats,
        "errors": errors if errors else None,
    }

    os.makedirs(AETHER_DIR, exist_ok=True)
    with open(ORACLE_JSON, "w") as f:
        json.dump(oracle, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # --- 6. Build bulletin entry ---
    ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M")

    btc_price = _format_price(price_map.get("BTC/USDT"))
    eth_price = _format_price(price_map.get("ETH/USDT"))
    btc_k = count_map.get("BTC/USDT", 0)
    eth_k = count_map.get("ETH/USDT", 0)

    extra = ""
    if errors:
        error_summary = "; ".join(errors[:3])  # cap at 3
        extra = f"⚠️ {error_summary}"
        template = _yellow_heart()
    else:
        template = _green_heart()

    line = template.format(
        ts=ts,
        btc=f"BTC={btc_price}",
        eth=f"ETH={eth_price}",
        btc_k=btc_k,
        eth_k=eth_k,
        extra=extra,
    )

    # Build full bulletin entry with separator
    entry = f"\n---\n{line}\n"

    with open(BULLETIN_MD, "a") as f:
        f.write(entry)

    # --- 7. Print summary to stdout ---
    status = "YELLOW ⚠️" if errors else "GREEN"
    print(f"Oracle heartbeat: {status}")
    print(f"  BTC: {btc_price}  |  ETH: {eth_price}")
    print(f"  Klines: {btc_k}/{eth_k}")
    if errors:
        for err in errors:
            print(f"  ⚠️ {err}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        # Emergency bulletin entry
        ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
        emergency = (
            f"\n---\n### {ts} — 🔴 Oracle CRASH ⚠️ — {exc}\n"
        )
        os.makedirs(AETHER_DIR, exist_ok=True)
        with open(BULLETIN_MD, "a") as f:
            f.write(emergency)
        sys.exit(1)
