#!/usr/bin/env python3
"""
PERF-069: 365-day sweep-based Deflated Sharpe Ratio for ALL MR strategies.

Computes DSR on 365d of 1h data using the authoritative BacktestEngine.
Compares 90d vs 365d DSR to quantify how much the engine's 90d window
understates statistical significance.

Output: updated prometheus.json with per-strategy 365d DSR metrics.
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
import yaml
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.signal_gen import dispatch_signals

# ── Strategy Definitions ────────────────────────────────────────────────
# Class name → dispatch key mapping + best params from strategies.yaml
STRATEGIES = {
    "RSI_MR_BTC": {
        "class": "RSIMeanReversionStrategy",
        "symbol": "BTC/USDT",
        "n_trials": 972,  # oversold[4]×overbought[3]×exit[3]×sl[3]×tp[4]×cooldown[3]=~972
        "params": {
            "rsi_period": 14, "oversold": 20, "overbought": 80,
            "exit_rsi": 45, "stop_loss_pct": 0.015, "take_profit_pct": 0.06,
            "cooldown_bars": 3,
        },
    },
    "RSI_MR_ETH": {
        "class": "RSIMeanReversionStrategy",
        "symbol": "ETH/USDT",
        "n_trials": 972,
        "params": {
            "rsi_period": 14, "oversold": 20, "overbought": 75,
            "exit_rsi": 50, "stop_loss_pct": 0.02, "take_profit_pct": 0.04,
            "cooldown_bars": 5,
        },
    },
    "KeltnerMR_BTC": {
        "class": "KeltnerMRStrategy",
        "symbol": "BTC/USDT",
        "n_trials": 864,  # 72 combos × BTC+ETH from PERF-050 keltner_mr_sweep
        "params": {
            "kc_period": 20, "atr_mult": 1.75, "atr_period": 14,
            "rsi_period": 14, "oversold": 20, "overbought": 75,
            "exit_level": 50, "stop_loss_pct": 0.015, "take_profit_pct": 0.02,
            "cooldown_bars": 3,
        },
    },
    "KeltnerMR_ETH": {
        "class": "KeltnerMRStrategy",
        "symbol": "ETH/USDT",
        "n_trials": 864,
        "params": {
            "kc_period": 24, "atr_mult": 2.0, "atr_period": 14,
            "rsi_period": 14, "oversold": 20, "overbought": 75,
            "exit_level": 50, "stop_loss_pct": 0.01, "take_profit_pct": 0.02,
            "cooldown_bars": 3,
        },
    },
    "DonchianMR_BTC": {
        "class": "DonchianMRStrategy",
        "symbol": "BTC/USDT",
        "n_trials": 324,  # DP[5]×OS[4]×CD[4]×SL[2]×TP[2] ≈ 320
        "params": {
            "donchian_period": 8, "rsi_period": 14,
            "oversold": 20, "overbought": 80, "exit_level": 50,
            "stop_loss_pct": 0.015, "take_profit_pct": 0.04,
            "cooldown_bars": 9,
        },
    },
    "DonchianMR_ETH": {
        "class": "DonchianMRStrategy",
        "symbol": "ETH/USDT",
        "n_trials": 324,
        "params": {
            "donchian_period": 10, "rsi_period": 14,
            "oversold": 20, "overbought": 80, "exit_level": 50,
            "stop_loss_pct": 0.02, "take_profit_pct": 0.04,
            "cooldown_bars": 5,
        },
    },
    "BandMR_BTC": {
        "class": "BandMRStrategy",
        "symbol": "BTC/USDT",
        "n_trials": 324,  # band_mr_sweep 324 combos
        "params": {
            "donchian_period": 20, "rsi_period": 14,
            "oversold": 30, "overbought": 75, "exit_level": 50,
            "stop_loss_pct": 0.01, "take_profit_pct": 0.025,
            "cooldown_bars": 8, "volume_filter": 1.2,
        },
    },
    "BandMR_ETH": {
        "class": "BandMRStrategy",
        "symbol": "ETH/USDT",
        "n_trials": 324 + 162,  # 324 sweep + 162 DD refine
        "params": {
            "donchian_period": 10, "rsi_period": 14,
            "oversold": 30, "overbought": 75, "exit_level": 50,
            "stop_loss_pct": 0.015, "take_profit_pct": 0.025,
            "cooldown_bars": 5, "volume_filter": 1.2,
        },
    },
}

# ── Main ────────────────────────────────────────────────────────────────

def main():
    db_path = "/home/rinnen/binance_quant/data/market.db"
    prometheus_path = "/home/rinnen/binance_quant/.aether/state/prometheus.json"
    work_dir = "/home/rinnen/binance_quant"

    storage = MarketStorage(db_path)
    engine = BacktestEngine(initial_capital=10000)
    t0 = time.time()

    results = {}
    print(f"{'Strategy':20s} {'Days':>5s} {'Trades':>6s} {'Ret%':>8s} {'SR':>7s} {'DSR':>7s} {'DD%':>7s} {'WR%':>6s} {'N_trials':>8s}")
    print("-" * 90)

    for name, cfg in STRATEGIES.items():
        # Load data
        df = storage.load_klines(cfg["symbol"], "1h")
        if df.empty:
            print(f"{name:20s} {'NO DATA':>60s}")
            continue
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        df.sort_index(inplace=True)

        # 365-day window
        cutoff_365 = df.index[-1] - pd.Timedelta(days=365)
        df_365 = df[df.index >= cutoff_365]

        # 90-day window (for comparison)
        cutoff_90 = df.index[-1] - pd.Timedelta(days=90)
        df_90 = df[df.index >= cutoff_90]

        # 365d backtest
        if len(df_365) < 100:
            results[name] = {"error": "insufficient data", "bars": len(df_365)}
            continue

        signals_365 = dispatch_signals(df_365, cfg["class"], cfg["params"])
        bt_365 = engine.run(df_365, signals_365, leverage=3, n_trials=cfg["n_trials"])

        # 90d backtest
        signals_90 = dispatch_signals(df_90, cfg["class"], cfg["params"])
        bt_90 = engine.run(df_90, signals_90, leverage=3, n_trials=cfg["n_trials"])

        results[name] = {
            "365d": {
                "total_return_pct": round(bt_365["total_return_pct"], 2),
                "sharpe": round(bt_365["sharpe_ratio"], 4),
                "dsr": round(bt_365.get("deflated_sharpe_ratio", 0), 4),
                "max_dd_pct": round(bt_365["max_drawdown_pct"], 2),
                "win_rate": round(bt_365["win_rate"], 2),
                "total_trades": bt_365["total_trades"],
                "profit_factor": round(bt_365.get("profit_factor", 0), 2),
                "bars": len(df_365),
            },
            "90d": {
                "total_return_pct": round(bt_90["total_return_pct"], 2),
                "sharpe": round(bt_90["sharpe_ratio"], 4),
                "dsr": round(bt_90.get("deflated_sharpe_ratio", 0), 4),
                "max_dd_pct": round(bt_90["max_drawdown_pct"], 2),
                "win_rate": round(bt_90["win_rate"], 2),
                "total_trades": bt_90["total_trades"],
                "profit_factor": round(bt_90.get("profit_factor", 0), 2),
                "bars": len(df_90),
            },
            "n_trials": cfg["n_trials"],
            "symbol": cfg["symbol"],
            "class": cfg["class"],
        }

        r365 = results[name]["365d"]
        r90 = results[name]["90d"]
        print(f"{name:20s} "
              f"365d {r365['total_trades']:>4d} {r365['total_return_pct']:>7.1f}% "
              f"{r365['sharpe']:>6.4f} {r365['dsr']:>6.4f} {r365['max_dd_pct']:>6.1f}% "
              f"{r365['win_rate']:>5.1f}% {cfg['n_trials']:>8d}")
        print(f"{'':20s} "
              f" 90d {r90['total_trades']:>4d} {r90['total_return_pct']:>7.1f}% "
              f"{r90['sharpe']:>6.4f} {r90['dsr']:>6.4f} {r90['max_dd_pct']:>6.1f}% "
              f"{r90['win_rate']:>5.1f}% {cfg['n_trials']:>8d}")

    elapsed = time.time() - t0
    print(f"\nCompleted {len(results)} strategies in {elapsed:.1f}s")

    # ── Write to prometheus.json ──
    with open(prometheus_path) as f:
        prom = json.load(f)

    prom["_dsr_365d"] = {
        "run_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "description": "PERF-069: 365d sweep-based DSR for all MR strategies. Compares 365d vs 90d (engine default) to quantify DSR understatement.",
        "results": results,
        "elapsed_s": round(elapsed, 1),
    }

    # Update best_sharpe and best_sharpe_strategy from 365d results
    best_sr = -999
    best_name = None
    for name, r in results.items():
        if "365d" in r:
            sr = r["365d"]["sharpe"]
            if sr > best_sr:
                best_sr = sr
                best_name = name

    if best_name:
        prom["best_sharpe"] = best_sr
        prom["best_sharpe_strategy"] = best_name
        prom["best_sharpe_source"] = "PERF-069_365d"

    # Add 365d DSR summary
    dsr_summary = {}
    for name, r in results.items():
        if "365d" in r:
            dsr_summary[name] = {
                "sharpe_365d": r["365d"]["sharpe"],
                "dsr_365d": r["365d"]["dsr"],
                "sharpe_90d": r["90d"]["sharpe"],
                "dsr_90d": r["90d"]["dsr"],
                "dsr_delta": round(r["365d"]["dsr"] - r["90d"]["dsr"], 4),
                "n_trials": r["n_trials"],
                "trades_365d": r["365d"]["total_trades"],
                "trades_90d": r["90d"]["total_trades"],
            }
    prom["dsr_summary_365d"] = dsr_summary

    prom["last_optimization"] = "PERF-069_365d_dsr"
    prom["last_run"] = pd.Timestamp.now(tz="UTC").isoformat()
    prom["_updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
    prom["next"] = "PERF-070: Apply 365d DSR confidence to position sizing"

    with open(prometheus_path, "w") as f:
        json.dump(prom, f, indent=2, ensure_ascii=False)

    print(f"\nUpdated {prometheus_path}")
    print(f"best_sharpe={best_sr:.4f} ({best_name})")
    print(f"DSR deltas (365d-90d):")
    for name, s in dsr_summary.items():
        status = "[GENUINE]" if s["dsr_365d"] > 0.95 else ("[OK]" if s["dsr_365d"] > 0.80 else "[OVERFIT]")
        print(f"  {name:20s} DSR 365d={s['dsr_365d']:.4f} 90d={s['dsr_90d']:.4f} delta={s['dsr_delta']:+.4f} {status}")

if __name__ == "__main__":
    main()
