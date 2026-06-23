#!/usr/bin/env python3
"""
Aether 自动化引擎 — 后台持续运行所有机械性工作

回测、风控检查、信号执行全部自动化。
专员只读取结果，做判断和决策。
"""
import sys, os, json, time, logging, warnings
from datetime import datetime, timezone

# PERF-006: Module-level base directory — eliminates 7 repetitive
# os.path.dirname(os.path.abspath(__file__)) calls per engine cycle.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Suppress sklearn feature-name warnings (LightGBM 4.6.0 bug)
warnings.filterwarnings('ignore', message='X does not have valid feature names')
warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

sys.path.insert(0, BASE_DIR)
from dotenv import load_dotenv; load_dotenv(os.path.join(BASE_DIR, ".env"))
from data.db import get_market_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [ENGINE] %(message)s")
logger = logging.getLogger("engine")

STATE_DIR = os.path.join(BASE_DIR, ".aether", "state")
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
        db_path = os.path.join(BASE_DIR, "data", "market.db")
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
        # PERF-026: Single source of truth — use signal_gen's canonical dispatch
        # instead of maintaining a duplicate _SIGNAL_DISPATCH here (-75 lines).
        from backtest.signal_gen import SIGNAL_DISPATCH, dispatch_signals
        import numpy as np
        import pandas as pd
        import yaml

        # Load ALL strategies from YAML to read config metadata (only backtest enabled ones)
        yaml_path = os.path.join(BASE_DIR, "config", "strategies.yaml")
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

                # Generate signals using canonical dispatch (single source of truth)
                try:
                    signals = dispatch_signals(df, strategy_type, p)
                except KeyError:
                    results[name] = {
                        "status": "skipped",
                        "symbol": sym, "timeframe": tf,
                        "data_rows": len(df),
                        "error": f"Unknown strategy type: {strategy_type}",
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

        # AUDIT-104: Preserve 365d sweep metrics from athena.json for PAPER strategies.
        # Engine's 90d run has limited data window (31-90 trades) vs 365d sweeps
        # (75-131+ trades). If athena.json holds more trades with higher Sharpe for
        # a PAPER strategy, the 90d engine result is stale/shrunken — inject the
        # authoritative 365d sweep metrics into the strategy result entry.
        # This prevents AUDIT-099 franken-metrics (return=910% but SR=-0.0225 from
        # different windows) from polluting backtest_results.json.
        _athena_data = load_json(os.path.join(STATE_DIR, "athena.json"))
        _athena_strategies = _athena_data.get("strategies", {}) if isinstance(_athena_data, dict) else {}
        for _name, _new_entry in list(results.items()):
            _verdict = _new_entry.get("verdict", "")
            if _verdict not in ("PAPER", "LIVE"):
                continue
            _ae = _athena_strategies.get(_name, {})
            _a_trades = _ae.get("trades") or 0
            _n_trades = _new_entry.get("total_trades") or 0
            _a_sr = _ae.get("sharpe") or 0
            _n_sr = _new_entry.get("sharpe_ratio") or 0
            # Inject 365d metrics when athena has MORE trades AND HIGHER Sharpe
            if _a_trades > _n_trades and _a_sr > _n_sr:
                _new_entry["total_return_pct"] = _ae.get("return_pct", _new_entry.get("total_return_pct", 0))
                _new_entry["sharpe_ratio"] = _a_sr
                _new_entry["max_drawdown_pct"] = _ae.get("max_dd", _new_entry.get("max_drawdown_pct", 0))
                _new_entry["win_rate"] = _ae.get("win_rate", _new_entry.get("win_rate", 0))
                _new_entry["total_trades"] = _a_trades
                _m = _new_entry.setdefault("metrics", {})
                _m["total_return_pct"] = _ae.get("return_pct", 0)
                _m["sharpe_ratio"] = _a_sr
                _m["max_drawdown_pct"] = _ae.get("max_dd", 0)
                _m["win_rate"] = _ae.get("win_rate", 0)
                _m["total_trades"] = _a_trades
                results[_name] = _new_entry
                logger.info(
                    "AUDIT-104 INJECTED: %s 365d sweep metrics from athena.json "
                    "(athena=%d trades/SR=%.3f > engine=%d trades/SR=%.3f)",
                    _name, _a_trades, _a_sr, _n_trades, _n_sr
                )

        # AUDIT-094 FIX: Merge strategies section into existing backtest_results.json
        # to preserve top-level sweep keys (BandMR_BTC, keltner_mr_sweep, etc.).
        # Previously write_json({"strategies": results}) OVERWROTE the file,
        # destroying all top-level keys written by sweep scripts.
        merged = dict(existing_bt) if isinstance(existing_bt, dict) else {}
        merged["strategies"] = results
        write_json("backtest_results.json", merged)
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
_all_yaml_strategy_names = set()  # cached set of ALL strategy names (for disabled calculation)

def run_signal_check():
    """Generate trading signals from active strategies."""
    global _persistent_mgr, _yaml_mtime, _yaml_enabled_names, _all_yaml_strategy_names
    try:
        from strategy.manager import StrategyManager
        from data.collector import BinanceDataCollector
        from config.settings import get_config
        import numpy as np

        cfg = get_config()
        collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)
        import pandas as _pd

        # ── Reload strategies.yaml only when file changes (mtime check) ──
        _yaml_path = "config/strategies.yaml"
        _cur_mtime = os.path.getmtime(_yaml_path)
        _reload = (_persistent_mgr is None or _cur_mtime != _yaml_mtime)

        if _reload:
            import yaml as _yaml
            with open(_yaml_path) as _f:
                _yaml_cfg = _yaml.safe_load(_f)
            _yaml_enabled_names = {s['name'] for s in _yaml_cfg.get('strategies', []) if s.get('enabled')}
            _all_yaml_strategy_names = {s['name'] for s in _yaml_cfg.get('strategies', [])}  # all names for disabled calc
            _yaml_mtime = _cur_mtime
            _persistent_mgr = StrategyManager.load_from_yaml(_yaml_path)
            logger.info("Strategies reloaded from YAML (mtime changed): %d enabled", len(_yaml_enabled_names))

            # ═══ AUDIT-047: Regression guard — detect re-enabled disabled strategies ═══
            # Cross-reference newly-enabled strategies against athena.json AND backtest_results.json.
            # If a strategy was previously disabled (verdict=PAPER/DO_NOT_ENABLE/RETIRED)
            # and appears as enabled:true in YAML, auto-correct it back to enabled:false.
            # 
            # AUDIT-047 v2: Also cross-check backtest_results.json (Prometheus authority).
            # If athena.json is manipulated to show LIVE while backtest_results.json
            # shows PAPER/DO_NOT_ENABLE/RETIRED, block the promotion.
            _athena_path = os.path.join(STATE_DIR, "athena.json")
            _bt_results_path = os.path.join(STATE_DIR, "backtest_results.json")
            _bt_verdicts = {}
            if os.path.exists(_bt_results_path):
                try:
                    _bt_data = load_json(_bt_results_path)
                    for _bn, _bv in _bt_data.get("strategies", {}).items():
                        _bt_verdicts[_bn] = _bv.get("verdict", "")
                except Exception:
                    pass
            if os.path.exists(_athena_path):
                _athena = load_json(_athena_path)
                _athena_strats = _athena.get("strategies", {})
                _re_enabled = []
                for _name in _yaml_enabled_names:
                    _v = _athena_strats.get(_name, {}).get("verdict", "")
                    # AUDIT-047 v2: also check backtest_results.json authority
                    _btv = _bt_verdicts.get(_name, "")
                    # If athena says LIVE but backtest_results says non-LIVE, treat as blocked
                    if _v == "LIVE" and _btv and _btv not in ("LIVE", ""):
                        _v = _btv  # use backtest_results authority
                        logger.warning(
                            "AUDIT-047 v2: %s athena verdict=LIVE but backtest_results=%s. Using backtest_results authority.",
                            _name, _btv
                        )
                    # AUDIT-047 v3: DO_NOT_ENABLE/RETIRED/PAUSED/NOT_EVALUATED
                    # strategies must NOT be enabled in YAML.
                    # PAPER strategies ARE allowed — they generate signals for paper
                    # trading (signal-only, no real orders per mercury_run.py athena_paper).
                    # Only block truly retired/failed strategies.
                    if _v in ("DO_NOT_ENABLE", "RETIRED", "PAUSED", "NOT_EVALUATED"):
                        _re_enabled.append(f"{_name}(verdict={_v})")
                if _re_enabled:
                    # Auto-fix: flip enabled back to false in strategies.yaml
                    import yaml as _yaml2
                    _fixed = []
                    for _s in _yaml_cfg.get("strategies", []):
                        _sn = _s.get("name", "")
                        _sv = _athena_strats.get(_sn, {}).get("verdict", "")
                        if _sn in _yaml_enabled_names and _sv in ("DO_NOT_ENABLE", "RETIRED", "PAUSED", "NOT_EVALUATED"):
                            _s["enabled"] = False
                            _fixed.append(_sn)
                    if _fixed:
                        with open(_yaml_path, "w") as _f:
                            _yaml2.dump(_yaml_cfg, _f, default_flow_style=False, sort_keys=False)
                        _yaml_enabled_names = {s['name'] for s in _yaml_cfg.get('strategies', []) if s.get('enabled')}
                        _all_yaml_strategy_names = {s['name'] for s in _yaml_cfg.get('strategies', [])}
                        _yaml_mtime = os.path.getmtime(_yaml_path)
                        _persistent_mgr = StrategyManager.load_from_yaml(_yaml_path)
                        logger.critical(
                            "AUDIT-047 AUTO-FIX: Re-disabled %d strategies that were re-enabled in YAML: %s",
                            len(_fixed), ", ".join(_fixed)
                        )

        mgr = _persistent_mgr
        _enabled_names = _yaml_enabled_names

        # ═══ PERF-031+049: Regime-aware strategy gating ═══
        # Mean-reversion strategies (RSI_MR, DonchianMR, KeltnerMR, etc.) thrive
        # in RANGING regimes and bleed in TRENDING regimes.
        #
        # PERF-031 (original): Binary gate — suppress MR signals when P(Trend) > threshold.
        #   Hard safety gate at P>0.85: fully suppress (prevent catastrophic MR entries).
        #
        # PERF-049 (Prometheus): Continuous regime-aware position multiplier.
        #   Instead of binary gating, position size decays linearly as P(Trend)
        #   rises from 0.45→0.65. mercury_run.py applies this multiplier directly.
        #   engine.py passes it through signal_data for trade_executor.py.
        #
        # THRESHOLD: athena.json _perf031_threshold controls hard-gate (default 0.85).
        #   Raised from 0.5→0.6 by Athena 2026-06-23; now superseded by continuous approach.
        _regime_multiplier = 1.0  # PERF-049: continuous position multiplier
        _regime_gated = set()      # PERF-031: binary hard-gate (P>0.85 only)
        _p_trend = 0.0
        _regime_label = "UNKNOWN"
        _regime_path = os.path.join(STATE_DIR, "regime_monitor.json")
        _athena_path = os.path.join(STATE_DIR, "athena.json")
        _PERF031_HARD_GATE = 0.85  # default hard-gate, overridable via athena.json
        if os.path.exists(_athena_path):
            try:
                _athena_data = load_json(_athena_path)
                _PERF031_HARD_GATE = _athena_data.get("_perf031_hard_gate", 0.85)
            except Exception:
                pass
        if os.path.exists(_regime_path):
            try:
                _regime_data = load_json(_regime_path)
                _regime_label = _regime_data.get("regime", "")
                _p_trend = _regime_data.get("p_trending", 0)

                # ── PERF-049: Compute continuous regime multiplier ──
                MR_NEUTRAL = 0.45
                MR_MAX_REDUCE = 0.65
                MR_FLOOR = 0.10
                if _p_trend > MR_NEUTRAL:
                    if _p_trend >= MR_MAX_REDUCE:
                        _regime_multiplier = MR_FLOOR
                    else:
                        _regime_multiplier = 1.0 - (1.0 - MR_FLOOR) * (_p_trend - MR_NEUTRAL) / (MR_MAX_REDUCE - MR_NEUTRAL)

                # ── PERF-031: Hard safety gate at extreme trend ──
                if _regime_label == "TRENDING" and _p_trend > _PERF031_HARD_GATE:
                    _mr_patterns = ("_MR_", "MR_", "RSI_", "DonchianMR", "KeltnerMR",
                                    "BBandRSI", "StochRSI", "BBand", "MeanRev")
                    for _en in _enabled_names:
                        if any(_pat in _en for _pat in _mr_patterns):
                            _regime_gated.add(_en)
                    if _regime_gated:
                        logger.warning(
                            "PERF-031 HARD GATE: TRENDING (P=%.3f > %.2f). Gating %d MR strategies: %s",
                            _p_trend, _PERF031_HARD_GATE, len(_regime_gated), ", ".join(sorted(_regime_gated))
                        )
                elif _regime_multiplier < 1.0:
                    logger.info(
                        "PERF-049 REGIME: %s P(Trend)=%.3f → MR position multiplier=%.2f",
                        _regime_label, _p_trend, _regime_multiplier
                    )
            except Exception as _re:
                logger.debug("Regime gate check skipped: %s", _re)

        signals = {}
        # Track signals per symbol for conflict detection
        symbol_signals = {}  # symbol -> list of (name, signal_dict)
        
        # Cache fetched klines by (symbol, timeframe) to avoid redundant API calls
        # when multiple strategies share the same symbol/timeframe
        _klines_cache = {}
        _indicator_state = {}  # PERF-065: per-strategy indicator snapshot

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
                    # ═══ DATA SOURCE: market.db first (authoritative), API fallback ═══
                    # market.db is maintained by Oracle's data_ext pipeline and is the
                    # source of truth. API data can diverge (especially on testnet).
                    # AUDIT-101: API data caused BTC RSI=25.3 while market.db had 13.4.
                    _db_data = None
                    _db = get_market_db()
                    try:
                        _rows = _db.execute(
                            "SELECT open_time, open, high, low, close, volume FROM klines WHERE symbol=? AND timeframe=? ORDER BY open_time DESC LIMIT 300",
                            (sym, tf)
                        ).fetchall()
                    finally:
                        _db.close()
                    if _rows and len(_rows) >= 50:
                        _rows.reverse()  # oldest first
                        _db_data = _pd.DataFrame(
                            _rows,
                            columns=["open_time", "open", "high", "low", "close", "volume"]
                        ).set_index("open_time")

                    if _db_data is not None:
                        _klines_cache[cache_key] = _db_data
                    else:
                        # Fallback to API if market.db is insufficient
                        try:
                            _klines_cache[cache_key] = collector.fetch_current_klines(sym, tf, 300)
                            logger.warning("market.db insufficient for %s %s, using API fallback", sym, tf)
                        except Exception as _api_err:
                            raise RuntimeError(f"No data for {sym} {tf} (market.db empty + API failed: {_api_err})")
                df = _klines_cache[cache_key]
                mgr.feed_data_only(sym, tf, df)
                sig = strat.generate_signal(sym)
                if sig.type.name != "HOLD":
                    logger.info("Strategy '%s' generated %s: %s", name, sig.type.name, sig.reason[:120] if sig.reason else "no reason")
            except Exception as e:
                logger.error("Signal error for strategy '%s' (%s %s): %s", name, sym, tf, e)
                continue

            if sig.type.name != "HOLD":
                # PERF-031+049: Regime-aware signal handling
                # Hard gate (P>0.85): fully suppress MR signals (safety)
                if name in _regime_gated:
                    logger.info(
                        "PERF-031 HARD GATE: %s %s signal suppressed (regime=%s P=%.3f > %.2f)",
                        name, sig.type.name, _regime_label, _p_trend, _PERF031_HARD_GATE
                    )
                    continue
                # PERF-049: Pass regime multiplier through signal for downstream sizing
                _is_mr = any(pat in name for pat in ("_MR_", "MR_", "RSI_", "DonchianMR", "KeltnerMR",
                                                     "BBandRSI", "StochRSI", "BBand", "MeanRev"))
                _sig_mult = _regime_multiplier if _is_mr else 1.0
                signal_data = {
                    "symbol": sym, "timeframe": tf,
                    "signal": sig.type.value,
                    "price": float(sig.price) if not np.isnan(float(sig.price)) else float(df.iloc[-1]["close"]),
                    "stop_loss": float(sig.stop_loss) if not np.isnan(float(sig.stop_loss)) else None,
                    "take_profit": float(sig.take_profit) if not np.isnan(float(sig.take_profit)) else None,
                    "confidence": sig.confidence,
                    "reason": sig.reason,
                    "strategy": name,
                    "regime_multiplier": _sig_mult,       # PERF-049: for trade_executor position sizing
                    "regime": _regime_label,
                    "p_trend": round(_p_trend, 3),
                }
                symbol_signals.setdefault(sym, []).append((name, signal_data))

            # ── PERF-065: Collect indicator snapshot for diagnostics ──
            # Save key indicator state per strategy so any agent can inspect
            # what the engine is "seeing" without querying the DB directly.
            _ind_key = (sym, tf)
            _ind = strat._indicators.get(_ind_key)
            if _ind is not None:
                _latest = _ind.iloc[-1]
                _snap = {"symbol": sym, "timeframe": tf, "price": float(df.iloc[-1]["close"])}
                for _col in ["rsi", "kc_lower", "kc_upper", "kc_mid", "dc_lower", "dc_upper", "dc_mid"]:
                    _val = _latest.get(_col)
                    if _val is not None and not (isinstance(_val, float) and np.isnan(_val)):
                        _snap[_col] = round(float(_val), 2)
                _indicator_state[name] = _snap

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
        # Also write in executor-compatible format
        write_json("trade_signals.json", {"signals": signals, "timestamp": datetime.now(timezone.utc).isoformat()})
        # PERF-065: Save indicator snapshot for transparent diagnostics
        write_json("indicator_state.json", {
            "strategies": _indicator_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "regime": _regime_label,
            "p_trend": round(_p_trend, 3),
            "rsi_method": "Wilder_EWM",  # PERF-066: canonical RSI algorithm — prevents SMA vs Wilder confusion
        })
        logger.info("Signals: %d generated", len(signals))
    except Exception as e:
        logger.error("Signal error: %s", e)
        write_json("signals.json", {"error": str(e), "status": "error"})


def _retain_last_valid_live_state(reason: str = "API failure") -> bool:
    """Retain last valid live_exchange.json state when API returns garbage.

    AUDIT-009/PERE-011: Extracted from fetch_live_exchange() to eliminate
    duplicated retain-last-valid-state logic (~20 lines duplicated × 2 call sites).
    Returns True if a valid previous state was retained, False otherwise.
    """
    state_path = os.path.join(STATE_DIR, "live_exchange.json")
    if not os.path.exists(state_path):
        return False
    try:
        with open(state_path) as f:
            existing = json.load(f)
        existing_bal = existing.get("balance", {})
        if isinstance(existing_bal, dict):
            if existing_bal.get("balance", 0) > 0:
                write_json("live_exchange.json", existing)
                logger.warning(
                    "Live exchange: %s, retained last valid state (balance=%.2f)",
                    reason, existing_bal.get("balance", 0),
                )
                return True
        elif isinstance(existing_bal, (int, float)) and existing_bal > 0:
            write_json("live_exchange.json", existing)
            logger.warning(
                "Live exchange: %s, retained last valid state (balance=%.2f)",
                reason, existing_bal,
            )
            return True
    except Exception:
        pass
    return False


def fetch_live_exchange():
    """Pull live account data from Binance testnet.

    AUDIT-009 FIX: When API is rate-limited, Binance returns balance=0
    and ghost positions. Detect this and retain the last valid state
    file instead of overwriting it with zeros.

    PERF-001/002/003: Uses client.get_live_snapshot() which:
      - Skips ccxt on testnet (balance/orders ccxt calls always time out)
      - Uses /fapi/v1/ticker/bookTicker for both symbols in one REST call
      - Reduces from 5-8 API calls to 4 sequential REST calls

    PERF-011: Extracted retain-last-valid-state into _retain_last_valid_live_state().
    """
    try:
        from execution.client import BinanceFuturesClient
        from config.settings import get_config
        cfg = get_config()
        client = BinanceFuturesClient(cfg.api_key, cfg.api_secret, cfg.testnet)

        # Use optimized snapshot: balance (REST, skips ccxt) + positions (REST)
        # + orders (REST, skips ccxt) + tickers (single bookTicker REST call)
        bal, positions, orders, tickers = client.get_live_snapshot()

        # ── AUDIT-009: Validate API data before overwriting ──
        if bal.get("balance", 0) == 0:
            if _retain_last_valid_live_state("API returned balance=0"):
                return

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
        if not _retain_last_valid_live_state(str(e)):
            write_json("live_exchange.json", {"error": str(e), "status": "error"})


def _build_strategy_summary(bt, _prom_data, _disabled_in_yaml):
    """PERF-032: Build authoritative strategy summary with multi-source cross-validation.

    Reads backtest_results.json, cross-references prometheus.json (AUDIT-053 v3/v4)
    and athena.json (AUDIT-047 v2, AUDIT-053) to produce the canonical strategy
    summary written to athena.json and consumed by all downstream agents.

    Args:
        bt: Parsed backtest_results.json dict
        _prom_data: Parsed prometheus.json dict ({} if missing)
        _disabled_in_yaml: Set of strategy names disabled in strategies.yaml

    Returns:
        dict: strat_summary mapping strategy_name → {return_pct, sharpe, win_rate,
              trades, max_dd, verdict, signals, status}
    """
    # ── Phase 1: Inject Prometheus 365d sweep metrics into bt (AUDIT-053 v3/v4) ──
    if _prom_data:
        try:
            _prom_strategies = _prom_data.get("strategies", {})
            # Map special prometheus.json keys to strategy names
            _prom_special_map = {
                "donchianmr_btc_paper_ready": "DonchianMR_BTC",
                "donchianmr_eth_paper_ready": "DonchianMR_ETH",
                "rsi_mr_eth_live_confirmed": "RSI_MR_ETH",
            }
            for _pk, _pv in _prom_data.items():
                if _pk in _prom_special_map and isinstance(_pv, dict) and _pv.get("trades"):
                    _prom_strategies[_prom_special_map[_pk]] = _pv
            # AUDIT-053 v4: Guardian.json fallback (live + paper)
            _gd_path = os.path.join(STATE_DIR, "guardian.json")
            _gd_data = load_json(_gd_path)
            if _gd_data:
                _gd_live = _gd_data.get("live_strategy_metrics", {})
                for _gn, _gv in _gd_live.items():
                    if not isinstance(_gv, dict):
                        continue
                    _g_trades = _gv.get("trades") or 0
                    _p_trades_existing = (_prom_strategies.get(_gn, {}) or {}).get("trades") or 0
                    if _g_trades > _p_trades_existing:
                        _prom_strategies[_gn] = {
                            "return_pct": _gv.get("return_pct", 0),
                            "sharpe": _gv.get("sharpe", 0),
                            "win_rate": _gv.get("win_rate", 0),
                            "trades": _g_trades,
                            "max_dd": _gv.get("max_dd", 0),
                        }
                # AUDIT-099: Also fallback to guardian paper_strategy_metrics
                # (BandMR_ETH/BTC live here as PAPER, with correct 365d sweep SR)
                _gd_paper = _gd_data.get("paper_strategy_metrics", {})
                for _gn, _gv in _gd_paper.items():
                    if not isinstance(_gv, dict):
                        continue
                    _g_trades = _gv.get("trades") or 0
                    _p_trades_existing = (_prom_strategies.get(_gn, {}) or {}).get("trades") or 0
                    if _g_trades > _p_trades_existing:
                        _prom_strategies[_gn] = {
                            "return_pct": _gv.get("return_pct", 0),
                            "sharpe": _gv.get("sharpe", 0),
                            "win_rate": _gv.get("win_rate", 0),
                            "trades": _g_trades,
                            "max_dd": _gv.get("max_dd", 0),
                        }
            # AUDIT-099: Frankestein-metrics detection — return>100% but sharpe<0
            # is mathematically impossible (different backtest windows mixed).
            # Flag corrupted prometheus entries and skip injection so guardian
            # fallback above can supply correct 365d sweep SR.
            for _name, _pdata in list(_prom_strategies.items()):
                if not isinstance(_pdata, dict):
                    continue
                _p_ret = _pdata.get("return_pct", 0) or 0
                _p_sr_check = _pdata.get("sharpe", 0) or 0
                if _p_ret > 100 and _p_sr_check < 0:
                    logger.warning(
                        "AUDIT-099 FRANKEN-METRICS: %s return=%.1f%% sharpe=%.4f "
                        "— mathematically impossible, skipping prometheus injection",
                        _name, _p_ret, _p_sr_check
                    )
                    del _prom_strategies[_name]
            # Inject prometheus data into bt when prom has MORE trades
            # AND prometheus data appears authoritative (365d sweep), BUT only if
            # prometheus SR is HIGHER (prevents stale data overriding refined sweeps
            # where fewer trades = better quality, e.g. KeltnerMR refined 22→21 trades).
            _bt_strategies = bt.get("strategies", {})
            for _name, _pdata in _prom_strategies.items():
                if not isinstance(_pdata, dict):
                    continue
                _p_trades = _pdata.get("trades") or 0
                if _p_trades <= 0:
                    continue
                _bt_entry = _bt_strategies.get(_name, {})
                _bt_trades = _bt_entry.get("total_trades") or 0
                _p_sr = _pdata.get("sharpe", 0)
                _bt_sr = _bt_entry.get("sharpe_ratio", 0)
                # PERF-033: Only inject prometheus data if it has MORE trades AND
                # HIGHER Sharpe. If backtest_results has higher SR but fewer trades,
                # it's likely a refined sweep — keep backtest_results authority.
                if _p_trades > _bt_trades and _p_sr > _bt_sr:
                    _injected = {
                        "total_return_pct": _pdata.get("return_pct", 0),
                        "sharpe_ratio": _pdata.get("sharpe", 0),
                        "max_drawdown_pct": _pdata.get("max_dd", _pdata.get("dd_pct", 0)),
                        "win_rate": _pdata.get("win_rate", _pdata.get("wr_pct", 0)),
                        "total_trades": _p_trades,
                        "profit_factor": _pdata.get("profit_factor", _pdata.get("pf", None)),
                        "backtest_period": _pdata.get("backtest_period", "365d"),
                    }
                    for k, v in _injected.items():
                        if v is not None:
                            _bt_entry[k] = v
                            _bt_metrics = _bt_entry.setdefault("metrics", {})
                            _bt_metrics[k] = v
                    _bt_strategies[_name] = _bt_entry
                    logger.info(
                        "AUDIT-053 v3 INJECTED: %s metrics from prometheus.json "
                        "(prom=%d trades > bt=%d trades)", _name, _p_trades, _bt_trades
                    )
            bt["strategies"] = _bt_strategies
        except Exception as _e:
            logger.warning("AUDIT-053 v3 prometheus injection failed: %s", _e)

    # ── Phase 2: Build strat_summary from (now-enriched) bt ──
    strat_summary = {}
    for name, s in bt.get("strategies", {}).items():
        entry = {"signals": s.get("signals_count", 0), "status": s.get("status", "?")}
        m = s.get("metrics", {})
        has_metrics = m and m.get("total_trades", 0) > 0
        top_trades = s.get("total_trades") or 0
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

    # ── Phase 3: Cross-validate verdicts against athena.json and YAML (AUDIT-047/053) ──
    existing_athena = load_json(os.path.join(STATE_DIR, "athena.json"))
    _bt_verdicts = {_bn: _bv.get("verdict", "") for _bn, _bv in bt.get("strategies", {}).items()}

    # AUDIT-053 v5: Preserve metrics from athena.json when it has MORE trades
    # AND better or equal Sharpe. If new data has higher SR with fewer trades,
    # it's likely a refined sweep — trust the higher-quality result.
    # (Fixes AUDIT-064 9.5h KeltnerMR_BTC split where refined sweep 21t/SR=0.516
    # was overwritten by stale athena.json 22t/SR=0.401 because old logic only
    # checked trade count, ignoring quality.)
    for name in strat_summary:
        existing_strat = existing_athena.get("strategies", {}).get(name, {})
        existing_trades = existing_strat.get("trades") or 0
        existing_sr = existing_strat.get("sharpe") or 0
        new_trades = strat_summary[name].get("trades") or 0
        new_sr = strat_summary[name].get("sharpe") or 0
        if existing_trades > new_trades and new_trades > 0 and existing_sr >= new_sr:
            for field in ("return_pct", "sharpe", "win_rate", "trades", "max_dd"):
                if field in existing_strat:
                    strat_summary[name][field] = existing_strat[field]
            logger.info(
                "AUDIT-053 PRESERVED: %s metrics (old=%d trades/SR=%.3f > new=%d trades/SR=%.3f)",
                name, existing_trades, existing_sr, new_trades, new_sr
            )
        elif existing_trades > new_trades and new_sr > existing_sr:
            logger.info(
                "AUDIT-053 v5 REFINED ACCEPTED: %s (new=%d trades/SR=%.3f beats old=%d trades/SR=%.3f — refined sweep)",
                name, new_trades, new_sr, existing_trades, existing_sr
            )

    # AUDIT-047 v2: Block invalid verdict promotions
    for name in strat_summary:
        existing_verdict = existing_athena.get("strategies", {}).get(name, {}).get("verdict", "")
        if not existing_verdict or existing_verdict in ("", "?"):
            continue
        if existing_verdict == "LIVE" and name in _disabled_in_yaml:
            logger.warning("AUDIT-047 BLOCKED: %s verdict=LIVE but disabled in YAML → PAPER", name)
            strat_summary[name]["verdict"] = "PAPER"
        elif existing_verdict == "LIVE":
            _btv = _bt_verdicts.get(name, "")
            if _btv and _btv not in ("LIVE", ""):
                logger.warning("AUDIT-047 v2 BLOCKED: %s athena=LIVE but bt=%s → %s", name, _btv, _btv)
                strat_summary[name]["verdict"] = _btv
            else:
                strat_summary[name]["verdict"] = existing_verdict
        else:
            strat_summary[name]["verdict"] = existing_verdict

    return strat_summary


def sync_agent_states():
    """Update agent state files — MERGE with existing, preserve tasks."""
    try:
        def merge_state(agent, updates):
            existing = load_json(os.path.join(STATE_DIR, f"{agent}.json"))
            updates["_updated_at"] = datetime.now(timezone.utc).isoformat()
            existing.update(updates)
            write_json(f"{agent}.json", existing)

        def _normalize_guardian_state():
            """Post-merge normalization: ensure ALL position/order fields self-consistent."""
            gpath = os.path.join(STATE_DIR, "guardian.json")
            if not os.path.exists(gpath):
                return
            gstate = load_json(gpath)
            pos_cnt = gstate.get("positions_count", gstate.get("positions", 0))
            orders = gstate.get("open_orders", 0)
            gstate["positions"] = pos_cnt
            gstate["positions_count"] = pos_cnt
            gstate["effective_positions"] = pos_cnt
            gstate["open_orders"] = orders
            if "account" not in gstate:
                gstate["account"] = {}
            gstate["account"]["positions"] = pos_cnt
            gstate["account"]["open_orders"] = orders
            write_json("guardian.json", gstate)

        pipe = load_json(os.path.join(STATE_DIR, "pipeline.json"))

        import subprocess as _sp
        def _find_pid(cmd_pattern):
            """Find the actual python3 process PID, filtering out bash wrappers
            and other non-matching python3 processes (e.g. Hermes cron jobs
            whose command lines happen to contain the pattern string)."""
            try:
                r = _sp.run(["pgrep", "-f", cmd_pattern], capture_output=True, text=True, timeout=5)
                pids = [int(x) for x in r.stdout.strip().split("\n") if x]
                python_pids = []
                current_pid = os.getpid()
                for pid in pids:
                    if pid == current_pid:
                        continue  # skip self
                    try:
                        comm = open(f"/proc/{pid}/comm").read().strip()
                        if comm != "python3":
                            continue  # skip bash wrappers and other non-python3
                        # Verify cmdline actually IS the target script (not a
                        # cron/Hermes python3 that merely contains the string)
                        cmdline = open(f"/proc/{pid}/cmdline").read()
                        cmdline_parts = cmdline.replace("\x00", " ").strip().split()
                        # cmdline_parts[0] is "python3", [1] is the script name
                        if len(cmdline_parts) >= 2 and cmdline_parts[1].endswith(
                            cmd_pattern.split()[-1]  # e.g. "data_ext.py" from "python3 data_ext.py"
                        ):
                            python_pids.append(pid)
                    except Exception:
                        pass
                return python_pids[-1] if python_pids else (pids[-1] if pids else None)
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
        pid_path = os.path.join(STATE_DIR, "engine.pid")
        with open(pid_path, "w") as pf:
            pf.write(str(os.getpid()))

        try:
            db = get_market_db()
            oracle_updates["klines_count"] = db.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
            db.close()
        except Exception:
            pass

        _disabled_in_yaml = _all_yaml_strategy_names - _yaml_enabled_names
        # ── AUDIT-047 v4: filter PAPER strategies from oracle state ──
        # Engine needs PAPER strategies in _yaml_enabled_names for signal
        # generation, but oracle state should only report LIVE strategies.
        # Without this filter, state/oracle.json drifts from .aether/oracle.json
        # (which pipeline.py correctly filters via AUDIT-051 guard).
        _oracle_live_names = set(_yaml_enabled_names)
        _ath_path = os.path.join(STATE_DIR, "athena.json")
        if os.path.exists(_ath_path):
            try:
                _ath = load_json(_ath_path)
                for _sn, _sd in _ath.get("strategies", {}).items():
                    if _sd.get("verdict") == "PAPER" and _sn in _oracle_live_names:
                        _oracle_live_names.discard(_sn)
            except Exception:
                pass
        oracle_updates["strategies_enabled"] = sorted(_oracle_live_names)
        oracle_updates["strategies_disabled"] = len(_disabled_in_yaml)
        merge_state("oracle", oracle_updates)
        # AUDIT-092: ensure klines_total duplicate is purged from state file
        try:
            _ostate_path = os.path.join(STATE_DIR, "oracle.json")
            if os.path.exists(_ostate_path):
                with open(_ostate_path, "r") as _f:
                    _ostate = json.load(_f)
                if "klines_total" in _ostate:
                    del _ostate["klines_total"]
                    write_json("oracle.json", _ostate)
        except Exception:
            pass

        # ── Build strategy summary with cross-validation ──
        bt = load_json(os.path.join(STATE_DIR, "backtest_results.json"))
        _prom_data = load_json(os.path.join(STATE_DIR, "prometheus.json"))
        strat_summary = _build_strategy_summary(bt, _prom_data, _disabled_in_yaml)
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

        # Prometheus: pull real backtest metrics from strat_summary (which has
        # AUDIT-053 athena.json preservation applied at L1000-1017), NOT raw
        # s["metrics"] which contains engine's 90d short backtest that may have
        # fewer trades than Prometheus-authoritative 365d sweeps.
        # AUDIT-053 v4: This was the missing link — strat_summary already has
        # the correct preserved metrics but prometheus.json write was bypassing it.
        prom_state = {"status": "active", "strategies": {}, "engine_pid": os.getpid()}
        for name, ss_entry in strat_summary.items():
            # Only include strategies that exist in bt and have meaningful data
            if ss_entry.get("trades", 0) <= 0:
                continue
            prom_state["strategies"][name] = {
                "return_pct": ss_entry.get("return_pct", 0),
                "sharpe": ss_entry.get("sharpe", 0),
                "win_rate": ss_entry.get("win_rate", 0),
                "trades": ss_entry.get("trades", 0),
                "max_dd": ss_entry.get("max_dd", 0),
            }
        # Preserve Prometheus-specific metadata fields from already-loaded _prom_data
        for key in ("dsr_implemented", "walk_forward_implemented", "anti_overfitting_run", "wf_findings", "next",
                     "recommendation", "live_validation", "ml_alpha_status", "ml_validation",
                     "regime_model", "regime_monitor", "regime_classifier", "last_optimization",
                     "strategy_landscape_20260622", "rsi_mr_eth_live_confirmed",
                     "donchian_mr_eth_wf_validation", "donchian_mr_eth_365d",
                     "donchian_mr_btc_optimized", "portfolio_correlation"):
            if key in _prom_data:
                prom_state[key] = _prom_data[key]
        # NOTE: strategies metrics are ENGINE-DERIVED (from backtest_results.json).
        # Prometheus-generated metrics live in rsi_mr_eth_live_confirmed etc., NOT
        # in the strategies section which is overwritten by every engine tick.
        # Do NOT preserve existing prometheus.json strategies metrics — the engine
        # is the single source of truth for backtest performance data.
        # Clean up stale hardcoded fake fields (replaced by strategies dict)
        # PERF-039: Removed dgt_* null writes — dead fields that served no purpose
        # and bloated prometheus.json on every engine tick
        merge_state("prometheus", prom_state)
    except Exception as e:
        logger.error("State sync error: %s", e)

    # Regenerate dashboard
    try:
        import subprocess, sys
        subprocess.run([sys.executable, "generate_dashboard.py"], capture_output=True, timeout=30)
    except Exception as e:
        logger.warning("Dashboard regeneration failed: %s", e)

    # Post engine heartbeat summary to bulletin (keeps bulletin fresh every tick)
    try:
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
        bulletin_path = os.path.join(BASE_DIR, ".aether", "bulletin.md")
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
    except Exception:
        return {}


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
                subprocess.Popen(
                    ["/usr/bin/bash", "-lic",
                     f"set +m; cd {BASE_DIR} && source venv/bin/activate && "
                     "python3 pipeline.py 2>&1 | tee logs/pipeline.log"],
                    cwd=BASE_DIR,
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

    PERF-005: Calls regime_monitor.py directly (no subprocess). Model is cached
    in-memory after first load, eliminating ~0.3-0.5s deserialization per cycle.
    Previous subprocess approach spawned a full Python process every 5 min.
    Result is still written to state/regime_monitor.json for downstream consumers.
    """
    try:
        from regime_monitor import run_regime_check, _write_and_post
        now = datetime.now(timezone.utc)
        result = run_regime_check()
        if result is not None:
            _write_and_post(result, now)
            logger.info("Regime: %s | P(Trend)=%.3f",
                        result['regime'], result['p_trending'])
        else:
            logger.debug("Regime monitor skipped: insufficient data or model missing")
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


def _acquire_engine_lock():
    """PERF-066: Singleton lock — prevent duplicate engine instances.

    Checks engine.pid for an existing running engine process.
    If found and still alive, exits immediately to avoid:
    - Race conditions on state files (indicator_state.json, signals.json, etc.)
    - Redundant Binance API calls (rate limits)
    - Divergent indicator values between instances (e.g. RSI at different times)

    Returns True if lock acquired, exits process if another engine is running.
    """
    pid_path = os.path.join(STATE_DIR, "engine.pid")
    if os.path.exists(pid_path):
        try:
            with open(pid_path) as f:
                old_pid = int(f.read().strip())
            # Check if the old PID is still a running python3 engine.py process
            try:
                os.kill(old_pid, 0)  # signal 0 = existence check only
                # Read /proc/<pid>/cmdline to verify it's actually engine.py
                try:
                    with open(f"/proc/{old_pid}/cmdline", "rb") as cf:
                        cmdline = cf.read().decode("utf-8", errors="replace").replace("\x00", " ")
                    if "engine.py" in cmdline:
                        logger.critical(
                            "PERF-066: Engine PID %d already running. "
                            "Refusing to start duplicate. Exiting.", old_pid
                        )
                        sys.exit(0)
                except Exception:
                    pass  # can't read cmdline, assume it's engine.py
            except OSError:
                # PID not running — stale lock file, safe to overwrite
                logger.info("PERF-066: Stale engine.pid (%d) — process not found. Overwriting.", old_pid)
        except (ValueError, FileNotFoundError):
            pass  # malformed or deleted, safe to overwrite

    # Write current PID
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))
    logger.info("PERF-066: Engine lock acquired (PID=%d)", os.getpid())
    return True


def _release_engine_lock():
    """Release the singleton lock on clean shutdown."""
    pid_path = os.path.join(STATE_DIR, "engine.pid")
    try:
        if os.path.exists(pid_path):
            with open(pid_path) as f:
                stored = f.read().strip()
            if stored == str(os.getpid()):
                os.remove(pid_path)
                logger.info("PERF-066: Engine lock released (PID=%d)", os.getpid())
    except Exception:
        pass


def run_all():
    _acquire_engine_lock()
    logger.info("Aether Engine started — interval %ds", INTERVAL)
    _last_wal_checkpoint = 0
    try:
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
    except KeyboardInterrupt:
        logger.info("Engine shutting down (KeyboardInterrupt)")
        _release_engine_lock()
    except Exception as e:
        logger.critical("Engine fatal error: %s", e)
        _release_engine_lock()
        raise


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
