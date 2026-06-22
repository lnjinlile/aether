#!/usr/bin/env python3
"""
Aether 自动化引擎 — 后台持续运行所有机械性工作

回测、风控检查、信号执行全部自动化。
专员只读取结果，做判断和决策。
"""
import sys, os, json, time, logging, warnings
from datetime import datetime, timezone

# Suppress sklearn feature-name warnings (LightGBM 4.6.0 bug)
warnings.filterwarnings('ignore', message='X does not have valid feature names')
warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv; load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from data.db import get_market_db

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

        db = get_market_db(db_path)
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
    """Run backtests on all enabled strategies using BacktestEngine, write results.
    
    Cached: skips re-computation if results are < 30 min old (backtests are expensive:
    21 strategies × data load × signal generation × metrics = ~30-60s per run).
    Strategy evaluation doesn't need 5-min granularity — signals stay fresh via run_signal_check.
    """
    try:
        # Cache check: skip if results are fresh (< 30 min)
        results_path = os.path.join(STATE_DIR, "backtest_results.json")
        if os.path.exists(results_path):
            mtime = os.path.getmtime(results_path)
            if time.time() - mtime < 1800:  # 30 minutes
                logger.debug("Backtest results are fresh (%.0f min old), skipping.", (time.time() - mtime) / 60)
                return

        # Load existing results to preserve metrics for disabled strategies
        # (Prometheus persona writes top-level metrics that engine would otherwise clobber)
        existing_results = {}
        if os.path.exists(results_path):
            try:
                with open(results_path) as f:
                    existing = json.load(f)
                existing_results = existing.get("strategies", {})
            except Exception:
                pass

        from strategy.manager import StrategyManager
        from data.storage import MarketStorage
        from backtest.engine import BacktestEngine
        from backtest.signal_gen import (
            trendfollow_signals, rsi_mr_signals,
            dynamic_grid_signals, ma_cross_signals,
            bband_rsi_signals, donchian_mr_signals,
            vol_breakout_signals, supertrend_signals,
            macd_crossover_signals,
            adx_trend_signals, momentum_signals,
            trend_pullback_signals,
        )
        import numpy as np
        import pandas as pd
        import yaml

        # Load ALL strategies from YAML to read config metadata (only backtest enabled ones)
        yaml_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "strategies.yaml")
        with open(yaml_path, "r") as f:
            strat_cfg = yaml.safe_load(f)
        all_strategies = strat_cfg.get("strategies", [])

        storage = MarketStorage()
        engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

        # Cache dataframes by (symbol, timeframe) to avoid redundant DB loads
        # (multiple strategies sharing the same (sym, tf) were loading identical data)
        _df_cache = {}

        results = {}
        for s in all_strategies:
            name = s["name"]
            enabled = s.get("enabled", True)
            p = s.get("params", {})
            sym = p.get("symbols", [None])[0]
            tf = p.get("timeframes", [None])[0]
            strategy_type = s["class"].split(".")[-1]

            # Skip disabled strategies — preserve existing metrics but enforce correct verdict
            # AUDIT-040: Don't blindly preserve "LIVE" verdict for disabled strategies.
            # Use performance data from existing results to determine correct verdict.
            if not enabled:
                existing = existing_results.get(name, {})
                # Derive verdict from actual performance data, not from stale cached verdict
                existing_verdict = existing.get("verdict", "NOT_EVALUATED")
                existing_return = existing.get("total_return_pct", None)
                existing_sharpe = existing.get("sharpe_ratio", None)
                existing_wr = existing.get("win_rate", None)
                # If strategy is disabled and has performance data, override stale LIVE verdict
                if existing_verdict == "LIVE" and existing_return is not None:
                    ret = existing_return
                    sr = existing_sharpe or 0
                    wr = existing_wr or 0
                    if ret <= 0 or sr <= 0.3 or wr <= 40:
                        existing_verdict = "RETIRED"
                    elif sr <= 0.5:
                        existing_verdict = "PAUSED"
                results[name] = {
                    "status": "disabled",
                    "enabled": False,
                    "symbol": sym or "unknown",
                    "timeframe": tf or "unknown",
                    # Preserve Prometheus-written metrics (top-level)
                    "total_return_pct": existing.get("total_return_pct", None),
                    "sharpe_ratio": existing.get("sharpe_ratio", None),
                    "max_drawdown_pct": existing.get("max_drawdown_pct", None),
                    "win_rate": existing.get("win_rate", None),
                    "total_trades": existing.get("total_trades", 0),
                    "backtest_period": existing.get("backtest_period", "pending"),
                    "verdict": existing_verdict,
                    "retired_reason": existing.get("retired_reason", None),
                }
                continue

            if not sym or not tf:
                results[name] = {"status": "no_config", "error": "Missing symbols/timeframes"}
                continue

            try:
                cache_key = (sym, tf)
                if cache_key not in _df_cache:
                    _df_cache[cache_key] = storage.load_klines(sym, tf)
                df_raw = _df_cache[cache_key]
                if df_raw is None or df_raw.empty or len(df_raw) < 50:
                    results[name] = {
                        "status": "no_data",
                        "symbol": sym, "timeframe": tf,
                        "data_rows": len(df_raw) if df_raw is not None else 0,
                        "error": f"Insufficient data ({len(df_raw) if df_raw is not None else 0} bars) for {sym} {tf}",
                    }
                    continue

                # ── Copy from cache & filter to last 90 days ──
                df = df_raw.copy()
                df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                df.set_index('open_time', inplace=True)
                df.sort_index(inplace=True)
                cutoff = df.index[-1] - pd.Timedelta(days=90)
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
                    elif strategy_type == "BBandMeanReversion":
                        signals = bband_rsi_signals(
                            df,
                            bb_period=p.get("bb_period", 20),
                            bb_std=p.get("bb_std", 2.5),
                            rsi_period=p.get("rsi_period", 14),
                            rsi_oversold=p.get("rsi_oversold", 30),
                            rsi_overbought=p.get("rsi_overbought", 70),
                            stop_loss_pct=p.get("stop_loss_pct", 0.02),
                            take_profit_pct=p.get("take_profit_pct", 0.05),
                            cooldown_bars=p.get("cooldown_bars", 3),
                        )
                    elif strategy_type == "DonchianMRStrategy":
                        signals = donchian_mr_signals(
                            df,
                            donchian_period=p.get("donchian_period", 20),
                            rsi_period=p.get("rsi_period", 14),
                            oversold=p.get("oversold", 25),
                            overbought=p.get("overbought", 75),
                            exit_level=p.get("exit_level", 50),
                            stop_loss_pct=p.get("stop_loss_pct", 0.02),
                            take_profit_pct=p.get("take_profit_pct", 0.04),
                            cooldown_bars=p.get("cooldown_bars", 5),
                        )
                    elif strategy_type == "SupertrendStrategy":
                        signals = supertrend_signals(
                            df,
                            atr_period=p.get("atr_period", 10),
                            atr_mult=p.get("atr_mult", 3.0),
                            cooldown_bars=p.get("cooldown_bars", 3),
                        )
                    elif strategy_type == "VolBreakoutStrategy":
                        signals = vol_breakout_signals(
                            df,
                            atr_period=p.get("atr_period", 20),
                            atr_mult=p.get("atr_mult", 2.0),
                            ema_period=p.get("ema_period", 50),
                            atr_sl_mult=p.get("atr_sl_mult", 1.5),
                            atr_tp_mult=p.get("atr_tp_mult", 3.0),
                            cooldown_bars=p.get("cooldown_bars", 5),
                            volume_filter=p.get("volume_filter", True),
                            vol_ma_period=p.get("vol_ma_period", 20),
                        )
                    elif strategy_type == "MACDCrossoverStrategy":
                        signals = macd_crossover_signals(
                            df,
                            fast_period=p.get("fast_period", 12),
                            slow_period=p.get("slow_period", 26),
                            signal_period=p.get("signal_period", 9),
                            stop_loss_pct=p.get("stop_loss_pct", 0.02),
                            take_profit_pct=p.get("take_profit_pct", 0.04),
                            cooldown_bars=p.get("cooldown_bars", 5),
                        )
                    elif strategy_type == "ADXTrendStrategy":
                        signals = adx_trend_signals(
                            df,
                            adx_period=p.get("adx_period", 14),
                            adx_threshold=p.get("adx_threshold", 25),
                            adx_exit=p.get("adx_exit", 20),
                            ema_period=p.get("ema_period", 50),
                            atr_period=p.get("atr_period", 14),
                            atr_sl_mult=p.get("atr_sl_mult", 2.0),
                            atr_tp_mult=p.get("atr_tp_mult", 4.0),
                            cooldown_bars=p.get("cooldown_bars", 3),
                        )
                    elif strategy_type == "MomentumStrategy":
                        signals = momentum_signals(
                            df,
                            fast_ema=p.get("fast_ema", 12),
                            slow_ema=p.get("slow_ema", 26),
                            signal_period=p.get("signal_period", 9),
                            atr_period=p.get("atr_period", 14),
                            atr_sl_mult=p.get("atr_sl_mult", 2.0),
                            atr_tp_mult=p.get("atr_tp_mult", 3.5),
                        )
                    elif strategy_type == "TrendPullback":
                        signals = trend_pullback_signals(
                            df,
                            ema_period=p.get("ema_period", 100),
                            atr_period=p.get("atr_period", 14),
                            atr_sl_mult=p.get("atr_sl_mult", 1.5),
                            atr_tp_mult=p.get("atr_tp_mult", 3.0),
                            cooldown_bars=p.get("cooldown_bars", 5),
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
                    # Top-level flattened metrics (for Hermes AUDIT verification)
                    "total_return_pct": m["total_return_pct"],
                    "sharpe_ratio": m["sharpe_ratio"],
                    "max_drawdown_pct": m["max_drawdown_pct"],
                    "win_rate": m["win_rate"],
                    "total_trades": m["total_trades"],
                    "profit_factor": m["profit_factor"],
                    "backtest_period": "90d",
                    "verdict": existing_results.get(name, {}).get("verdict", "PAPER"),
                    # Nested detailed metrics (for backward compat)
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

        # AUDIT-048 FIX: Preserve existing backtest results that have MORE trades
        # (indicating a more comprehensive backtest like Prometheus 365d sweep).
        # Engine's 90d backtest must not overwrite longer-window authoritative results.
        existing_bt = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
        existing_strategies = existing_bt.get("strategies", {}) if isinstance(existing_bt, dict) else {}
        for name, new_entry in list(results.items()):
            old_entry = existing_strategies.get(name, {})
            old_trades = old_entry.get("total_trades") or 0
            new_trades = new_entry.get("total_trades") or 0
            # Preserve old entry if it has strictly more trades (longer/better backtest)
            # AND the new entry doesn't have a status upgrade (e.g. disabled→ok)
            if old_trades > new_trades and new_trades > 0:
                logger.info(
                    "AUDIT-048 PRESERVED: %s (old=%d trades > new=%d trades) — keeping longer backtest",
                    name, old_trades, new_trades
                )
                # Keep old entry but update enabled/status fields from current config
                old_entry["enabled"] = new_entry.get("enabled", old_entry.get("enabled", False))
                old_entry["status"] = new_entry.get("status", old_entry.get("status", "?"))
                results[name] = old_entry
            elif old_trades > 0 and new_trades == 0:
                logger.info(
                    "AUDIT-048 PRESERVED: %s (old=%d trades > new=0) — new backtest produced no trades, keeping old",
                    name, old_trades
                )
                old_entry["enabled"] = new_entry.get("enabled", old_entry.get("enabled", False))
                old_entry["status"] = new_entry.get("status", old_entry.get("status", "?"))
                results[name] = old_entry

        write_json("backtest_results.json", {"strategies": results})
        # Summary log
        ok_count = sum(1 for r in results.values() if r.get("status") == "ok")
        logger.info("Backtests: %d/%d strategies evaluated with real metrics", ok_count, len(results))

        # ═══ AUDIT-040: Cross-file consistency guard ═══
        # Verify no disabled strategy leaked through with verdict=LIVE.
        # This catches engine.py bugs and stale cache poisoning at write time.
        leaked = []
        for name, r in results.items():
            if not r.get("enabled", False) and r.get("verdict") == "LIVE":
                leaked.append(name)
        if leaked:
            logger.error("AUDIT-040 CONSISTENCY VIOLATION: %d disabled strategies have verdict=LIVE: %s",
                         len(leaked), ", ".join(leaked))
        else:
            live_count = sum(1 for r in results.values() if r.get("verdict") == "LIVE")
            logger.info("AUDIT-040 guard: %d LIVE strategies, 0 leaked — consistency OK", live_count)
    except Exception as e:
        logger.error("Backtest error: %s", e)
        write_json("backtest_results.json", {"error": str(e), "status": "error"})


def run_risk_check():
    """Check account balance, positions, risk metrics.

    OPTIMIZED: Reuses data from fetch_live_exchange() to avoid
    redundant API calls (was calling get_balance/get_positions/get_open_orders
    twice per engine cycle — once here, once in fetch_live_exchange).
    """
    try:
        # Reuse live_exchange.json data (already fetched by fetch_live_exchange)
        live_path = os.path.join(STATE_DIR, "live_exchange.json")
        if not os.path.exists(live_path):
            write_json("risk_check.json", {"error": "live_exchange.json not found", "status": "error"})
            return

        live = load_json(live_path)
        if "error" in live:
            # Live exchange fetch failed — retain last valid risk check
            state_path = os.path.join(STATE_DIR, "risk_check.json")
            if os.path.exists(state_path):
                existing = load_json(state_path)
                if existing.get("balance", 0) > 0:
                    write_json("risk_check.json", existing)
                    return
            write_json("risk_check.json", {"error": live.get("error", "upstream failure"), "status": "error"})
            return

        bal = live.get("balance", {})
        positions = live.get("positions", [])
        orders_count = live.get("open_orders", 0)

        bal_value = bal.get("balance", 0) if isinstance(bal, dict) else (bal if isinstance(bal, (int, float)) else 0)
        available = bal.get("available", bal_value) if isinstance(bal, dict) else bal_value
        upnl = bal.get("unrealized_pnl", bal.get("unrealizedPnl", 0)) if isinstance(bal, dict) else 0.0

        # AUDIT-009: validate
        if bal_value == 0:
            state_path = os.path.join(STATE_DIR, "risk_check.json")
            if os.path.exists(state_path):
                existing = load_json(state_path)
                if existing.get("balance", 0) > 0:
                    write_json("risk_check.json", existing)
                    logger.warning("Risk check: live_exchange returned balance=0, retained last valid state")
                    return
            write_json("risk_check.json", {"error": "balance=0 from live_exchange", "status": "error"})
            return

        # Risk metrics
        real_positions = [p for p in positions if abs(float(p.get("contracts", p.get("positionAmt", 0)))) > 0]
        total_notional = sum(abs(float(p.get("notional", p.get("contracts", 0)) * p.get("mark_price", p.get("markPrice", 0)))) for p in real_positions) if real_positions else 0
        position_pct = total_notional / bal_value * 100 if bal_value > 0 else 0

        # Liq alerts (enriched positions already have liq_distance_pct)
        alerts = []
        for p in positions:
            liq_dist = p.get("liq_distance_pct", 999)
            if liq_dist < 10:
                alerts.append({"level": "warning", "msg": f'{p.get("symbol","?")} liq distance {liq_dist:.1f}%'})
            if liq_dist < 5:
                alerts.append({"level": "critical", "msg": f'{p.get("symbol","?")} LIQUIDATION RISK {liq_dist:.1f}%'})

        risk_level = "critical" if any(a["level"] == "critical" for a in alerts) else \
                     "warning" if alerts else "normal"

        write_json("risk_check.json", {
            "status": "ok",
            "balance": bal_value,
            "available": available,
            "unrealized_pnl": upnl,
            "positions_count": len(positions),
            "open_orders": orders_count,
            "total_notional": total_notional,
            "position_pct": round(position_pct, 1),
            "risk_level": risk_level,
            "alerts": alerts,
            "positions": positions,
        })
        logger.info("Risk check: %.2f, %d positions, risk=%s", bal_value, len(positions), risk_level)
    except Exception as e:
        logger.error("Risk check error: %s", e)
        state_path = os.path.join(STATE_DIR, "risk_check.json")
        if os.path.exists(state_path):
            existing = load_json(state_path)
            if existing.get("balance", 0) > 0:
                write_json("risk_check.json", existing)
                return
        write_json("risk_check.json", {"error": str(e), "status": "error"})


# Peristent strategy manager — survives across engine ticks so strategy
# state (regime, cooldown, bars_since_last_trade) is preserved.
_persistent_mgr = None
_yaml_mtime = 0          # mtime of last-read strategies.yaml
_yaml_enabled_names = set()  # cached set of enabled strategy names

def run_signal_check():
    """Generate trading signals from active strategies."""
    global _persistent_mgr, _yaml_mtime, _yaml_enabled_names
    try:
        from strategy.manager import StrategyManager
        from data.collector import BinanceDataCollector
        from config.settings import get_config
        import numpy as np

        cfg = get_config()
        collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)

        # ── Reload strategies.yaml only when file changes (mtime check) ──
        _yaml_path = "config/strategies.yaml"
        _cur_mtime = os.path.getmtime(_yaml_path)
        _reload = (_persistent_mgr is None or _cur_mtime != _yaml_mtime)

        if _reload:
            import yaml as _yaml
            with open(_yaml_path) as _f:
                _yaml_cfg = _yaml.safe_load(_f)
            _yaml_enabled_names = {s['name'] for s in _yaml_cfg.get('strategies', []) if s.get('enabled')}
            _yaml_mtime = _cur_mtime
            _persistent_mgr = StrategyManager.load_from_yaml(_yaml_path)
            logger.info("Strategies reloaded from YAML (mtime changed): %d enabled", len(_yaml_enabled_names))

            # ═══ AUDIT-047: Regression guard — detect re-enabled disabled strategies ═══
            # Cross-reference newly-enabled strategies against athena.json verdicts.
            # If a strategy was previously disabled (verdict=PAPER/DO_NOT_ENABLE/RETIRED)
            # and appears as enabled:true in YAML, auto-correct it back to enabled:false.
            # This catches optimization scripts accidentally flipping the enabled flag.
            _athena_path = os.path.join(STATE_DIR, "athena.json")
            if os.path.exists(_athena_path):
                _athena = load_json(_athena_path)
                _athena_strats = _athena.get("strategies", {})
                _re_enabled = []
                for _name in _yaml_enabled_names:
                    _v = _athena_strats.get(_name, {}).get("verdict", "")
                    if _v in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED", "NOT_EVALUATED"):
                        _re_enabled.append(f"{_name}(verdict={_v})")
                if _re_enabled:
                    # Auto-fix: flip enabled back to false in strategies.yaml
                    import yaml as _yaml2
                    _fixed = []
                    for _s in _yaml_cfg.get("strategies", []):
                        _sn = _s.get("name", "")
                        _sv = _athena_strats.get(_sn, {}).get("verdict", "")
                        if _sn in _yaml_enabled_names and _sv in ("PAPER", "DO_NOT_ENABLE", "RETIRED", "PAUSED", "NOT_EVALUATED"):
                            _s["enabled"] = False
                            _fixed.append(_sn)
                    if _fixed:
                        with open(_yaml_path, "w") as _f:
                            _yaml2.dump(_yaml_cfg, _f, default_flow_style=False, sort_keys=False)
                        _yaml_enabled_names = {s['name'] for s in _yaml_cfg.get('strategies', []) if s.get('enabled')}
                        _yaml_mtime = os.path.getmtime(_yaml_path)
                        _persistent_mgr = StrategyManager.load_from_yaml(_yaml_path)
                        logger.critical(
                            "AUDIT-047 AUTO-FIX: Re-disabled %d strategies that were re-enabled in YAML: %s",
                            len(_fixed), ", ".join(_fixed)
                        )

        mgr = _persistent_mgr
        _enabled_names = _yaml_enabled_names

        signals = {}
        # Track signals per symbol for conflict detection
        symbol_signals = {}  # symbol -> list of (name, signal_dict)
        
        # Cache fetched klines by (symbol, timeframe) to avoid redundant API calls
        # when multiple strategies share the same symbol/timeframe
        _klines_cache = {}

        for name in mgr.get_active_strategies():
            if name not in _enabled_names:
                continue  # Skip disabled strategies (guard against stale persistent manager)
            strat = mgr.get_strategy(name)
            if not strat: continue
            sym = strat.symbols[0]
            tf = strat.timeframes[0]

            try:
                cache_key = (sym, tf)
                if cache_key not in _klines_cache:
                    _klines_cache[cache_key] = collector.fetch_current_klines(sym, tf, 300)
                df = _klines_cache[cache_key]
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
            """Score a strategy for priority: positive metrics = higher score.
            
            Supports both nested 'metrics' sub-dict (newer format) and flat top-level
            fields (legacy format from Prometheus sweeps). Returns -999.0 for
            NOT_EVALUATED / error strategies to ensure they lose in arbitration.
            """
            s = bt_strategies.get(name, {})
            if s.get("status") == "error" or s.get("verdict") == "NOT_EVALUATED":
                return -999.0
            # Try nested metrics first (engine.py format), fall back to flat fields (Prometheus sweep format)
            m = s.get("metrics", {})
            if m:
                return m.get("sharpe_ratio", 0) * 10 + m.get("total_return_pct", 0) / 10
            # Flat format: top-level sharpe_ratio / total_return_pct
            sr = s.get("sharpe_ratio") or 0
            ret = s.get("total_return_pct") or 0
            if sr == 0 and ret == 0:
                return -999.0  # no real metrics
            return sr * 10 + ret / 10

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
    """Pull live account data from Binance testnet.

    AUDIT-009 FIX: When API is rate-limited, Binance returns balance=0
    and ghost positions. Detect this and retain the last valid state
    file instead of overwriting it with zeros.
    """
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

        # ── AUDIT-009: Validate API data before overwriting ──
        bal_value = bal.get("balance", 0)
        if bal_value == 0:
            # API returned garbage (rate-limit). Retain last valid state.
            state_path = os.path.join(STATE_DIR, "live_exchange.json")
            if os.path.exists(state_path):
                try:
                    with open(state_path) as f:
                        existing = json.load(f)
                    existing_bal = existing.get("balance", {})
                    if isinstance(existing_bal, dict):
                        if existing_bal.get("balance", 0) > 0:
                            write_json("live_exchange.json", existing)
                            logger.warning(
                                "Live exchange: API returned balance=0, retained last valid "
                                "state (balance=%.2f)",
                                existing_bal.get("balance", 0),
                            )
                            return
                    elif isinstance(existing_bal, (int, float)) and existing_bal > 0:
                        write_json("live_exchange.json", existing)
                        logger.warning(
                            "Live exchange: API returned balance=0, retained last valid "
                            "state (balance=%.2f)", existing_bal,
                        )
                        return
                except Exception:
                    pass

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
        # On exception, retain last valid state if available
        state_path = os.path.join(STATE_DIR, "live_exchange.json")
        if os.path.exists(state_path):
            try:
                with open(state_path) as f:
                    existing = json.load(f)
                existing_bal = existing.get("balance", {})
                if isinstance(existing_bal, dict) and existing_bal.get("balance", 0) > 0:
                    write_json("live_exchange.json", existing)
                    return
                elif isinstance(existing_bal, (int, float)) and existing_bal > 0:
                    write_json("live_exchange.json", existing)
                    return
            except Exception:
                pass
        write_json("live_exchange.json", {"error": str(e), "status": "error"})


def sync_agent_states():
    """Update agent state files — MERGE with existing, preserve tasks."""
    try:
        def merge_state(agent, updates):
            existing = load_json(os.path.join(STATE_DIR, f"{agent}.json"))
            existing.update(updates)
            write_json(f"{agent}.json", existing)

        def _normalize_guardian_state():
            """Post-merge normalization: ensure ALL position/order fields self-consistent.
            Fixes AUDIT-026 root cause: conflicting writes by engine vs guardian persona
            leave position fields internally contradictory (e.g. positions=1, positions_count=0).
            After this, every position-related key points to ONE canonical value."""
            gpath = os.path.join(STATE_DIR, "guardian.json")
            if not os.path.exists(gpath):
                return
            gstate = load_json(gpath)
            # Canonical source: positions_count (set by this function from risk_check)
            pos_cnt = gstate.get("positions_count", gstate.get("positions", 0))
            orders = gstate.get("open_orders", 0)
            # Force ALL position fields to same value
            gstate["positions"] = pos_cnt
            gstate["positions_count"] = pos_cnt
            gstate["effective_positions"] = pos_cnt
            gstate["open_orders"] = orders
            # Force account sub-fields to match
            if "account" not in gstate:
                gstate["account"] = {}
            gstate["account"]["positions"] = pos_cnt
            gstate["account"]["open_orders"] = orders
            write_json("guardian.json", gstate)

        pipe = load_json(os.path.join(STATE_DIR, "pipeline.json"))

        # Collect PID info
        import subprocess as _sp
        def _find_pid(cmd_pattern):
            try:
                r = _sp.run(["pgrep", "-f", cmd_pattern], capture_output=True, text=True, timeout=5)
                pids = [int(x) for x in r.stdout.strip().split("\n") if x]
                return pids[0] if pids else None
            except Exception:
                return None

        oracle_updates = {
            "status": pipe.get("status", "unknown"),
            "data_fresh": True,
            "last_pipeline": pipe.get("last_run", ""),
            "pipeline_pid": _find_pid("python3 pipeline.py"),
            "data_ext_pid": _find_pid("python3 data_ext.py"),
            "engine_pid": os.getpid(),
        }
        # ALWAYS write standalone engine PID file (avoids AUDIT-028 race condition
        # where prometheus persona overwrites prometheus.json losing the PID)
        pid_path = os.path.join(STATE_DIR, "engine.pid")
        with open(pid_path, "w") as pf:
            pf.write(str(os.getpid()))

        # Count klines (use shared db helper for WAL + busy_timeout)
        try:
            db = get_market_db()
            oracle_updates["klines_total"] = db.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
            db.close()
        except Exception:
            pass

        # Sync strategies_enabled from strategies.yaml
        # Also compute _disabled_in_yaml for AUDIT-047 guard (avoid double YAML read)
        _disabled_in_yaml = set()
        try:
            import yaml
            cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "strategies.yaml")
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            strategies = cfg.get("strategies", [])
            enabled = [s["name"] for s in strategies if s.get("enabled", False)]
            disabled = [s["name"] for s in strategies if not s.get("enabled", False)]
            _disabled_in_yaml = set(disabled)
            oracle_updates["strategies_enabled"] = enabled
            oracle_updates["strategies_disabled"] = len(disabled)
        except Exception:
            pass

        merge_state("oracle", oracle_updates)

        bt = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
        strat_summary = {}
        for name, s in bt.get("strategies", {}).items():
            entry = {"signals": s.get("signals_count", 0), "status": s.get("status", "?")}
            # Support both nested (canonical metrics) and top-level (legacy) fields.
            # Prefer nested metrics when available — top-level can be stale duplicates
            # (both top-level and nested come from same engine run — identical)
            m = s.get("metrics", {})
            has_metrics = m and m.get("total_trades", 0) > 0
            top_trades = s.get("total_trades") or 0
            # Use nested metrics if they exist and have >= trades (more authoritative)
            if has_metrics and m.get("total_trades", 0) >= top_trades:
                entry["return_pct"] = m.get("total_return_pct", 0)
                entry["sharpe"] = m.get("sharpe_ratio", 0)
                entry["win_rate"] = m.get("win_rate", 0)
                entry["trades"] = m.get("total_trades", 0)
                entry["max_dd"] = m.get("max_drawdown_pct", 0)
            else:
                entry["return_pct"] = s.get("total_return_pct") or m.get("total_return_pct", 0)
                entry["sharpe"] = s.get("sharpe_ratio") or m.get("sharpe_ratio", 0)
                entry["win_rate"] = s.get("win_rate") or m.get("win_rate", 0)
                entry["trades"] = s.get("total_trades") or m.get("total_trades", 0)
                entry["max_dd"] = s.get("max_drawdown_pct") or m.get("max_drawdown_pct", 0)
            entry["verdict"] = s.get("verdict", m.get("verdict", ""))
            strat_summary[name] = entry
        # Preserve existing verdicts from athena.json that were manually set
        # (e.g. Athena/Prometheus promote to LIVE, PAUSE, RETIRE — engine must not overwrite)
        #
        # AUDIT-047 guard: Never preserve a LIVE verdict for a strategy that is
        # disabled in strategies.yaml. This prevents the pattern where a strategy
        # is re-enabled by accident (YAML flip), promoted by Athena, and the
        # engine blindly preserves the now-invalid LIVE verdict.
        existing_athena = load_json(os.path.join(STATE_DIR, "athena.json"))
        # _disabled_in_yaml already computed in oracle section above (avoids double YAML read)
        for name in strat_summary:
            existing_verdict = existing_athena.get("strategies", {}).get(name, {}).get("verdict", "")
            if existing_verdict and existing_verdict not in ("", "?"):
                # AUDIT-047: Block LIVE verdict for disabled strategies
                if existing_verdict == "LIVE" and name in _disabled_in_yaml:
                    logger.warning(
                        "AUDIT-047 BLOCKED: %s verdict=LIVE but strategy is disabled in YAML. "
                        "Downgrading to PAPER.", name
                    )
                    strat_summary[name]["verdict"] = "PAPER"
                else:
                    strat_summary[name]["verdict"] = existing_verdict
        merge_state("athena", {"status": "ok", "strategies": strat_summary})

        risk = load_json(os.path.join(STATE_DIR, "risk_check.json"))
        live_ex = load_json(os.path.join(STATE_DIR, "live_exchange.json"))
        # Extract live balance (nested or flat)
        live_bal = live_ex.get("balance", {})
        if isinstance(live_bal, dict):
            balance_val = live_bal.get("balance", risk.get("balance", 0))
            available_val = live_bal.get("available", balance_val)
            upnl_val = live_bal.get("unrealized_pnl", 0)
        else:
            balance_val = risk.get("balance", 0)
            available_val = balance_val
            upnl_val = 0.0
        pos_count = risk.get("positions_count", 0)
        orders_count = risk.get("open_orders", 0)
        # Comprehensive guardian merge — update ALL position/order fields atomically
        # to prevent internal contradictions (AUDIT-026 root cause fix)
        guardian_updates = {
            "status": "ok",
            "balance": balance_val,
            "available": available_val,
            "risk_level": risk.get("risk_level", "?"),
            "risk_module": "ok" if risk.get("status") == "ok" else "degraded",
            "positions": pos_count,
            "positions_count": pos_count,
            "effective_positions": pos_count,
            "open_orders": orders_count,
            "total_notional": risk.get("total_notional", 0),
            "margin_used": 0 if pos_count == 0 else (balance_val - available_val),
            "unrealized_pnl": upnl_val,
            "account": {
                "balance": balance_val,
                "available": available_val,
                "margin_used": 0 if pos_count == 0 else (balance_val - available_val),
                "margin_pct": 0.0,
                "unrealized_pnl": upnl_val,
                "positions": pos_count,
                "open_orders": orders_count,
            },
        }
        merge_state("guardian", guardian_updates)
        # Post-merge normalization: ensure ALL position-related fields are consistent
        _normalize_guardian_state()

        sig = load_json(os.path.join(STATE_DIR, "signals.json"))
        merge_state("mercury", {"status": "ok", "signals_active": len(sig.get("signals",{})), "signals": sig.get("signals",{})})

        # Prometheus: pull real backtest metrics, never hardcode PnL
        prom_state = {"status": "active", "strategies": {}, "engine_pid": os.getpid()}
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
        # Preserve Prometheus-specific metadata fields from existing state
        existing_prom = load_json(os.path.join(STATE_DIR, "prometheus.json"))
        for key in ("dsr_implemented", "walk_forward_implemented", "anti_overfitting_run", "wf_findings", "next",
                     "recommendation", "live_validation", "ml_alpha_status", "ml_validation",
                     "regime_model", "regime_monitor", "regime_classifier", "last_optimization",
                     "strategy_landscape_20260622", "rsi_mr_eth_live_confirmed",
                     "donchian_mr_eth_wf_validation", "donchian_mr_eth_365d",
                     "donchian_mr_btc_optimized", "portfolio_correlation"):
            if key in existing_prom:
                prom_state[key] = existing_prom[key]
        # NOTE: strategies metrics are ENGINE-DERIVED (from backtest_results.json).
        # Prometheus-generated metrics live in rsi_mr_eth_live_confirmed etc., NOT
        # in the strategies section which is overwritten by every engine tick.
        # Do NOT preserve existing prometheus.json strategies metrics — the engine
        # is the single source of truth for backtest performance data.
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
        # Truncate to last 500 lines to prevent unbounded growth
        with open(bulletin_path, "r") as bf:
            lines = bf.readlines()
        if len(lines) > 500:
            with open(bulletin_path, "w") as bf:
                bf.writelines(lines[-500:])
    except Exception:
        pass  # bulletin is non-critical

    # Sync trades_log DB with exchange (auto-reconcile each heartbeat)
    sync_trades_db()


def load_json(path):
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except: return {}


# ── Pipeline health watchdog ──
_PIPELINE_RESTART_COOLDOWN = 0  # epoch of last restart, 0 = never
_PIPELINE_RESTART_COUNT = 0


def check_pipeline_health():
    """Monitor pipeline liveness. Restart if stalled >10 min."""
    global _PIPELINE_RESTART_COOLDOWN, _PIPELINE_RESTART_COUNT
    now = time.time()
    pipeline_state = os.path.join(STATE_DIR, "pipeline.json")

    if not os.path.exists(pipeline_state):
        logger.warning("Pipeline state file missing — pipeline may not be running")
        return

    try:
        with open(pipeline_state) as f:
            data = json.load(f)
        last_run = data.get("last_run", "")
        if not last_run:
            return
        last_dt = datetime.fromisoformat(last_run)
        age_sec = (datetime.now(timezone.utc) - last_dt).total_seconds()

        if age_sec > 600:  # 10 minutes
            # Check cooldown — don't restart more than once per 15 min
            if now - _PIPELINE_RESTART_COOLDOWN < 900:
                logger.warning(
                    "Pipeline stalled (last update %.0fs ago) but in cooldown (last restart %.0fs ago)",
                    age_sec, now - _PIPELINE_RESTART_COOLDOWN,
                )
                return

            _PIPELINE_RESTART_COUNT += 1
            _PIPELINE_RESTART_COOLDOWN = now
            logger.error(
                "Pipeline stalled — last update %.0fs ago. Restarting (attempt #%d)...",
                age_sec, _PIPELINE_RESTART_COUNT,
            )

            # Kill old pipeline process
            import subprocess
            try:
                subprocess.run(
                    ["pkill", "-f", "python3 pipeline.py"],
                    timeout=10, capture_output=True,
                )
                time.sleep(2)
            except Exception as kill_err:
                logger.warning("Failed to kill old pipeline: %s", kill_err)

            # Restart pipeline
            try:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                subprocess.Popen(
                    ["/usr/bin/bash", "-lic",
                     f"set +m; cd {base_dir} && source venv/bin/activate && "
                     f"python3 pipeline.py 2>&1 | tee logs/pipeline.log"],
                    cwd=base_dir,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                logger.info("Pipeline restarted successfully")
            except Exception as start_err:
                logger.error("Failed to restart pipeline: %s", start_err)
    except Exception as e:
        logger.warning("Pipeline health check error: %s", e)


def run_regime_monitor():
    """Classify ETH 1h market regime (TRENDING vs RANGING) using LightGBM model.
    RSI_MR_ETH thrives in RANGING; TRENDING regime signals elevated risk.
    Runs regime_monitor.py as subprocess for isolation; writes to state/regime_monitor.json.
    """
    try:
        import subprocess, os, json
        result = subprocess.run(
            [sys.executable, 'regime_monitor.py'],
            capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        if result.returncode != 0:
            logger.warning("Regime monitor failed: %s", result.stderr.strip()[:200])
        elif result.stdout.strip():
            logger.info("Regime: %s", result.stdout.strip())
    except Exception as e:
        logger.warning("Regime monitor error: %s", e)


def _wal_checkpoint():
    """Periodic WAL checkpoint — prevents WAL file from growing unbounded.
    
    SQLite WAL accumulates all writes since the last checkpoint. Without periodic
    truncation, the WAL file grows indefinitely (observed: 6.9MB after ~12h).
    This runs every 60 minutes to keep the WAL file small.
    """
    try:
        from data.db import wal_checkpoint
        before, after = wal_checkpoint(truncate=True)
        if before > 100:  # Only log if there was meaningful work
            logger.info("WAL checkpoint: %d → %d pages", before, after)
    except Exception as e:
        logger.debug("WAL checkpoint skipped: %s", e)


def run_all():
    logger.info("Aether Engine started — interval %ds", INTERVAL)
    _last_wal_checkpoint = 0
    while True:
        loop_start = time.time()
        try:
            _timed("pipeline_health", check_pipeline_health)
            _timed("backtests", run_backtests)
            _timed("regime_monitor", run_regime_monitor)  # must run before risk_check — regime shift detection
            _timed("live_exchange", fetch_live_exchange)   # MUST run before risk_check (risk_check reuses live_exchange.json)
            _timed("risk_check", run_risk_check)
            _timed("signal_check", run_signal_check)
            _timed("state_sync", sync_agent_states)
            # WAL checkpoint every 60 minutes (12 loops × 5min)
            if time.time() - _last_wal_checkpoint > 3600:
                _wal_checkpoint()
                _last_wal_checkpoint = time.time()
        except Exception as e:
            logger.error("Engine loop error: %s", e)
        elapsed = time.time() - loop_start
        if elapsed > INTERVAL * 0.8:
            logger.warning("Engine loop took %.1fs (%.0f%% of interval) — approaching saturation", elapsed, elapsed / INTERVAL * 100)
        sleep_time = max(0, INTERVAL - elapsed)
        time.sleep(sleep_time)


def _timed(name, fn):
    """Run fn with timing instrumentation. Logs warning if step exceeds threshold."""
    t0 = time.time()
    fn()
    dt = time.time() - t0
    if dt > 30:
        logger.warning("Slow step '%s': %.1fs", name, dt)
    elif dt > 10:
        logger.info("Step '%s': %.1fs", name, dt)
    return dt


if __name__ == "__main__":
    run_all()
