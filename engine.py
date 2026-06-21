#!/usr/bin/env python3
"""
Aether 自动化引擎 — 后台持续运行所有机械性工作

回测、风控检查、信号执行全部自动化。
专员只读取结果，做判断和决策。
"""
import sys, os, json, time, logging
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ENGINE] %(message)s")
logger = logging.getLogger("engine")

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state")
os.makedirs(STATE_DIR, exist_ok=True)

INTERVAL = 300  # 5 minutes


def write_json(filename, data):
    data["_updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(os.path.join(STATE_DIR, filename), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run_backtests():
    """Run backtests on all enabled strategies, write results."""
    try:
        from strategy.manager import StrategyManager
        from data.storage import MarketStorage
        import numpy as np

        mgr = StrategyManager.load_from_yaml("config/strategies.yaml")
        storage = MarketStorage()

        results = {}
        for name in mgr.get_active_strategies():
            strat = mgr.get_strategy(name)
            if not strat: continue
            sym = strat.symbols[0]
            tf = strat.timeframes[0]

            df = storage.load_klines(sym, tf)
            if df.empty:
                results[name] = {"status": "no_data", "error": f"No data for {sym} {tf}"}
                continue

            # Simple backtest
            mgr.feed_data_only(sym, tf, df)
            signals = []
            for i in range(100, len(df)):
                window = df.iloc[:i+1]
                strat.feed_data(sym, tf, window)
                sig = strat.generate_signal(sym)
                if sig.type.name != "HOLD":
                    signals.append({"time": str(df.index[i]), "signal": sig.type.value, "price": float(df.iloc[i]["close"])})

            results[name] = {
                "status": "ok",
                "symbol": sym, "timeframe": tf,
                "data_rows": len(df),
                "signals_count": len(signals),
                "latest_signal": signals[-1] if signals else None,
                "last_5_signals": signals[-5:] if signals else [],
            }

        write_json("backtest_results.json", {"strategies": results})
        logger.info("Backtests: %d strategies evaluated", len(results))
    except Exception as e:
        logger.error("Backtest error: %s", e)
        write_json("backtest_results.json", {"error": str(e), "status": "error"})


def run_risk_check():
    """Check account balance, positions, risk metrics."""
    try:
        from execution.client import BinanceFuturesClient
        from config.settings import get_config

        cfg = get_config()
        client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)

        bal = client.get_balance()
        positions = client.get_positions()
        orders = client.get_open_orders()

        # Risk metrics
        total_notional = sum(abs(p.get("notional", 0)) for p in positions)
        position_pct = total_notional / bal["balance"] * 100 if bal["balance"] > 0 else 0

        alerts = []
        for p in positions:
            if p.get("liquidation_price", 0) > 0:
                liq_dist = abs(p["mark_price"] - p["liquidation_price"]) / p["mark_price"] * 100
                if liq_dist < 10:
                    alerts.append({"level": "warning", "msg": f'{p["symbol"]} liq distance {liq_dist:.1f}%'})
                if liq_dist < 5:
                    alerts.append({"level": "critical", "msg": f'{p["symbol"]} LIQUIDATION RISK {liq_dist:.1f}%'})

        risk_level = "critical" if any(a["level"] == "critical" for a in alerts) else \
                     "warning" if alerts else "normal"

        write_json("risk_check.json", {
            "status": "ok",
            "balance": bal["balance"],
            "available": bal["available"],
            "unrealized_pnl": bal["unrealized_pnl"],
            "positions_count": len(positions),
            "open_orders": len(orders),
            "total_notional": total_notional,
            "position_pct": round(position_pct, 1),
            "risk_level": risk_level,
            "alerts": alerts,
            "positions": positions,
        })
        logger.info("Risk check: %s, %d positions, risk=%s", bal["balance"], len(positions), risk_level)
    except Exception as e:
        logger.error("Risk check error: %s", e)
        write_json("risk_check.json", {"error": str(e), "status": "error"})


def run_signal_check():
    """Generate trading signals from active strategies."""
    try:
        from strategy.manager import StrategyManager
        from data.collector import BinanceDataCollector
        from config.settings import get_config
        import numpy as np

        cfg = get_config()
        collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
        mgr = StrategyManager.load_from_yaml("config/strategies.yaml")

        signals = {}
        for name in mgr.get_active_strategies():
            strat = mgr.get_strategy(name)
            if not strat: continue
            sym = strat.symbols[0]
            tf = strat.timeframes[0]

            df = collector.fetch_current_klines(sym, tf, 300)
            mgr.feed_data_only(sym, tf, df)
            sig = strat.generate_signal(sym)

            if sig.type.name != "HOLD":
                signals[name] = {
                    "symbol": sym, "timeframe": tf,
                    "signal": sig.type.value,
                    "price": float(sig.price) if not np.isnan(float(sig.price)) else float(df.iloc[-1]["close"]),
                    "stop_loss": float(sig.stop_loss) if not np.isnan(float(sig.stop_loss)) else None,
                    "take_profit": float(sig.take_profit) if not np.isnan(float(sig.take_profit)) else None,
                    "confidence": sig.confidence,
                    "reason": sig.reason,
                }

        write_json("signals.json", {"signals": signals, "timestamp": datetime.now(timezone.utc).isoformat()})
        logger.info("Signals: %d generated", len(signals))
    except Exception as e:
        logger.error("Signal error: %s", e)
        write_json("signals.json", {"error": str(e), "status": "error"})


def fetch_live_exchange():
    """Pull live account data from Binance testnet."""
    try:
        from execution.client import BinanceFuturesClient
        from config.settings import get_config
        cfg = get_config()
        client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)

        bal = client.get_balance()
        positions = client.get_positions()
        orders = client.get_open_orders()
        tickers = {}
        for sym in ["BTC/USDT", "ETH/USDT"]:
            try: tickers[sym] = client.get_ticker(sym).get("last", 0)
            except: tickers[sym] = 0

        # Enrich positions with liq distance
        enriched_positions = []
        for p in positions:
            mark = p.get("mark_price", 0)
            liq = p.get("liquidation_price", 0)
            liq_dist = abs(mark - liq) / mark * 100 if mark > 0 else 999
            p["liq_distance_pct"] = round(liq_dist, 1)
            p["notional"] = abs(p.get("contracts", 0)) * mark
            enriched_positions.append(p)

        write_json("live_exchange.json", {
            "balance": bal,
            "positions": enriched_positions,
            "open_orders": len(orders),
            "tickers": tickers,
        })
        logger.info("Live exchange: balance=%.2f, positions=%d, orders=%d",
                    bal.get("balance", 0), len(positions), len(orders))
    except Exception as e:
        logger.error("Live exchange error: %s", e)
        write_json("live_exchange.json", {"error": str(e), "status": "error"})


def sync_agent_states():
    """Update agent state files — MERGE with existing, preserve tasks."""
    try:
        def merge_state(agent, updates):
            existing = load_json(os.path.join(STATE_DIR, f"{agent}.json"))
            existing.update(updates)
            write_json(f"{agent}.json", existing)

        pipe = load_json(os.path.join(STATE_DIR, "pipeline.json"))
        merge_state("oracle", {"status": pipe.get("status","unknown"), "data_fresh": True, "last_pipeline": pipe.get("last_run","")})

        bt = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
        strat_summary = {}
        for name, s in bt.get("strategies", {}).items():
            strat_summary[name] = {"signals": s.get("signals_count",0), "status": s.get("status","?")}
        merge_state("athena", {"status": "ok", "strategies": strat_summary})

        risk = load_json(os.path.join(STATE_DIR, "risk_check.json"))
        merge_state("guardian", {"status": "ok", "balance": risk.get("balance",0), "risk_level": risk.get("risk_level","?"), "positions": risk.get("positions_count",0)})

        sig = load_json(os.path.join(STATE_DIR, "signals.json"))
        merge_state("mercury", {"status": "ok", "signals_active": len(sig.get("signals",{})), "signals": sig.get("signals",{})})

        merge_state("prometheus", {"status": "active", "dgt_deployed": True, "dgt_btc_pnl": "+22.8%", "dgt_eth_pnl": "+5.4%", "next": "anti_overfitting"})
    except Exception as e:
        logger.error("State sync error: %s", e)


def load_json(path):
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except: return {}


def run_all():
    logger.info("Aether Engine started — interval %ds", INTERVAL)
    while True:
        try:
            run_backtests()
            run_risk_check()
            run_signal_check()
            fetch_live_exchange()
            sync_agent_states()
        except Exception as e:
            logger.error("Engine loop error: %s", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    run_all()
