#!/usr/bin/env python3
"""Mercury — Aether 交易执行者。拉升行情→加载策略→生成信号→币安测试网下单。"""

import sys, os, json, time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

# Ensure project root is first in sys.path
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)
# Remove any stale cwd entries that might shadow stdlib
for p in list(sys.path):
    if p == "" or p == ".":
        sys.path.remove(p)

from config.settings import get_config
from data.collector import BinanceDataCollector
from data.storage import MarketStorage
from execution.client import BinanceFuturesClient
from execution.engine import OrderExecutionEngine
from risk.manager import RiskManager
from strategy.base import Signal, SignalType
from strategy.manager import StrategyManager

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def fmt_price(p: float) -> str:
    if p is None or pd.isna(p):
        return "N/A"
    return f"{p:,.2f}"

def fmt_pnl(p: float) -> str:
    if p is None or pd.isna(p):
        return "N/A"
    color = "🟢" if p >= 0 else "🔴"
    return f"{color} {p:+.2f}"

def debug_log(msg: str):
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ──────────────────────────────────────────────────────────────
# Main Execution
# ──────────────────────────────────────────────────────────────

def main():
    cfg = get_config()
    print("=" * 62)
    print("  ☿ Mercury (墨丘利) — Aether 交易执行者")
    print(f"  启动: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  网络: {'测试网' if cfg.testnet else '实盘'}")
    print("=" * 62)

    # ── 1. Initialize modules ─────────────────────────────────
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
    client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)
    engine = OrderExecutionEngine(client, max_retries=3, retry_delay=1.5)
    risk = RiskManager(
        max_positions=3,
        max_leverage=10,
        max_per_symbol_pct=0.20,
        max_total_position_pct=0.50,
        daily_loss_limit_pct=0.05,
        min_order_usdt=10.0,
    )
    storage = MarketStorage()

    # ── 2. Get account snapshot ───────────────────────────────
    print("\n📊 账户快照")
    print("-" * 40)
    balance = None
    positions = []
    for attempt in range(3):
        try:
            balance = client.get_balance()
            positions = client.get_positions()
            if balance.get("balance", 0) > 0 or balance.get("available", 0) > 0:
                break
            print(f"  ⚠️  余额为0,重试 {attempt+1}/3...")
            time.sleep(1)
        except Exception as e:
            print(f"  ⚠️  获取账户失败(尝试{attempt+1}/3): {e}")
            time.sleep(1)
    if balance is None:
        balance = {"balance": 0, "available": 0, "unrealized_pnl": 0}

    print(f"  余额:     {balance['balance']:,.2f} USDT")
    print(f"  可用:     {balance['available']:,.2f} USDT")
    print(f"  未实现盈亏: {fmt_pnl(balance['unrealized_pnl'])} USDT")
    risk.update_daily_balance(balance['balance'])

    account_info = {
        "balance": balance["balance"],
        "available": balance["available"],
        "unrealized_pnl": balance["unrealized_pnl"],
        "positions": positions,
    }

    if positions:
        print(f"\n  当前持仓 ({len(positions)}):")
        for p in positions:
            side_emoji = "🟩 LONG" if p.get("side") == "long" else "🟥 SHORT"
            print(f"    {side_emoji}  {p.get('symbol','?')}  "
                  f"数量:{p.get('contracts',0):.4f}  "
                  f"入场:{p.get('entry_price',0):.2f}  "
                  f"标记:{p.get('mark_price',0):.2f}  "
                  f"PnL:{fmt_pnl(p.get('unrealized_pnl',0))}  "
                  f"强平:{p.get('liquidation_price',0):.2f}  "
                  f"杠杆:{p.get('leverage',1)}x")
    else:
        print("\n  当前无持仓")

    # ── 3. Load strategies ────────────────────────────────────
    print("\n🧠 加载策略")
    print("-" * 40)
    try:
        mgr = StrategyManager.load_from_yaml('config/strategies.yaml')
        active = mgr.get_active_strategies()
        print(f"  已加载 {len(active)} 个策略: {', '.join(active)}")
    except Exception as e:
        print(f"  ❌ 策略加载失败: {e}")
        return

    # ── 4. Pull market data ───────────────────────────────────
    print("\n📡 拉取行情数据")
    print("-" * 40)

    # Determine needed timeframes from active strategies
    needed_timeframes = set()
    needed_symbols = set()
    for name in active:
        s = mgr.get_strategy(name)
        for sym in s.symbols:
            needed_symbols.add(sym)
        for tf in s.timeframes:
            needed_timeframes.add(tf)

    # Ensure we have at least 1h for oracle-level data
    needed_timeframes.add("1h")
    needed_symbols = needed_symbols or {"BTC/USDT", "ETH/USDT"}

    print(f"  标的: {', '.join(sorted(needed_symbols))}")
    print(f"  周期: {', '.join(sorted(needed_timeframes))}")

    # Fetch data for all symbol×timeframe combos
    market_data = {}
    for sym in sorted(needed_symbols):
        for tf in sorted(needed_timeframes):
            df = None
            try:
                df = collector.fetch_current_klines(sym, tf, lookback_bars=500)
                last_close = float(df["close"].iloc[-1])
                print(f"  {sym} {tf}: {len(df)}根K线 | 最新价: {last_close:,.2f}")
            except Exception as e:
                print(f"  {sym} {tf}: ❌ 获取失败: {e}")
                continue

            # Save to DB (non-fatal — if DB is locked by another process, still use in-memory data)
            try:
                storage.save_klines(df, sym, tf)
            except Exception as e:
                print(f"  {sym} {tf}: ⚠️ DB写入跳过 ({e})")

            market_data[(sym, tf)] = df

    # ── 5. Feed data to strategies & generate signals ────────
    print("\n🎯 信号生成")
    print("-" * 40)

    all_signals: List[Signal] = []
    signal_details: List[Dict] = []

    for name in active:
        strategy = mgr.get_strategy(name)
        for sym in strategy.symbols:
            # Feed data for each timeframe the strategy needs
            for tf in strategy.timeframes:
                df = market_data.get((sym, tf))
                if df is not None and not df.empty:
                    strategy.feed_data(sym, tf, df)

            # Generate signal
            sig = strategy.generate_signal(sym)
            strategy_name = sig.strategy_name or name

            if sig.type == SignalType.HOLD:
                signal_details.append({
                    "strategy": strategy_name,
                    "symbol": sym,
                    "signal": "HOLD",
                    "reason": sig.reason,
                })
                continue

            sig_dict = sig.to_dict()
            sig_dict["leverage"] = sig.leverage or cfg.default_leverage
            all_signals.append((strategy_name, sym, sig, sig_dict))

            current_price = market_data.get((sym, strategy.timeframes[0]))
            price_val = float(current_price["close"].iloc[-1]) if current_price is not None else 0

            signal_details.append({
                "strategy": strategy_name,
                "symbol": sym,
                "signal": sig.type.value,
                "price": price_val,
                "qty": sig.quantity,
                "sl": sig.stop_loss,
                "tp": sig.take_profit,
                "leverage": sig.leverage or cfg.default_leverage,
                "reason": sig.reason,
                "confidence": sig.confidence,
            })

    # Print all signal details
    if not signal_details:
        print("  (无信号)")
    for sd in signal_details:
        if sd["signal"] == "HOLD":
            print(f"  ⏸️  {sd['strategy']} | {sd['symbol']}: HOLD ({sd['reason'][:60]})")
        else:
            print(f"  🚨 {sd['strategy']} | {sd['symbol']}: {sd['signal']} "
                  f"@ ${sd['price']:,.2f} x{sd.get('qty',0):.4f} "
                  f"| SL:{fmt_price(sd.get('sl'))} TP:{fmt_price(sd.get('tp'))} "
                  f"| 杠杆:{sd.get('leverage',5)}x "
                  f"| 置信度:{sd.get('confidence',0):.1%}")
            if sd.get("reason"):
                print(f"      理由: {sd['reason'][:80]}")

    # ── 6. Risk check & Execute ──────────────────────────────
    trades_executed = 0
    trades_skipped = 0
    execution_results = []

    if all_signals:
        print("\n⚡ 风控 & 执行")
        print("-" * 40)

    for strategy_name, sym, sig, sig_dict in all_signals:
        sig_type = sig.type

        # Check if position already exists for this symbol
        bin_sym = client.to_binance_symbol(sym)
        existing_pos = [
            p for p in positions
            if p.get("symbol", "").replace("/", "").replace(":USDT", "").upper() == bin_sym.upper()
        ]
        has_position = len(existing_pos) > 0

        if sig_type in (SignalType.LONG, SignalType.SHORT) and has_position:
            existing_side = existing_pos[0].get("side", "")
            new_side = "long" if sig_type == SignalType.LONG else "short"
            if existing_side != new_side:
                # ═══ Reversal: close existing position, then open opposite ═══
                print(f"  🔄 {sym}: 持有{existing_side}, 收到{new_side}信号 → 反转中...")
                close_type = SignalType.CLOSE_LONG if existing_side == "long" else SignalType.CLOSE_SHORT
                close_sig = {
                    "type": close_type.value,
                    "symbol": sym,
                    "quantity": existing_pos[0].get("contracts", sig_dict.get("quantity", 0)),
                    "price": None,
                }
                try:
                    close_result = engine.execute_signal(close_sig, account_info)
                    close_order = close_result.get("order") or {}
                    if close_result.get("success"):
                        pnl = float(close_order.get("realizedPnl", 0) or 0)
                        print(f"    ✅ 平{existing_side}成功 — 已实现盈亏: {fmt_pnl(pnl)}")
                        positions = [p for p in positions if p.get("symbol","").replace("/","").replace(":USDT","").upper() != bin_sym.upper()]
                        account_info["positions"] = positions
                        has_position = False
                    else:
                        print(f"    ❌ 平仓失败: {close_result.get('error')}")
                        trades_skipped += 1
                        execution_results.append({
                            "symbol": sym, "strategy": strategy_name,
                            "signal": sig_type.value, "status": "FAILED",
                            "reason": f"平仓反转失败: {close_result.get('error', 'unknown')}"
                        })
                        continue
                except Exception as e:
                    print(f"    ❌ 反转异常: {e}")
                    trades_skipped += 1
                    execution_results.append({
                        "symbol": sym, "strategy": strategy_name,
                        "signal": sig_type.value, "status": "ERROR",
                        "reason": f"反转异常: {e}"
                    })
                    continue
            else:
                print(f"  ⏭️  {sym} {sig_type.value}: 已有同向持仓,跳过加仓")
                trades_skipped += 1
                execution_results.append({
                    "symbol": sym, "strategy": strategy_name,
                    "signal": sig_type.value, "status": "SKIPPED",
                    "reason": "已有同向持仓"
                })
                continue

        if sig_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT) and not has_position:
            print(f"  ⏭️  {sym} {sig_type.value}: 无持仓,跳过平仓")
            trades_skipped += 1
            execution_results.append({
                "symbol": sym, "strategy": strategy_name,
                "signal": sig_type.value, "status": "SKIPPED",
                "reason": "无持仓"
            })
            continue

        # Risk check
        risk_result = risk.check_signal(sig_dict, account_info)
        if risk_result.action == "REJECT":
            print(f"  🛑 {sym} {sig_type.value} [{strategy_name}] 风控拒绝: {risk_result.reason}")
            trades_skipped += 1
            execution_results.append({
                "symbol": sym, "strategy": strategy_name,
                "signal": sig_type.value, "status": "REJECTED",
                "reason": risk_result.reason
            })
            continue

        if risk_result.action == "REDUCE" and risk_result.adjusted_quantity:
            sig_dict["quantity"] = risk_result.adjusted_quantity
            print(f"  ⚠️  {sym} 仓位调降: {risk_result.adjusted_quantity:.4f}")

        # Execute!
        # ═══ Before opening: cancel existing open orders on this symbol ═══
        if sig_type in (SignalType.LONG, SignalType.SHORT):
            try:
                open_orders = client.get_open_orders(sym)
                if open_orders:
                    print(f"  🧹 清理 {sym} 现有 {len(open_orders)} 个挂单...")
                    client.cancel_all_orders(sym)
            except Exception as oe:
                print(f"  ⚠️  清理挂单异常: {oe}")

        print(f"  📡 {sym} {sig_type.value} [{strategy_name}] → 执行中...")
        try:
            result = engine.execute_signal(sig_dict, account_info)

            order = result.get("order") or {}
            order_id = order.get("id", order.get("orderId", "N/A"))
            avg_price = float(order.get("average", order.get("price", 0)) or 0)
            executed_qty = float(order.get("amount", order.get("executedQty", sig_dict.get("quantity", 0))) or 0)
            status = order.get("status", "UNKNOWN")

            if result.get("success"):
                print(f"    ✅ 成交! 订单ID: {order_id} | 状态: {status} | "
                      f"价格: {avg_price:.2f} | 数量: {executed_qty:.4f}")
                trades_executed += 1
                execution_results.append({
                    "symbol": sym, "strategy": strategy_name,
                    "signal": sig_type.value, "status": "FILLED",
                    "order_id": str(order_id),
                    "price": avg_price,
                    "qty": executed_qty,
                    "order_status": status,
                })

                # Update account positions for subsequent risk checks
                if sig_type in (SignalType.LONG, SignalType.SHORT):
                    new_pos = {
                        "symbol": bin_sym,
                        "side": "long" if sig_type == SignalType.LONG else "short",
                        "contracts": executed_qty,
                        "entry_price": avg_price,
                        "mark_price": avg_price,
                        "unrealized_pnl": 0,
                        "liquidation_price": 0,
                        "leverage": sig_dict.get("leverage", cfg.default_leverage),
                        "notional": executed_qty * avg_price,
                    }
                    positions.append(new_pos)
                    account_info["positions"] = positions
                elif sig_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                    positions = [p for p in positions if p.get("symbol","").replace("/","").replace(":USDT","").upper() != bin_sym.upper()]
                    account_info["positions"] = positions
            else:
                error_msg = result.get("error", "Unknown error")
                print(f"    ❌ 失败: {error_msg}")
                trades_skipped += 1
                execution_results.append({
                    "symbol": sym, "strategy": strategy_name,
                    "signal": sig_type.value, "status": "FAILED",
                    "reason": error_msg,
                })
        except Exception as e:
            print(f"    ❌ 异常: {e}")
            trades_skipped += 1
            execution_results.append({
                "symbol": sym, "strategy": strategy_name,
                "signal": sig_type.value, "status": "ERROR",
                "reason": str(e),
            })

    # ── 7. Summary ────────────────────────────────────────────
    print("\n" + "=" * 62)
    print("  📋 Mercury 执行摘要")
    print("=" * 62)

    # Final positions
    try:
        final_positions = client.get_positions()
        final_balance = client.get_balance()
    except Exception:
        final_positions = positions
        final_balance = balance

    print(f"\n  账户余额: {final_balance['balance']:,.2f} USDT")
    print(f"  交易执行: {trades_executed} 笔 | 跳过: {trades_skipped} 笔")

    if final_positions:
        print(f"\n  当前持仓 ({len(final_positions)}):")
        for p in final_positions:
            side_e = "🟩 LONG" if p.get("side") == "long" else "🟥 SHORT"
            entry = p.get("entry_price", 0)
            mark = p.get("mark_price", 0)
            upnl = p.get("unrealized_pnl", 0)
            liq = p.get("liquidation_price", 0)
            lev = p.get("leverage", 1)
            notion = p.get("notional", 0)
            margin_pct = (notion / final_balance['balance'] * 100) if final_balance['balance'] > 0 else 0

            print(f"    {side_e}  {p.get('symbol','?')}")
            print(f"      数量: {p.get('contracts',0):.4f} | 入场: {entry:,.2f} | 标记: {mark:,.2f}")
            print(f"      未实现: {fmt_pnl(upnl)} | 强平: {liq:,.2f} | 杠杆: {lev}x")
            print(f"      名义价值: {notion:,.2f} USDT ({margin_pct:.2f}% 保证金占用)")
    else:
        print("\n  当前无持仓")

    # Print execution results table
    if execution_results:
        print(f"\n  执行明细:")
        for r in execution_results:
            sid = r["symbol"]
            sig = r["signal"]
            strat = r.get("strategy", "")
            status = r["status"]
            emoji = {"FILLED": "✅", "SKIPPED": "⏭️", "REJECTED": "🛑", "FAILED": "❌", "ERROR": "💥"}.get(status, "❓")
            if status == "FILLED":
                print(f"    {emoji} {sid} {sig} @ ${r.get('price',0):,.2f} x{r.get('qty',0):.4f} | 订单:{r.get('order_id','?')}")
            else:
                print(f"    {emoji} {sid} {sig} [{strat}] → {r.get('reason', status)}")

    # ── 8. Write state & Bulletin ────────────────────────────
    print("\n📝 写入状态...")
    pos_count = len(final_positions)
    trade_count = trades_executed

    state_json = json.dumps({
        "status": "ok",
        "positions": pos_count,
        "trades": trade_count,
        "balance": round(final_balance['balance'], 2),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    cmd_state = f'cd /home/rinnen/binance_quant && python .aether/platform.py write-state mercury \'{state_json}\''
    cmd_bulletin = None

    if trades_executed > 0:
        filled = [r for r in execution_results if r["status"] == "FILLED"]
        parts = []
        for r in filled[:3]:
            parts.append(f"{r['signal']} {r['symbol']}")
        summary = f"Mercury: 执行{trades_executed}笔 — {', '.join(parts)}"
        cmd_bulletin = f'cd /home/rinnen/binance_quant && python .aether/platform.py post-bulletin "{summary}"'
    elif execution_results:
        summary = f"Mercury: 监控中 | 持仓{pos_count} | 无新交易"
        cmd_bulletin = f'cd /home/rinnen/binance_quant && python .aether/platform.py post-bulletin "{summary}"'
    else:
        summary = f"Mercury: 监控中 | 持仓{pos_count} | 无信号"
        cmd_bulletin = f'cd /home/rinnen/binance_quant && python .aether/platform.py post-bulletin "{summary}"'

    print(f"  STATE: {state_json[:120]}...")
    print(f"  BULLETIN: {summary}")

    return state_json, cmd_state, cmd_bulletin, final_positions, final_balance

if __name__ == "__main__":
    state_json, cmd_state, cmd_bulletin, final_positions, final_balance = main()

    # Execute state write
    print("\n" + "=" * 62)
    os.system(cmd_state)
    if cmd_bulletin:
        os.system(cmd_bulletin)
    print("=" * 62)
