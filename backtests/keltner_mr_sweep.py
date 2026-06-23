#!/usr/bin/env python3
"""KeltnerMR Parameters Sweep — standalone module, run via python3 -m backtests.keltner_mr_sweep"""
import sys, os, json, itertools, time, warnings
os.chdir("/home/rinnen/binance_quant")
sys.path.insert(0, "/home/rinnen/binance_quant")
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from datetime import datetime, timezone
from backtest.sweep_utils import load_data
from backtest.engine import BacktestEngine
from backtest.signal_gen import keltner_mr_signals

def run():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--refined", action="store_true", help="Athena Request #66 refined sweep")
    args, _ = ap.parse_known_args()

    if args.refined:
        # PERF-025a / Request #66: wider channel, tighter band, tighter stop => reduce DD
        param_grid = {
            "kc_period": [20, 24, 30], "atr_mult": [1.5, 1.75, 2.0], "atr_period": [14],
            "rsi_period": [14], "oversold": [20, 25], "overbought": [70, 75, 80],
            "cooldown_bars": [3, 5], "stop_loss_pct": [0.01, 0.015],
            "take_profit_pct": [0.02, 0.03], "exit_level": [50], "leverage": [3],
        }
        print("Running REFINED sweep (Req #66): 3×3×2×3×2×2×2 = 432 combos × 2 symbols")
    else:
        param_grid = {
            "kc_period": [14, 20], "atr_mult": [2.0, 2.5], "atr_period": [14],
            "rsi_period": [14], "oversold": [20, 25], "overbought": [70, 75, 80],
            "cooldown_bars": [3, 5], "stop_loss_pct": [0.015, 0.02],
            "take_profit_pct": [0.03, 0.04], "exit_level": [50], "leverage": [3],
        }

    t0 = time.time()
    results = []
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    for symbol in ["BTC/USDT", "ETH/USDT"]:
        print(f"Loading {symbol} 1h (365d)...")
        df = load_data(symbol, "1h", 365)
        if df is None or len(df) < 100:
            n = len(df) if df is not None else 0
            print(f"  SKIP: {n} bars")
            continue
        print(f"  {len(df)} bars: {df.index[0]} -> {df.index[-1]}")

        keys, vals = zip(*param_grid.items())
        cnt = 0
        for combo in itertools.product(*vals):
            p = dict(zip(keys, combo))
            cnt += 1
            try:
                sig = keltner_mr_signals(
                    df, p["kc_period"], p["atr_mult"], p["atr_period"],
                    p["rsi_period"], p["oversold"], p["overbought"],
                    p["exit_level"], p["stop_loss_pct"], p["take_profit_pct"],
                    p["cooldown_bars"])
                res = engine.run(df, sig, leverage=p["leverage"])
                m = res["metrics"]
            except Exception:
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
                print(f"  [{cnt}/192] {symbol} ({time.time()-t0:.1f}s)")

    elapsed = time.time() - t0
    print(f"Done: {elapsed:.1f}s, {len(results)} results")

    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)

    # Top 10
    print("\nTOP 10 KeltnerMR CONFIGS (by Sharpe)")
    for i, r in enumerate(results[:10]):
        print(f"  {i+1:2d}. {r['symbol']:8s} KC={r['kc_period']} ATRm={r['atr_mult']} "
              f"OS={r['oversold']} OB={r['overbought']} CD={r['cooldown_bars']} "
              f"SL={r['stop_loss_pct']*100:.1f}% TP={r['take_profit_pct']*100:.1f}% "
              f"Net={r['total_return_pct']:+.2f}% Sharpe={r['sharpe_ratio']:+.3f} "
              f"DD={r['max_drawdown_pct']:.1f}% WR={r['win_rate']:.0f}% #T={r['total_trades']}")

    # Verdicts
    verdicts = {}
    pk = list(param_grid.keys())
    for sym in ["BTC/USDT", "ETH/USDT"]:
        sr = [r for r in results if r["symbol"] == sym]
        if not sr:
            verdicts[sym] = {"verdict": "FAIL", "reason": "no data"}
            continue
        ss = sorted(sr, key=lambda x: x["sharpe_ratio"], reverse=True)
        top = ss[0]
        qual = [r for r in ss if r["sharpe_ratio"] > 0.3 and r["total_trades"] >= 20
                and r["max_drawdown_pct"] < 25 and r["win_rate"] > 35]
        best = qual[0] if qual else top

        if best["sharpe_ratio"] > 0.5 and best["max_drawdown_pct"] < 20 and best["win_rate"] > 40:
            v = "LIVE"
        elif best["sharpe_ratio"] > 0.3 and best["total_trades"] >= 20:
            v = "PAPER"
        elif best["total_trades"] < 20:
            v = "INCONCLUSIVE"
        else:
            v = "FAIL"

        verdicts[sym] = {
            "verdict": v,
            "best_params": {k: best[k] for k in pk},
            "metrics": {k: best[k] for k in ["total_return_pct", "sharpe_ratio",
                         "max_drawdown_pct", "win_rate", "profit_factor", "total_trades"]},
            "qualified_count": len(qual),
            "total_combos": len(sr),
        }
        print(f"\n{sym}: {v} | KC={best['kc_period']} ATRm={best['atr_mult']} "
              f"OS={best['oversold']} OB={best['overbought']} CD={best['cooldown_bars']} "
              f"SL={best['stop_loss_pct']*100:.1f}% TP={best['take_profit_pct']*100:.1f}%")
        print(f"  Sharpe={best['sharpe_ratio']:+.3f} Net={best['total_return_pct']:+.2f}% "
              f"DD={best['max_drawdown_pct']:.1f}% WR={best['win_rate']:.0f}% #T={best['total_trades']}")

    # Save
    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "KeltnerMR", "request_id": 66 if getattr(args, 'refined', False) else 173,
        "run_type": "refined" if getattr(args, 'refined', False) else "initial",
        "lookback_days": 365, "timeframe": "1h",
        "symbols": ["BTC/USDT", "ETH/USDT"],
        "total_combos": len(results),
        "backtest_completed": datetime.now(timezone.utc).isoformat(),
        "verdicts": verdicts, "top10_by_sharpe": results[:10],
        "all_results_summary": {"count": len(results), "elapsed_seconds": round(elapsed, 1)},
    }

    ex_path = "/home/rinnen/binance_quant/.aether/state/backtest_results.json"
    existing = {}
    if os.path.exists(ex_path):
        with open(ex_path) as f:
            existing = json.load(f)
    existing["keltner_mr_sweep"] = out
    with open(ex_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print("\nSaved to .aether/state/backtest_results.json")

if __name__ == "__main__":
    run()
