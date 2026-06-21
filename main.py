#!/usr/bin/env python3
"""
Aether (以太) — Binance USDT-M Futures 全自动量化交易系统

运行模式:
  python main.py --mode paper             # 模拟盘 (测试网)
  python main.py --mode backtest          # 回测模式
  python main.py --mode live              # 实盘 (需确认)
  python main.py --maintenance            # 数据库维护

用法:
  python main.py --mode paper --symbols BTC/USDT,ETH/USDT --timeframe 15m
"""

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from config.settings import get_config
from data.collector import BinanceDataCollector
from data.storage import MarketStorage
from execution.client import BinanceFuturesClient
from execution.engine import OrderExecutionEngine
from risk.manager import RiskManager
from strategy.base import Signal, SignalType
from strategy.manager import StrategyManager
from strategy.examples.ma_cross import MACrossoverStrategy
from strategy.examples.rsi_mean_reversion import RSIMeanReversionStrategy

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Global shutdown flag
# ---------------------------------------------------------------------------
_shutdown = False

def _handle_shutdown(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down gracefully...", signum)
    _shutdown = True

signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ===========================================================================
# 回测模式
# ===========================================================================
def run_backtest(symbols: List[str], timeframe: str, lookback_days: int = 90):
    """运行完整回测并输出报告。"""
    from backtest.engine import BacktestEngine

    cfg = get_config()
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)

    # 加载历史数据
    logger.info("🔄 加载 %s K线数据 (%s, %d天)...", symbols, timeframe, lookback_days)
    for sym in symbols:
        df = collector.fetch_historical(sym, timeframe, days=lookback_days)
        logger.info("  %s: %d 根K线", sym, len(df))

        # 存入本地 SQLite
        storage = MarketStorage()
        storage.save_klines(df, sym, timeframe)

    # 加载数据并运行回测
    storage = MarketStorage()
    engine = BacktestEngine(
        initial_capital=cfg.initial_capital,
        commission=cfg.commission,
        slippage=cfg.slippage,
    )

    for sym in symbols:
        df = storage.load_klines(sym, timeframe)
        if df.empty:
            logger.warning("  ⚠️ %s 无数据,跳过", sym)
            continue

        logger.info("\n📊 ====== %s 回测 ======", sym)

        # 创建策略管理器并注册策略
        mgr = StrategyManager()
        ma_strat = MACrossoverStrategy(
            symbols=[sym], timeframes=[timeframe],
            fast_period=7, slow_period=25,
            atr_period=14, atr_sl_mult=2.0, atr_tp_mult=3.0,
        )
        rsi_strat = RSIMeanReversionStrategy(
            symbols=[sym], timeframes=[timeframe],
            rsi_period=14, oversold=30, overbought=70,
        )
        mgr.register(ma_strat)
        mgr.register(rsi_strat)

        # 喂入全部历史数据
        mgr.feed_data_only(sym, timeframe, df)

        # 生成逐K线信号序列
        signals_list = []
        for i in range(100, len(df)):  # 跳过前100根(指标计算需要)
            window = df.iloc[:i+1]
            for strategy in [ma_strat, rsi_strat]:
                strategy.feed_data(sym, timeframe, window)
                sig = strategy.generate_signal(sym)
                if sig.type in (SignalType.LONG, SignalType.SHORT):
                    signals_list.append({
                        "time": df.index[i],
                        "signal": 1 if sig.type == SignalType.LONG else -1,
                    })

        # 构建信号序列
        if signals_list:
            sig_df = pd.DataFrame(signals_list).set_index("time")
            full_sig = pd.Series(0, index=df.index)
            for idx, row in sig_df.iterrows():
                if idx in full_sig.index:
                    full_sig[idx] = row["signal"]
        else:
            full_sig = pd.Series(0, index=df.index)

        # 分别回测
        for strat_name, strat_obj in [("MA Crossover", ma_strat), ("RSI Mean Reversion", rsi_strat)]:
            print(f"\n{'='*50}")
            print(f"  策略: {strat_name} | 标的: {sym}")
            print(f"{'='*50}")
            result = engine.run(df, full_sig)
            engine.print_report(result)

    logger.info("\n✅ 回测完成")


# ===========================================================================
# 模拟盘 / 实盘模式
# ===========================================================================
def run_live(mode: str, symbols: List[str], timeframe: str,
             lookback_bars: int = 500, loop_seconds: int = 60):
    """
    模拟盘或实盘运行。

    mode: 'paper' (测试网) 或 'live' (实盘)
    """
    cfg = get_config()

    # 安全检查
    if mode == "live":
        print("\n⚠️⚠️⚠️  实盘模式警告  ⚠️⚠️⚠️")
        print("本系统将使用真实资金进行交易!")
        confirm = input("输入 'LIVE' 确认: ")
        if confirm != "LIVE":
            print("已取消")
            return

    # ---- 初始化各模块 ----
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
    client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)
    engine = OrderExecutionEngine(client)
    risk = RiskManager(
        max_positions=3, max_leverage=10,
        max_per_symbol_pct=0.15, max_total_position_pct=0.40,
        daily_loss_limit_pct=0.05,
    )
    storage = MarketStorage()

    # ---- 注册策略 ----
    mgr = StrategyManager()
    ma_strat = MACrossoverStrategy(
        symbols=symbols, timeframes=[timeframe],
        fast_period=7, slow_period=25,
        atr_period=14, atr_sl_mult=2.0, atr_tp_mult=3.0,
    )
    rsi_strat = RSIMeanReversionStrategy(
        symbols=symbols, timeframes=[timeframe],
        rsi_period=14, oversold=30, overbought=70,
    )
    mgr.register(ma_strat)
    mgr.register(rsi_strat)
    logger.info("✅ 已注册策略: %s", mgr.get_active_strategies())

    # ---- 加载历史数据作为策略基础 ----
    logger.info("🔄 加载历史K线数据 (lookback=%d根)...", lookback_bars)
    for sym in symbols:
        try:
            df = collector.fetch_current_klines(sym, timeframe, lookback_bars)
            logger.info("  %s: %d 根K线 (%s ~ %s)",
                        sym, len(df),
                        df.index[0] if not df.empty else "N/A",
                        df.index[-1] if not df.empty else "N/A")
            storage.save_klines(df, sym, timeframe)
            mgr.feed_data_only(sym, timeframe, df)
        except Exception as e:
            logger.error("  ❌ %s 加载失败: %s", sym, e)

    # ---- 获取当前账户信息 ----
    try:
        balance_info = client.get_balance()
        logger.info("💰 账户余额: %.2f USDT (可用: %.2f)", 
                    balance_info["balance"], balance_info["available"])
        risk.update_daily_balance(balance_info["balance"])
    except Exception as e:
        logger.warning("⚠️ 无法获取账户余额: %s", e)

    # ---- 主循环 ----
    logger.info("\n🚀 开始 %s 交易循环 (标的: %s, 周期: %s, 检查间隔: %ds)",
                "模拟盘" if mode == "paper" else "实盘",
                symbols, timeframe, loop_seconds)
    logger.info("按 Ctrl+C 停止\n")

    iteration = 0
    while not _shutdown:
        iteration += 1
        tick_start = time.time()

        try:
            # 1. 获取账户快照
            account_info = _get_account_snapshot(client, symbols)

            # 2. 遍历每个标的
            for sym in symbols:
                _process_symbol(
                    sym, timeframe, iteration,
                    collector, storage, mgr, risk, engine,
                    client, account_info, mode,
                )

            # 3. 日志
            elapsed = time.time() - tick_start
            logger.info("🔄 迭代 #%d 完成 (%.1fs)", iteration, elapsed)

        except Exception as e:
            logger.error("❌ 主循环异常: %s", e, exc_info=True)

        # 等待下一个周期
        sleep_time = max(1, loop_seconds - (time.time() - tick_start))
        for _ in range(int(sleep_time)):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("👋 系统已停止")


def _get_account_snapshot(client: BinanceFuturesClient,
                          symbols: List[str]) -> Dict:
    """获取账户快照用于风控。"""
    try:
        balance = client.get_balance()
        positions = client.get_positions()
        return {
            "balance": balance["balance"],
            "available": balance["available"],
            "unrealized_pnl": balance["unrealized_pnl"],
            "positions": positions,
        }
    except Exception:
        return {"balance": 0, "available": 0, "unrealized_pnl": 0, "positions": []}


def _process_symbol(symbol: str, timeframe: str, iteration: int,
                    collector, storage, mgr, risk, engine,
                    client, account_info: Dict, mode: str):
    """处理单个标的：拉取数据 → 生成信号 → 风控 → 执行。"""
    # 1. 拉取最新K线
    df = None
    try:
        df = collector.fetch_current_klines(symbol, timeframe, lookback_bars=500)
    except Exception as e:
        logger.debug("%s 数据拉取跳过: %s", symbol, e)
        return

    if df is None or df.empty:
        return

    # 2. 保存到本地数据库
    try:
        storage.save_klines(df, symbol, timeframe)
    except Exception:
        pass

    # 3. 喂入策略并生成信号
    mgr.feed_data_only(symbol, timeframe, df)
    signals = mgr.generate_all_signals(symbol)

    # 4. 逐个处理信号
    for strat_name, signal in signals.items():
        if signal.type in (SignalType.HOLD,):
            continue

        sig_dict = signal.to_dict()
        sig_dict["leverage"] = signal.leverage or 5

        # 检查持仓是否已存在 (避免重复开仓)
        bin_sym = client.to_binance_symbol(symbol)
        existing_pos = [
            p for p in account_info.get("positions", [])
            if p.get("symbol", "").replace("/", "").upper() == bin_sym.upper()
        ]
        has_position = len(existing_pos) > 0

        if signal.type in (SignalType.LONG, SignalType.SHORT) and has_position:
            logger.debug("  ⏭️  %s: %s 已有持仓,跳过开仓", symbol, signal.type.value)
            continue
        if signal.type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT) and not has_position:
            logger.debug("  ⏭️  %s: 无持仓,跳过平仓", symbol)
            continue

        # 5. 风控检查
        risk_result = risk.check_signal(sig_dict, account_info)
        if risk_result.action == "REJECT":
            logger.warning("  🛑 %s [%s] 风控拒绝: %s",
                          symbol, signal.type.value, risk_result.reason)
            continue
        if risk_result.action == "REDUCE" and risk_result.adjusted_quantity:
            sig_dict["quantity"] = risk_result.adjusted_quantity
            logger.info("  ⚠️ %s 仓位调降: %.4f", symbol, risk_result.adjusted_quantity)

        # 6. 执行订单
        logger.info("  📡 %s [%s] %s → 执行中... (理由: %s)",
                    symbol, signal.type.value, strat_name, signal.reason)

        if mode == "paper":
            # 模拟盘: 仅打印,不实际下单
            _paper_execute(symbol, signal, sig_dict, client, account_info, risk)
        else:
            # 实盘: 真实下单
            result = engine.execute_signal(sig_dict, account_info)
            if result.get("success"):
                logger.info("  ✅ 订单成功: %s", result.get("action"))
                # 更新风控的日盈亏
                if signal.type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
                    # 从订单结果计算PnL
                    order = result.get("order", {})
                    avg_price = float(order.get("average", order.get("price", 0)) or 0)
                    # 简化: 记录成交
                    strat = mgr.get_strategy(strat_name)
                    if strat and strat.has_position(symbol):
                        pos = strat.get_position(symbol)
                        entry = pos["entry_price"]
                        qty = pos["quantity"]
                        pnl = (avg_price - entry) * qty
                        risk.record_trade_pnl(pnl)
            else:
                logger.error("  ❌ 订单失败: %s", result.get("error"))


def _paper_execute(symbol: str, signal: Signal, sig_dict: Dict,
                   client, account_info: Dict, risk: RiskManager):
    """模拟盘执行: 打印订单信息,跟踪模拟仓位。"""
    from config.settings import get_config
    cfg = get_config()
    ticker = None
    try:
        ticker = client.get_ticker(symbol)
    except Exception:
        pass

    current_price = ticker.get("last", 0) if ticker else 0
    qty = sig_dict.get("quantity", cfg.default_quantity)
    leverage = sig_dict.get("leverage", cfg.default_leverage)
    order_value = qty * current_price if current_price else 0

    print(f"""
  ╔══════════════════════════════════════╗
  ║  📋 模拟订单                         ║
  ╠══════════════════════════════════════╣
  ║  标的:    {symbol:<28s} ║
  ║  方向:    {signal.type.value:<28s} ║
  ║  数量:    {qty:<28.4f} ║
  ║  价格:    {current_price:<28.2f} ║
  ║  金额:    {order_value:<28.2f} USDT ║
  ║  杠杆:    {leverage}x{'':>25s} ║
  ║  止损:    {signal.stop_loss if not pd.isna(signal.stop_loss) else 'N/A':>28s} ║
  ║  止盈:    {signal.take_profit if not pd.isna(signal.take_profit) else 'N/A':>28s} ║
  ║  策略:    {signal.strategy_name:<28s} ║
  ║  理由:    {signal.reason[:28]:<28s} ║
  ╚══════════════════════════════════════╝
    """)

    # 模拟更新仓位
    if signal.type in (SignalType.LONG, SignalType.SHORT):
        sid = "buy" if signal.type == SignalType.LONG else "sell"
        risk._daily_starting_balance = account_info.get("balance", 0)
        logger.info("  📝 模拟开仓: %s %s @ %.2f x%.4f",
                    symbol, signal.type.value, current_price, qty)
    elif signal.type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
        logger.info("  📝 模拟平仓: %s %s @ %.2f",
                    symbol, signal.type.value, current_price)


# ===========================================================================
# 主入口
# ===========================================================================
if __name__ == "__main__":
    cfg = get_config()

    parser = argparse.ArgumentParser(
        description="币安 U本位合约 全自动量化交易系统",
    )
    parser.add_argument(
        "--mode", choices=["backtest", "paper", "live"],
        default="paper", help="运行模式 (默认: paper)"
    )
    parser.add_argument(
        "--symbols", default=",".join(cfg.symbols),
        help="交易标的,逗号分隔 (默认: BTC/USDT,ETH/USDT)"
    )
    parser.add_argument(
        "--timeframe", default=cfg.default_timeframe,
        help="K线周期 (默认: 1h)"
    )
    parser.add_argument(
        "--lookback-days", type=int, default=90,
        help="回测历史天数 (默认: 90)"
    )
    parser.add_argument(
        "--lookback-bars", type=int, default=500,
        help="实盘/模拟盘历史K线根数 (默认: 500)"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="交易循环检查间隔(秒) (默认: 60)"
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]

    print(f"""
╔══════════════════════════════════════════════╗
║  Aether (以太) v1.0                           ║
║  Binance USDT-M Futures Auto Trading System   ║
╠══════════════════════════════════════════════╣
║  模式:     {args.mode:<33s} ║
║  标的:     {', '.join(symbols):<33s} ║
║  周期:     {args.timeframe:<33s} ║
║  测试网:   {'是' if cfg.testnet else '否':>33s} ║
╚══════════════════════════════════════════════╝
    """)

    if args.mode == "backtest":
        run_backtest(symbols, args.timeframe, args.lookback_days)
    else:
        run_live(args.mode, symbols, args.timeframe,
                 args.lookback_bars, args.interval)
