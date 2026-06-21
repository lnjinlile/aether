"""
Oracle heartbeat — 每15分钟拉取 BTC+ETH 1h K线，存DB，写状态，发公告。
"""
import sys
import os
import json
from datetime import datetime, timezone, timedelta

# Project root
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from data.collector import BinanceDataCollector
from data.storage import MarketStorage

# Import shared_state from sibling .aether dir
_sys_path = os.path.dirname(os.path.abspath(__file__))
if _sys_path not in sys.path:
    sys.path.insert(0, _sys_path)
# Use direct file path since it's not a package when run as script
import importlib.util as _iu
_ss_spec = _iu.spec_from_file_location("shared_state", os.path.join(_sys_path, "shared_state.py"))
shared_state = _iu.module_from_spec(_ss_spec)
_ss_spec.loader.exec_module(shared_state)
write_state = shared_state.write_state
post_bulletin = shared_state.post_bulletin


def heart_emoji(btc_change_pct, eth_change_pct):
    """决定心跳 emoji: 🟢正常 / 🟡小幅波动 / 🔴异常"""
    max_change = max(abs(btc_change_pct), abs(eth_change_pct))
    if max_change > 5:
        return "🔴"
    elif max_change > 2:
        return "🟡"
    else:
        return "🟢"


def run():
    collector = BinanceDataCollector()
    storage = MarketStorage()

    results = []
    anomalies = []

    for symbol in ["BTC/USDT", "ETH/USDT"]:
        try:
            df = collector.fetch_current_klines(symbol=symbol, timeframe="1h", lookback_bars=200)

            if df.empty:
                anomalies.append(f"{symbol}: 空数据")
                results.append({"symbol": symbol, "price": None, "count": 0, "error": "空数据"})
                continue

            latest = df.iloc[-1]
            price = float(latest["close"])
            count = len(df)

            # Save to DB
            storage.save_klines(df, symbol=symbol, timeframe="1h")

            # Detect anomalies: check 1h candle change vs previous
            if len(df) >= 2:
                prev_close = float(df.iloc[-2]["close"])
                change_pct = (price - prev_close) / prev_close * 100
                results.append({
                    "symbol": symbol,
                    "price": round(price, 2),
                    "count": count,
                    "change_1h_pct": round(change_pct, 4),
                })
            else:
                results.append({
                    "symbol": symbol,
                    "price": round(price, 2),
                    "count": count,
                })

        except Exception as e:
            anomalies.append(f"{symbol}: {str(e)[:120]}")
            results.append({"symbol": symbol, "price": None, "count": 0, "error": str(e)[:120]})

    # Pull out BTC/ETH specifics
    btc = next((r for r in results if r["symbol"] == "BTC/USDT"), {})
    eth = next((r for r in results if r["symbol"] == "ETH/USDT"), {})

    btc_price = btc.get("price")
    eth_price = eth.get("price")
    btc_count = btc.get("count", 0)
    eth_count = eth.get("count", 0)

    data_ok = btc_price is not None and eth_price is not None

    # Price change detection (using previous oracle state)
    import json as _json
    prev_path = os.path.join(os.path.dirname(__file__), "state", "oracle.json")
    prev_btc, prev_eth = None, None
    if os.path.exists(prev_path):
        try:
            with open(prev_path) as f:
                prev = _json.load(f)
            prev_btc = prev.get("btc_price")
            prev_eth = prev.get("eth_price")
        except Exception:
            pass

    btc_change = round((btc_price - prev_btc) / prev_btc * 100, 4) if btc_price and prev_btc else 0
    eth_change = round((eth_price - prev_eth) / prev_eth * 100, 4) if eth_price and prev_eth else 0

    # Write oracle.json state
    state = {
        "status": "ok" if data_ok else "degraded",
        "btc_price": btc_price,
        "eth_price": eth_price,
        "btc_klines": btc_count,
        "eth_klines": eth_count,
        "data_fresh": data_ok,
    }
    if btc_change:
        state["btc_change_pct"] = btc_change
    if eth_change:
        state["eth_change_pct"] = eth_change
    if anomalies:
        state["anomalies"] = anomalies
    elif data_ok:
        # Clear stale anomaly markers from previous runs
        state["anomalies"] = []

    write_state("oracle", state)

    # Build bulletin entry
    emoji = heart_emoji(btc_change, eth_change)
    timestamp = datetime.now(timezone.utc)

    btc_str = f"BTC={btc_price:,.1f}" if btc_price else "BTC=N/A"
    eth_str = f"ETH={eth_price:,.1f}" if eth_price else "ETH=N/A"

    if btc_change and eth_change:
        change_str = f"Δ BTC{btc_change:+.2f}% ETH{eth_change:+.2f}%"
    else:
        change_str = ""

    entry = f"{emoji} Oracle 心跳 — {btc_str} | {eth_str} | K线({btc_count}/{eth_count}) {change_str}"

    if anomalies:
        entry += f" ⚠️ {'; '.join(anomalies)}"

    post_bulletin(entry)

    print(entry)
    print(json.dumps(state, indent=2, ensure_ascii=False, default=str))

    # Also print DB stats
    stats = storage.get_db_stats()
    print(f"\nDB: {stats['db_size_mb']}MB, rows: {stats['tables']}")


if __name__ == "__main__":
    run()
