#!/usr/bin/env python3
"""
Aether AutoTrader — 真正的自动化交易。零人工干预。

架构: 取数据→算指标→判断→下单→管仓位→循环
      没有Agent介入，没有状态锁，没有权限审批。
"""
import os, sys, time, logging, json, sqlite3
from datetime import datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [TRADER] %(message)s")
logger = logging.getLogger("trader")

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
INTERVAL = 60  # 每分钟检查一次

# ====== 交易配置 ======
SYMBOLS = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT"}
LEVERAGE = 3
QTY_BTC = 0.001   # ~$60
QTY_ETH = 0.005   # ~$8
COOLDOWN_BARS = 4  # 持仓至少4根K线后才允许再开
SL_PCT = 0.02
TP_PCT = 0.04

# ====== 简易策略 ======
def check_signals(df, strategy_name):
    """检查入场条件。返回 LONG/SHORT/HOLD + 原因"""
    close = df["close"]
    last = close.iloc[-1]

    if strategy_name == "RSI":
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_v = rsi.iloc[-1]

        if rsi_v < 20:
            return "LONG", f"RSI={rsi_v:.1f} < 20", last
        elif rsi_v > 80:
            return "SHORT", f"RSI={rsi_v:.1f} > 80", last
        return "HOLD", f"RSI={rsi_v:.1f} 正常", last

    elif strategy_name == "BB":
        sma = close.rolling(20).mean()
        std = close.rolling(20).std()
        lower = sma - 2 * std
        upper = sma + 2 * std
        if last < lower.iloc[-1]:
            return "LONG", f"价格{last:.1f} < 布林下轨{lower.iloc[-1]:.1f}", last
        elif last > upper.iloc[-1]:
            return "SHORT", f"价格{last:.1f} > 布林上轨{upper.iloc[-1]:.1f}", last
        return "HOLD", f"价格{last:.1f} 在布林带内", last

    elif strategy_name == "MA_CROSS":
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        if ma5.iloc[-1] > ma20.iloc[-1] and ma5.iloc[-2] <= ma20.iloc[-2]:
            return "LONG", f"MA5({ma5.iloc[-1]:.1f})上穿MA20({ma20.iloc[-1]:.1f})", last
        elif ma5.iloc[-1] < ma20.iloc[-1] and ma5.iloc[-2] >= ma20.iloc[-2]:
            return "SHORT", f"MA5({ma5.iloc[-1]:.1f})下穿MA20({ma20.iloc[-1]:.1f})", last
        return "HOLD", f"MA5={ma5.iloc[-1]:.1f} MA20={ma20.iloc[-1]:.1f}", last

    return "HOLD", "未知策略", last


def get_client():
    from execution.client import BinanceFuturesClient
    from config.settings import get_config
    c = get_config()
    return BinanceFuturesClient(c.api_key, c.api_secret, c.testnet)


def get_live_positions(client):
    """获取当前真实持仓"""
    try:
        return client.get_positions()
    except:
        return []


def cancel_all(client, symbol):
    try:
        orders = client.get_open_orders()
        for o in orders:
            if o.get("symbol", "").replace("/", "") == symbol.replace("/", ""):
                client.cancel_order(o["id"], symbol)
    except:
        pass


def place_trade(client, symbol, side, qty, strategy_name, price):
    """下单+设止损止盈"""
    sym_raw = SYMBOLS.get(symbol, symbol).split("/")[0] + "USDT"
    try:
        client.set_leverage(symbol, LEVERAGE)
    except:
        pass

    cancel_all(client, symbol)
    
    # 下单
    result = client.place_order(symbol, side, qty, order_type="MARKET")
    order_id = result.get("order", {}).get("id", "?") if result else "?"
    fill_price = result.get("price", price) if result else price

    if not fill_price or fill_price <= 0:
        logger.warning("未拿到成交价，跳过SL/TP")
        return order_id, fill_price

    sl = fill_price * (1 - SL_PCT if side == "LONG" else 1 + SL_PCT)
    tp = fill_price * (1 + TP_PCT if side == "LONG" else 1 - TP_PCT)

    try:
        sl_side = "SELL" if side == "LONG" else "BUY"
        client.place_order(symbol, sl_side, qty, order_type="STOP_MARKET",
                          stop_price=sl, reduce_only=True)
        client.place_order(symbol, sl_side, qty, order_type="TAKE_PROFIT_MARKET",
                          stop_price=tp, reduce_only=True)
    except Exception as e:
        logger.warning("SL/TP: %s", e)

    card = {
        "策略": strategy_name, "标的": symbol, "方向": side,
        "数量": qty, "入场价": fill_price, "止损": round(sl, 1),
        "止盈": round(tp, 1), "杠杆": f"{LEVERAGE}x",
        "订单ID": order_id, "时间": datetime.now(timezone.utc).strftime("%H:%M"),
    }
    logger.info("TRADE: %s", json.dumps(card, ensure_ascii=False))

    # DB记录
    try:
        conn = sqlite3.connect(DB)
        conn.execute("""INSERT INTO trades_log(symbol,side,entry_time,entry_price,quantity,strategy_name,reason,status)
                      VALUES(?,?,?,?,?,?,?,?)""",
                   (sym_raw, side, time.time(), fill_price, qty, strategy_name,
                    f"{card['方向']} {card['入场价']}", "OPEN"))
        conn.commit(); conn.close()
    except: pass

    return order_id, fill_price


def close_position(client, symbol, side_str=""):
    """平仓"""
    cancel_all(client, symbol)
    try:
        client.close_position(symbol)
        logger.info("CLOSED: %s %s", symbol, side_str)
        # Update DB
        conn = sqlite3.connect(DB)
        sym_raw = SYMBOLS.get(symbol, "?").split("/")[0] + "USDT"
        conn.execute("""UPDATE trades_log SET status='CLOSED', exit_time=?, exit_price=
                       (SELECT mark_price FROM klines WHERE symbol=? ORDER BY open_time DESC LIMIT 1)
                       WHERE symbol=? AND status='OPEN'""",
                   (time.time(), symbol, sym_raw))
        conn.commit(); conn.close()
    except Exception as e:
        logger.error("Close error: %s", e)


def main():
    logger.info("AutoTrader 启动 — interval=%ds", INTERVAL)
    client = get_client()
    last_entry = {}  # symbol → timestamp of last entry
    
    while True:
        try:
            positions = get_live_positions(client)
            pos_symbols = {p["symbol"]: p for p in positions}
            
            for sym, bin_sym in SYMBOLS.items():
                # ====== 取数据 ======
                from data.collector import BinanceDataCollector
                from config.settings import get_config
                cfg = get_config()
                collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
                df = collector.fetch_current_klines(sym, "1h", 200)
                if df is None or df.empty:
                    continue
                
                has_pos = sym in pos_symbols
                current_pos = pos_symbols.get(sym, {})
                pos_side = current_pos.get("side", "")
                
                # ====== 策略判断 ======
                for strat_name in ["RSI", "BB", "MA_CROSS"]:
                    signal, reason, price = check_signals(df, strat_name)
                    
                    if signal == "HOLD":
                        continue
                    
                    if signal == "LONG" and pos_side == "long":
                        continue  # 已持仓同方向
                    if signal == "SHORT" and pos_side == "short":
                        continue
                    
                    # ====== 平反向仓位 ======
                    if (signal == "LONG" and pos_side == "short") or \
                       (signal == "SHORT" and pos_side == "long"):
                        logger.info("平反向仓位: %s %s → 准备开%s", sym, pos_side, signal)
                        close_position(client, sym, pos_side)
                        time.sleep(2)
                        has_pos = False
                    
                    # ====== 冷却检查 ======
                    last_t = last_entry.get(sym, 0)
                    if time.time() - last_t < COOLDOWN_BARS * 3600:
                        continue
                    
                    # ====== 开仓 ======
                    if not has_pos:
                        qty = QTY_BTC if "BTC" in sym else QTY_ETH
                        logger.info("🚀 %s: %s (%s)", sym, signal, reason)
                        place_trade(client, sym, signal, qty, strat_name, price)
                        last_entry[sym] = time.time()
                        break  # 一个标的只开一单
            
            # 每30秒输出一次状态
            if int(time.time()) % 30 < INTERVAL:
                bal = client.get_balance()
                logger.info("状态: 余额=%.2f 持仓=%d", bal.get("balance", 0), len(positions))
                
        except Exception as e:
            logger.error("Loop error: %s", e)
        
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
