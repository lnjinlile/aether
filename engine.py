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


def sync_trades_db():
    """Reconcile trades_log in market.db with live exchange state.
    
    Reads live_exchange.json (already fetched by engine) and:
    1. Closes stale DB records whose position no longer exists on exchange
    2. Inserts exchange positions missing from DB
    """
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "market.db")
        if not os.path.exists(db_path):
            return

        live_state = os.path.join(STATE_DIR, "live_exchange.json")
        if not os.path.exists(live_state):
            return
        with open(live_state) as f:
            live = json.load(f)

        positions = live.get("positions", [])
        if not positions:
            return

        db = sqlite3.connect(db_path)
        db.execute("PRAGMA busy_timeout=10000")  # 10s timeout for concurrent access (data_ext.py)
        db.row_factory = sqlite3.Row
        now = time.time()

        open_trades = db.execute("SELECT * FROM trades_log WHERE status='OPEN'").fetchall()

        # Build exchange key set
        ex_keys = set()
        for ep in positions:
            sym = ep["symbol"].replace(":USDT", "").replace("/", "")
            ex_keys.add(f'{sym}:{ep["side"].upper()}')

        changes = 0
        # 1. Close stale DB records
        for dt in open_trades:
            db_sym = dt["symbol"].replace(":USDT", "").replace("/", "")
            db_key = f'{db_sym}:{dt["side"].upper()}'
            if db_key not in ex_keys:
                db.execute(
                    "UPDATE trades_log SET status='CLOSED', exit_time=?, reason=reason || ' [SYNC: position not on exchange]' WHERE id=?",
                    (now, dt["id"]))
                logger.info("DB sync: closed stale ID#%d (%s %s)", dt["id"], dt["symbol"], dt["side"])
                changes += 1

        # 2. Insert missing exchange positions
        db_keys = {f'{dt["symbol"].replace(":USDT", "").replace("/", "")}:{dt["side"].upper()}' for dt in open_trades}
        for ep in positions:
            sym = ep["symbol"].replace(":USDT", "").replace("/", "")
            ex_key = f'{sym}:{ep["side"].upper()}'
            if ex_key not in db_keys:
                db.execute("""
                    INSERT INTO trades_log (symbol, side, entry_time, entry_price, quantity, pnl, pnl_pct, fee, strategy_name, reason, status)
                    VALUES (?, ?, ?, ?, ?, 0.0, 0.0, 0.0, 'SYNC', '[SYNC: from exchange via engine]', 'OPEN')
                """, (sym, ep["side"].upper(), now, ep["entry_price"], ep["contracts"]))
                logger.info("DB sync: inserted %s %s x%s @ %s", sym, ep["side"].upper(), ep["contracts"], ep["entry_price"])
                changes += 1

        db.commit()
        db.close()
        if changes:
            logger.info("DB sync complete: %d change(s)", changes)
    except Exception as e:
        logger.error("DB sync error: %s", e)


def run_backtests():
    """Run backtests on all enabled strategies using BacktestEngine, write results."""
    try:
        from strategy.manager import StrategyManager
        from data.storage import MarketStorage
        from backtest.engine import BacktestEngine
        from backtest.signal_gen import (
            trendfollow_signals, rsi_mr_signals,
            dynamic_grid_signals, ma_cross_signals,
        )
        import numpy as np
        import pandas as pd
        import yaml

        # Load ALL strategies from YAML (enabled+disabled) to know params for disabled ones
        yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "strategies.yaml")
        with open(yaml_path, "r") as f:
            strat_cfg = yaml.safe_load(f)
        all_strategies = strat_cfg.get("strategies", [])

        storage = MarketStorage()
        engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

        results = {}
        for s in all_strategies:
            name = s["name"]
            enabled = s.get("enabled", True)
            p = s.get("params", {})
            sym = p.get("symbols", [None])[0]
            tf = p.get("timeframes", [None])[0]
            strategy_type = s["class"].split(".")[-1]

            if not sym or not tf:
                results[name] = {"status": "no_config", "error": "Missing symbols/timeframes"}
                continue

            try:
                df = storage.load_klines(sym, tf)
                if df is None or df.empty or len(df) < 50:
                    results[name] = {
                        "status": "no_data",
                        "symbol": sym, "timeframe": tf,
                        "data_rows": len(df) if df is not None else 0,
                        "error": f"Insufficient data ({len(df) if df is not None else 0} bars) for {sym} {tf}",
                    }
                    continue

                # ── Filter to last 7 days (match athena_backtest.py) ──
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                df.set_index('open_time', inplace=True)
                df.sort_index(inplace=True)
                cutoff = df.index[-1] - pd.Timedelta(days=7)
                df = df[df.index >= cutoff]
                if len(df) < 50:
                    results[name] = {
                        "status": "no_data",
                        "symbol": sym, "timeframe": tf,
                        "data_rows": len(df),
                        "error": f"Insufficient recent data ({len(df)} bars in last 7d) for {sym} {tf}",
                    }
                    continue

                # Get leverage from strategy config (match athena_backtest.py)
                leverage = p.get('leverage', 1)

                # Generate signals based on strategy type
                try:
                    if strategy_type == "TrendFollow":
                        signals = trendfollow_signals(
                            df, p["ema_period"], p["stop_loss_pct"],
                            p["take_profit_pct"], p["cooldown_bars"],
                        )
                    elif strategy_type == "RSIMeanReversionStrategy":
                        signals = rsi_mr_signals(
                            df, p["rsi_period"], p["oversold"], p["overbought"],
                            p["exit_rsi"], p["stop_loss_pct"], p["take_profit_pct"],
                            p["cooldown_bars"],
                        )
                    elif strategy_type == "MACrossoverStrategy":
                        signals = ma_cross_signals(
                            df, p["fast_period"], p["slow_period"],
                            p["atr_period"], p["atr_sl_mult"], p["atr_tp_mult"],
                            p["cooldown_bars"],
                        )
                    elif strategy_type == "DynamicGridStrategy":
                        signals = dynamic_grid_signals(
                            df, p["grid_range_pct"], p["num_levels"],
                            p["qty_per_level"], p["rebalance_interval_bars"],
                            p["min_spread_pct"], p.get("leverage", 3),
                        )
                    elif strategy_type == "MLAlphaStrategy":
                        from athena_backtest import mlalpha_signals
                        signals = mlalpha_signals(
                            df, p.get("model_path", "ml_alpha/model.pkl"),
                            p.get("confidence_threshold", 0.55),
                            sl_pct=p.get("atr_sl_mult", 2.0) * 0.01,
                            tp_pct=p.get("atr_tp_mult", 3.0) * 0.01,
                        )
                    elif strategy_type == "MLEnsembleStrategy":
                        from athena_backtest import mlensemble_signals
                        signals = mlensemble_signals(
                            df, p.get("prediction_horizon", 5),
                            p.get("confidence_threshold", 0.60),
                            p.get("min_train_samples", 200),
                            sl_pct=p.get("atr_sl_mult", 2.0) * 0.01,
                            tp_pct=p.get("atr_tp_mult", 3.0) * 0.01,
                        )
                    elif strategy_type == "RegimeSwitchStrategy":
                        from athena_backtest import regimeswitch_signals
                        signals = regimeswitch_signals(
                            df,
                            trend_ema_period=p.get("trend_ema_period", 50),
                            trend_sl_pct=p.get("trend_sl_pct", 0.02),
                            trend_tp_pct=p.get("trend_tp_pct", 0.05),
                            mr_rsi_period=p.get("mr_rsi_period", 14),
                            mr_oversold=p.get("mr_oversold", 30),
                            mr_overbought=p.get("mr_overbought", 70),
                            mr_sl_pct=p.get("mr_sl_pct", 0.02),
                            mr_tp_pct=p.get("mr_tp_pct", 0.04),
                            vol_window=p.get("vol_window", 20),
                            regime_lookback=p.get("regime_lookback", 100),
                            cooldown_bars=p.get("cooldown_bars", 5),
                            high_vol_capital_pct=p.get("high_vol_capital_pct", 0.25),
                        )
                    else:
                        results[name] = {
                            "status": "skipped",
                            "symbol": sym, "timeframe": tf,
                            "data_rows": len(df),
                            "error": f"Unknown strategy type: {strategy_type}",
                        }
                        continue
                except KeyError as ke:
                    results[name] = {
                        "status": "error",
                        "symbol": sym, "timeframe": tf,
                        "data_rows": len(df),
                        "error": f"Missing param {ke} for {strategy_type}",
                    }
                    continue

                # Run BacktestEngine
                bt_result = engine.run(df, signals, leverage=leverage)
                m = bt_result["metrics"]

                # Count signals for backward compatibility
                signal_count = int((signals != 0).sum())

                # Build signal list
                signal_times = df.index[signals != 0]
                signal_values = signals[signals != 0]
                sig_list = []
                for i in range(len(signal_times)):
                    sig_list.append({
                        "time": str(signal_times[i]),
                        "signal": "LONG" if signal_values.iloc[i] == 1 else (
                            "SHORT" if signal_values.iloc[i] == -1 else "EXIT"
                        ),
                        "price": float(df.loc[signal_times[i], "close"]),
                    })

                results[name] = {
                    "status": "ok",
                    "enabled": enabled,
                    "symbol": sym,
                    "timeframe": tf,
                    "data_rows": len(df),
                    "signals_count": signal_count,
                    "latest_signal": sig_list[-1] if sig_list else None,
                    "last_5_signals": sig_list[-5:] if sig_list else [],
                    # Real backtest metrics (NEW)
                    "metrics": {
                        "total_return_pct": m["total_return_pct"],
                        "sharpe_ratio": m["sharpe_ratio"],
                        "deflated_sharpe_ratio": m["deflated_sharpe_ratio"],
                        "max_drawdown_pct": m["max_drawdown_pct"],
                        "win_rate": m["win_rate"],
                        "profit_factor": m["profit_factor"],
                        "total_trades": m["total_trades"],
                        "avg_win_pct": m["avg_win_pct"],
                        "avg_loss_pct": m["avg_loss_pct"],
                        "final_equity": m["final_equity"],
                    },
                }
            except Exception as e:
                logger.error("Backtest error for '%s': %s", name, e)
                results[name] = {
                    "status": "error",
                    "symbol": sym, "timeframe": tf,
                    "error": str(e),
                }

        write_json("backtest_results.json", {"strategies": results})
        # Summary log
        ok_count = sum(1 for r in results.values() if r.get("status") == "ok")
        logger.info("Backtests: %d/%d strategies evaluated with real metrics", ok_count, len(results))
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
        # Track signals per symbol for conflict detection
        symbol_signals = {}  # symbol -> list of (name, signal_dict)

        for name in mgr.get_active_strategies():
            strat = mgr.get_strategy(name)
            if not strat: continue
            sym = strat.symbols[0]
            tf = strat.timeframes[0]

            try:
                df = collector.fetch_current_klines(sym, tf, 300)
                mgr.feed_data_only(sym, tf, df)
                sig = strat.generate_signal(sym)
            except Exception as e:
                logger.error("Signal error for strategy '%s' (%s %s): %s", name, sym, tf, e)
                continue

            if sig.type.name != "HOLD":
                signal_data = {
                    "symbol": sym, "timeframe": tf,
                    "signal": sig.type.value,
                    "price": float(sig.price) if not np.isnan(float(sig.price)) else float(df.iloc[-1]["close"]),
                    "stop_loss": float(sig.stop_loss) if not np.isnan(float(sig.stop_loss)) else None,
                    "take_profit": float(sig.take_profit) if not np.isnan(float(sig.take_profit)) else None,
                    "confidence": sig.confidence,
                    "reason": sig.reason,
                    "strategy": name,
                }
                symbol_signals.setdefault(sym, []).append((name, signal_data))

        # ── Conflict arbitration (AUDIT-018) ──
        # When multiple strategies produce signals for the same symbol,
        # apply priority: backtest-verified > unverified, higher Sharpe > lower
        bt_results = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
        bt_strategies = bt_results.get("strategies", {})

        def strategy_score(name: str) -> float:
            """Score a strategy for priority: positive metrics = higher score."""
            s = bt_strategies.get(name, {})
            m = s.get("metrics", {})
            if not m or s.get("status") == "error":
                return -999.0  # unverified/errored strategies lose
            return m.get("sharpe_ratio", 0) * 10 + m.get("total_return_pct", 0) / 10

        for sym, candidates in symbol_signals.items():
            if len(candidates) == 1:
                sig_name, sig_data = candidates[0]
                signals[sig_name] = sig_data
                continue

            # Multiple strategies → resolve conflicts
            # Separate by direction
            long_candidates = [(n, d) for n, d in candidates if d["signal"] == "LONG"]
            short_candidates = [(n, d) for n, d in candidates if d["signal"] == "SHORT"]

            if long_candidates and short_candidates:
                # CONFLICT: opposing signals for same symbol
                best_long = max(long_candidates, key=lambda x: strategy_score(x[0]))
                best_short = max(short_candidates, key=lambda x: strategy_score(x[0]))
                long_score = strategy_score(best_long[0])
                short_score = strategy_score(best_short[0])

                if long_score > short_score:
                    winner_name, winner_data = best_long
                    logger.warning(
                        "CONFLICT ARBITRATION: %s — %s(LONG score=%.1f) vs %s(SHORT score=%.1f) → %s WINS",
                        sym, best_long[0], long_score, best_short[0], short_score, winner_name,
                    )
                elif short_score > long_score:
                    winner_name, winner_data = best_short
                    logger.warning(
                        "CONFLICT ARBITRATION: %s — %s(LONG score=%.1f) vs %s(SHORT score=%.1f) → %s WINS",
                        sym, best_long[0], long_score, best_short[0], short_score, winner_name,
                    )
                else:
                    # Tie — skip both, send HOLD
                    logger.warning(
                        "CONFLICT ARBITRATION: %s — %s(LONG score=%.1f) vs %s(SHORT score=%.1f) → TIE, ALL HELD",
                        sym, best_long[0], long_score, best_short[0], short_score,
                    )
                    continue

                signals[winner_name] = winner_data
            else:
                # Same direction from multiple strategies — pick highest score
                best = max(candidates, key=lambda x: strategy_score(x[0]))
                signals[best[0]] = best[1]

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
            entry = {"signals": s.get("signals_count", 0), "status": s.get("status", "?")}
            if "metrics" in s:
                m = s["metrics"]
                entry["return_pct"] = m.get("total_return_pct", 0)
                entry["sharpe"] = m.get("sharpe_ratio", 0)
                entry["win_rate"] = m.get("win_rate", 0)
                entry["trades"] = m.get("total_trades", 0)
                entry["max_dd"] = m.get("max_drawdown_pct", 0)
            strat_summary[name] = entry
        merge_state("athena", {"status": "ok", "strategies": strat_summary})

        risk = load_json(os.path.join(STATE_DIR, "risk_check.json"))
        merge_state("guardian", {"status": "ok", "balance": risk.get("balance",0), "risk_level": risk.get("risk_level","?"), "positions": risk.get("positions_count",0)})

        sig = load_json(os.path.join(STATE_DIR, "signals.json"))
        merge_state("mercury", {"status": "ok", "signals_active": len(sig.get("signals",{})), "signals": sig.get("signals",{})})

        # Prometheus: pull real backtest metrics, never hardcode PnL
        prom_state = {"status": "active", "strategies": {}}
        for name, s in bt.get("strategies", {}).items():
            if "metrics" not in s:
                continue
            m = s["metrics"]
            prom_state["strategies"][name] = {
                "return_pct": m.get("total_return_pct", 0),
                "sharpe": m.get("sharpe_ratio", 0),
                "win_rate": m.get("win_rate", 0),
                "trades": m.get("total_trades", 0),
                "max_dd": m.get("max_drawdown_pct", 0),
            }
        # Also preserve any Prometheus-specific fields from existing state
        existing_prom = load_json(os.path.join(STATE_DIR, "prometheus.json"))
        for key in ("dsr_implemented", "walk_forward_implemented", "anti_overfitting_run", "wf_findings", "next"):
            if key in existing_prom:
                prom_state[key] = existing_prom[key]
        # Clean up stale hardcoded fake fields (replaced by strategies dict)
        prom_state["dgt_deployed"] = None
        prom_state["dgt_btc_pnl"] = None
        prom_state["dgt_eth_pnl"] = None
        merge_state("prometheus", prom_state)
    except Exception as e:
        logger.error("State sync error: %s", e)

    # Regenerate dashboard
    try:
        import subprocess, sys
        subprocess.run([sys.executable, "generate_dashboard.py"], capture_output=True, timeout=30)
    except: pass

    # Post engine heartbeat summary to bulletin (keeps bulletin fresh every tick)
    try:
        from datetime import datetime, timezone
        risk = load_json(os.path.join(STATE_DIR, "risk_check.json"))
        bt = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
        ts = datetime.now(timezone.utc).strftime("%m-%d %H:%M")

        # Build strategy performance summary
        strat_lines = []
        for name, s in bt.get("strategies", {}).items():
            m = s.get("metrics", {})
            if m:
                ret = m.get("total_return_pct", 0)
                sr = m.get("sharpe_ratio", 0)
                wr = m.get("win_rate", 0)
                tr = m.get("total_trades", 0)
                sign = "🟢" if ret > 0 else "🔴"
                strat_lines.append(f"| {name} | {sign} {ret:+.2f}% | SR={sr:+.2f} | WR={wr:.1f}% | {tr}t |")
            else:
                strat_lines.append(f"| {name} | ⚪ no metrics | — | — | — |")

        strat_table = "\n".join(strat_lines) if strat_lines else "| — | — | — | — | — |"

        pos_info = "无持仓"
        positions = risk.get("positions", [])
        if positions:
            p = positions[0]
            pnl = p.get("unrealized_pnl", 0)
            pnl_sign = "🟢" if pnl >= 0 else "🔴"
            pos_info = f"{p.get('side','?')} {p.get('symbol','?')} {p.get('contracts','?')} @ {p.get('entry_price','?')} | uPNL {pnl_sign} {pnl:+.2f}"

        bulletin_entry = (
            f"\n---\n"
            f"### {ts} — Engine ♡ | 风控 {risk.get('risk_level','?')} | {pos_info}\n\n"
            f"| 策略 | 收益 | 夏普 | 胜率 | 笔数 |\n"
            f"|------|------|------|------|------|\n"
            f"{strat_table}\n"
        )
        bulletin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "bulletin.md")
        with open(bulletin_path, "a") as bf:
            bf.write(bulletin_entry)
    except Exception:
        pass  # bulletin is non-critical

    # Sync trades_log DB with exchange (auto-reconcile each heartbeat)
    sync_trades_db()


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
