#!/usr/bin/env python3
"""Supertrend Parameters Sweep — standalone module, run via python3 -m backtests.supertrend_sweep

Athena REQ #174 Phase 1: BTC strategy diversification.
Wide parameter sweep for Supertrend_BTC to find viable trend-following config.
"""
import sys, os, json, itertools, time, warnings
os.chdir("/home/rinnen/binance_quant")
sys.path.insert(0, "/home/rinnen/binance_quant")
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from backtest.sweep_utils import load_data
from backtest.engine import BacktestEngine
from backtest.signal_gen import supertrend_signals

def run():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refined", action="store_true", help="Phase 2: refined sweep on best region")
    args, _ = ap.parse_known_args()

    if args.refined:
        # Phase 2: tighter grid around best region
        param_grid = {
            "atr_period": [10, 12, 14],
            "atr_mult": [2.5, 3.0, 3.5],
            "cooldown_bars": [5, 8, 10],
            "leverage": [2, 3],
        }
        print("Running REFINED sweep: 3×3×3×2 = 54 combos × 2 symbols")
    else:
        # Phase 1: wide sweep (REQ #174)
        param_grid = {
            "atr_period": [7, 10, 14, 20],
            "atr_mult": [1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
            "cooldown_bars": [3, 5, 8, 12],
            "leverage": [2, 3],
        }
        print("Running WIDE sweep (REQ #174 Phase 1): 4×6×4×2 = 192 combos × 2 symbols")

    t0 = time.time()
    results = []
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    # REQ #174: BTC first (primary target), ETH secondary
    symbols = ["BTC/USDT", "ETH/USDT"]
    
    for symbol in symbols:
        print(f"\nLoading {symbol} 1h (365d)...")
        df = load_data(symbol, "1h", 365)
        if df is None or len(df) < 100:
            n = len(df) if df is not None else 0
            print(f"  SKIP: {n} bars")
            continue
        print(f"  {len(df)} bars: {df.index[0]} -> {df.index[-1]}")

        keys, vals = zip(*param_grid.items()) if param_grid else ([], [])
        cnt = 0
        for combo in itertools.product(*vals):
            p = dict(zip(keys, combo))
            cnt += 1
            try:
                sig = supertrend_signals(
                    df, 
                    atr_period=p["atr_period"],
                    atr_mult=p["atr_mult"],
                    cooldown_bars=p["cooldown_bars"])
                res = engine.run(df, sig, leverage=p["leverage"])
                m = res["metrics"]
            except Exception as e:
                if cnt <= 5:
                    print(f"  [{cnt}] ERROR: {e}")
                continue

            results.append({
                "symbol": symbol, **p,
                "total_return_pct": round(m["total_return_pct"], 2),
                "sharpe_ratio": round(m["sharpe_ratio"], 3),
                "max_drawdown_pct": round(m["max_drawdown_pct"], 2),
                "win_rate": round(m["win_rate"], 1),
                "profit_factor": round(m["profit_factor"], 3),
                "total_trades": m["total_trades"],
                "final_equity": round(m["final_equity"], 2),
            })
            if cnt % 50 == 0:
                elapsed = time.time() - t0
                est_total = elapsed / cnt * (len(vals[0]) * len(vals[1]) * len(vals[2]) * len(vals[3]))
                print(f"  [{cnt}] {symbol} ({elapsed:.1f}s elapsed, ~{est_total:.0f}s est total)")

    elapsed = time.time() - t0
    total_combos = sum(len(v) for v in vals) if vals else 0
    total_combos = len(vals[0]) * len(vals[1]) * len(vals[2]) * len(vals[3]) * len(symbols)
    print(f"\nDone: {elapsed:.1f}s, {len(results)}/{total_combos} results")

    if not results:
        print("ERROR: No results generated!")
        return

    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)

    # Top 10
    print("\nTOP 10 Supertrend CONFIGS (by Sharpe)")
    for i, r in enumerate(results[:10]):
        print(f"  {i+1:2d}. {r['symbol']:8s} ATRp={r['atr_period']} ATRm={r['atr_mult']} "
              f"CD={r['cooldown_bars']} Lev={r['leverage']}x "
              f"Net={r['total_return_pct']:+.2f}% Sharpe={r['sharpe_ratio']:+.3f} "
              f"DD={r['max_drawdown_pct']:.1f}% WR={r['win_rate']:.0f}% #T={r['total_trades']}")

    # Verdicts
    verdicts = {}
    pk = list(param_grid.keys())
    for sym in symbols:
        sr = [r for r in results if r["symbol"] == sym]
        if not sr:
            verdicts[sym] = {"verdict": "FAIL", "reason": "no data"}
            continue
        ss = sorted(sr, key=lambda x: x["sharpe_ratio"], reverse=True)
        top = ss[0]
        qual = [r for r in ss if r["sharpe_ratio"] > 0.3 and r["total_trades"] >= 20
                and r["max_drawdown_pct"] < 25 and r["win_rate"] > 35]
        best = qual[0] if qual else top

        if best["sharpe_ratio"] > 0.5 and best["max_drawdown_pct"] < 20 and best["win_rate"] > 40 and best["total_trades"] >= 30:
            v = "LIVE"
        elif best["sharpe_ratio"] > 0.3 and best["total_trades"] >= 20:
            v = "PAPER"
        elif best["total_trades"] < 20:
            v = "INCONCLUSIVE"
        else:
            v = "DO_NOT_ENABLE"

        verdicts[sym] = {
            "verdict": v,
            "best_params": {k: best[k] for k in pk},
            "metrics": {k: best[k] for k in ["total_return_pct", "sharpe_ratio",
                         "max_drawdown_pct", "win_rate", "profit_factor", "total_trades"]},
            "qualified_count": len(qual),
            "total_combos": len(sr),
        }
        print(f"\n{sym}: {v} | ATRp={best['atr_period']} ATRm={best['atr_mult']} "
              f"CD={best['cooldown_bars']} Lev={best['leverage']}x")
        print(f"  Sharpe={best['sharpe_ratio']:+.3f} Net={best['total_return_pct']:+.2f}% "
              f"DD={best['max_drawdown_pct']:.1f}% WR={best['win_rate']:.0f}% #T={best['total_trades']}")

    # Save to backtest_results.json
    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "Supertrend",
        "request_id": 174,
        "run_type": "refined" if getattr(args, 'refined', False) else "wide",
        "lookback_days": 365,
        "timeframe": "1h",
        "symbols": symbols,
        "total_combos": total_combos,
        "results_count": len(results),
        "backtest_completed": datetime.now(timezone.utc).isoformat(),
        "verdicts": verdicts,
        "top10_by_sharpe": results[:10],
        "all_results_summary": {"count": len(results), "elapsed_seconds": round(elapsed, 1)},
    }

    ex_path = "/home/rinnen/binance_quant/.aether/state/backtest_results.json"
    existing = {}
    if os.path.exists(ex_path):
        with open(ex_path) as f:
            existing = json.load(f)
    existing["supertrend_sweep"] = out
    with open(ex_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print("\nSaved to .aether/state/backtest_results.json")

if __name__ == "__main__":
    run()
