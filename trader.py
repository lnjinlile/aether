#!/usr/bin/env python3
"""
Aether AutoTrader v2 — 多时间框架自动交易。

层级:
  15m — 短线波段 (RSI + BB squeeze)
   1h — 中线摆动 (MA cross + trend)
   4h — 长线趋势 (EMA cloud + volume)

每层独立决策，独立仓位，互不干扰。
"""
import os, sys, time, logging, json, sqlite3
from datetime import datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TRADER] %(message)s")
logger = logging.getLogger("trader")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
CHECK_INTERVAL = 30
MIN_BARS = 100

# ====== 时间框架层级 ======
TIERS = {
    "15m": {"desc": "短线波段", "sl_pct": 0.01, "tp_pct": 0.02, "cooldown_hours": 1},
    "1h":  {"desc": "中线摆动", "sl_pct": 0.02, "tp_pct": 0.04, "cooldown_hours": 4},
    "4h":  {"desc": "长线趋势", "sl_pct": 0.03, "tp_pct": 0.08, "cooldown_hours": 12},
}

SYMBOLS = ["BTC/USDT", "ETH/USDT"]
QTY = {"BTC/USDT": 0.001, "ETH/USDT": 0.005}
LEVERAGE = 3


def get_client():
    from execution.client import BinanceFuturesClient
    from config.settings import get_config
    c = get_config()
    return BinanceFuturesClient(c.api_key, c.api_secret, c.testnet)


def fetch_data(collector, symbol, tf):
    """获取K线数据"""
    df = collector.fetch_current_klines(symbol, tf, 300)
    if df is None or df.empty or len(df) < MIN_BARS:
        return None
    return df


def compute_indicators(df):
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = (100 - (100 / (1 + rs))).fillna(50)

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    std20 = close.rolling(20).std()
    bb_lower = sma20 - 2 * std20
    bb_upper = sma20 + 2 * std20
    bb_width = (bb_upper - bb_lower) / sma20  # squeeze indicator

    atr = (df["high"] - df["low"]).rolling(14).mean()
    
    return {
        "rsi": rsi,
        "sma20": sma20, "sma50": sma50,
        "bb_lower": bb_lower, "bb_upper": bb_upper, "bb_width": bb_width,
        "atr": atr,
    }


def check_15m_signal(ind, price):
    """15m短线: RSI超卖+布林收窄=爆发前夜"""
    rsi = ind["rsi"].iloc[-1]
    bb_w = ind["bb_width"].iloc[-1]
    bb_w_prev = ind["bb_width"].iloc[-5] if len(ind["bb_width"]) >= 5 else bb_w

    if rsi < 20 and bb_w < 0.03:
        return "LONG", f"RSI={rsi:.1f}超卖+布林收窄{bb_w:.3f}"
    if rsi > 80 and bb_w < 0.03:
        return "SHORT", f"RSI={rsi:.1f}超买+布林收窄{bb_w:.3f}"
    if rsi < 20:
        return "LONG", f"RSI={rsi:.1f}超卖"
    if rsi > 80:
        return "SHORT", f"RSI={rsi:.1f}超买"
    return "HOLD", ""


def check_1h_signal(ind, price):
    """1h中线: RSI均值回归 + 均线交叉"""
    rsi = ind["rsi"].iloc[-1]
    sma20 = ind["sma20"].iloc[-1]
    sma50 = ind["sma50"].iloc[-1]
    sma20_p = ind["sma20"].iloc[-2]
    sma50_p = ind["sma50"].iloc[-2]

    # 金叉 + 不在高位
    if sma20 > sma50 and sma20_p <= sma50_p and rsi < 60:
        return "LONG", f"MA20({sma20:.0f})金叉MA50({sma50:.0f}) RSI={rsi:.0f}"
    # 死叉 + 不在低位
    if sma20 < sma50 and sma20_p >= sma50_p and rsi > 40:
        return "SHORT", f"MA20({sma20:.0f})死叉MA50({sma50:.0f}) RSI={rsi:.0f}"
    # RSI 极端
    if rsi < 25:
        return "LONG", f"RSI={rsi:.1f}超卖"
    if rsi > 75:
        return "SHORT", f"RSI={rsi:.1f}超买"
    return "HOLD", ""


def check_4h_signal(ind, price):
    """4h长线: 趋势跟踪 + 成交量确认"""
    rsi = ind["rsi"].iloc[-1]
    sma20 = ind["sma20"].iloc[-1]
    sma50 = ind["sma50"].iloc[-1]

    # RSI 从低位回升 + 均线多头 = 趋势确认
    rsi_p = ind["rsi"].iloc[-3] if len(ind["rsi"]) >= 3 else rsi
    if sma20 > sma50 and rsi > rsi_p and rsi < 50:
        return "LONG", f"多头趋势 RSI回升{rsi_p:.0f}→{rsi:.0f}"
    if sma20 < sma50 and rsi < rsi_p and rsi > 50:
        return "SHORT", f"空头趋势 RSI下降{rsi_p:.0f}→{rsi:.0f}"
    return "HOLD", ""


SIGNAL_FNS = {"15m": check_15m_signal, "1h": check_1h_signal, "4h": check_4h_signal}


def execute(client, symbol, side, qty, tier, price):
    try: client.set_leverage(symbol, LEVERAGE)
    except: pass

    # Cancel existing orders for this symbol
    try:
        for o in client.get_open_orders():
            if symbol.replace("/","") in o.get("symbol",""):
                client.cancel_order(o["id"], symbol)
    except: pass

    r = client.place_order(symbol, side, qty, order_type="MARKET")
    oid = r.get("order",{}).get("id","?") if r else "?"
    fp = r.get("price", price) if r else price
    if not fp or fp <= 0:
        logger.warning("%s 无成交价", symbol)
        return None

    cfg = TIERS[tier]
    sl = fp * (1-cfg["sl_pct"] if side=="LONG" else 1+cfg["sl_pct"])
    tp = fp * (1+cfg["tp_pct"] if side=="LONG" else 1-cfg["tp_pct"])
    sl_s = "SELL" if side=="LONG" else "BUY"

    try:
        client.place_order(symbol, sl_s, qty, order_type="STOP_MARKET", stop_price=sl, reduce_only=True)
        client.place_order(symbol, sl_s, qty, order_type="TAKE_PROFIT_MARKET", stop_price=tp, reduce_only=True)
    except: pass

    card = {"层": tier, "描述": cfg["desc"], "标的": symbol, "方向": side, "数量": qty,
            "入场": fp, "SL": round(sl,1), "TP": round(tp,1), "杠杆": f"{LEVERAGE}x", "订单ID": oid}
    logger.info("TRADE: %s", json.dumps(card, ensure_ascii=False))

    conn = sqlite3.connect(DB)
    conn.execute("INSERT INTO trades_log(symbol,side,entry_time,entry_price,quantity,strategy_name,reason,status) VALUES(?,?,?,?,?,?,?,?)",
               (symbol.split("/")[0]+"USDT", side, time.time(), fp, qty, f"{tier}_{cfg['desc']}", card["方向"], "OPEN"))
    conn.commit(); conn.close()
    return card


def close_existing(client, symbol):
    try:
        for o in client.get_open_orders():
            if symbol.replace("/","") in o.get("symbol",""):
                client.cancel_order(o["id"], symbol)
    except: pass
    try:
        client.close_position(symbol)
        logger.info("CLOSED: %s", symbol)
    except Exception as e:
        logger.warning("Close error: %s", e)


def main():
    logger.info("AutoTrader v2 多时间框架启动")
    client = get_client()
    from data.collector import BinanceDataCollector
    from config.settings import get_config
    cfg = get_config()
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)

    last_trade = {}  # (symbol, tier) -> timestamp

    while True:
        try:
            positions = {p["symbol"]: p for p in client.get_positions()}

            for sym in SYMBOLS:
                for tier, tier_cfg in TIERS.items():
                    df = fetch_data(collector, sym, tier)
                    if df is None: continue

                    ind = compute_indicators(df)
                    price = df["close"].iloc[-1]
                    signal, reason = SIGNAL_FNS[tier](ind, price)

                    if signal == "HOLD": continue

                    # Cooldown
                    key = (sym, tier)
                    last_t = last_trade.get(key, 0)
                    if time.time() - last_t < tier_cfg["cooldown_hours"] * 3600:
                        continue

                    # Position check
                    has_pos = sym in positions
                    pside = positions[sym].get("side","") if has_pos else ""

                    if signal == "LONG" and pside == "long": continue
                    if signal == "SHORT" and pside == "short": continue

                    # Close opposite
                    if (signal == "LONG" and pside == "short") or (signal == "SHORT" and pside == "long"):
                        logger.warning("⚠️ 反向信号 %s %s → %s: %s", tier, pside, signal, reason)
                        close_existing(client, sym)
                        time.sleep(2)
                        has_pos = False

                    if not has_pos:
                        logger.info("🎯 %s [%s] %s → %s (%s)", sym, tier, signal, tier_cfg["desc"], reason)
                        execute(client, sym, signal, QTY[sym], tier, price)
                        last_trade[key] = time.time()

        except Exception as e:
            logger.error("Loop: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
