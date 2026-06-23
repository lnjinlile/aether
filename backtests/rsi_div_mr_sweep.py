#!/usr/bin/env python3
"""PERF-076: RSI Divergence MR Sweep — BTC focused.
Tests whether RSI divergence confirmation improves SR beyond 0.45 ceiling.
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
from backtest.signal_gen import rsi_divergence_mr_signals, rsi_mr_signals

def run():
    # Baseline: standard RSI MR with best known params
    # Divergence variant: same params + divergence filter
    param_grid = {
        "rsi_period": [14, 16],
        "oversold": [18, 20, 22],
        "overbought": [80],
        "exit_rsi": [40, 45, 50],
        "stop_loss_pct": [0.015, 0.02],
        "take_profit_pct": [0.05, 0.06],
        "cooldown_bars": [3, 5],
        "div_lookback": [20, 30, 40],
        "require_divergence": [True, False],
        "leverage": [2, 3],
    }

    total = np.prod([len(v) for v in param_grid.values()])
    print(f"RSI Divergence MR Sweep (PERF-076): "
          f"{'×'.join(str(len(v)) for v in param_grid.values())} = {total} combos")
    print("Target: SR > 0.5 (break 0.45 ceiling)")

    t0 = time.time()
    results = []
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    symbol = "BTC/USDT"

    print(f"\nLoading {symbol} 1h (365d)...")
    df = load_data(symbol, "1h", 365)
    if df is None or len(df) < 100:
        print(f"  SKIP: insufficient data")
        return
    print(f"  {len(df)} bars: {df.index[0]} -> {df.index[-1]}")

    keys, vals = zip(*param_grid.items())
    cnt = 0
    fail_count = 0
    baseline_sharpe = 0.466  # best known standard RSI MR BTC

    for combo in itertools.product(*vals):
        p = dict(zip(keys, combo))
        cnt += 1
        try:
            sig = rsi_divergence_mr_signals(
                df,
                rsi_period=p["rsi_period"],
                oversold=p["oversold"],
                overbought=p["overbought"],
                exit_rsi=p["exit_rsi"],
                sl_pct=p["stop_loss_pct"],
                tp_pct=p["take_profit_pct"],
                cooldown_bars=p["cooldown_bars"],
                div_lookback=p["div_lookback"],
                require_divergence=p["require_divergence"],
                div_rsi_max=40.0)
            res = engine.run(df, sig, leverage=p["leverage"])
            m = res["metrics"]
        except Exception:
            fail_count += 1
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
        if cnt % 500 == 0:
            elapsed = time.time() - t0
            rate = cnt / elapsed if elapsed > 0 else 1
            remaining = (total - cnt) / rate if rate > 0 else 0
            best_so_far = max(r["sharpe_ratio"] for r in results) if results else 0
            print(f"  [{cnt}/{total}] {elapsed:.0f}s, ~{remaining:.0f}s left, "
                  f"best SR={best_so_far:.3f}, {fail_count} err")

    elapsed = time.time() - t0
    print(f"\nDone: {elapsed:.1f}s, {len(results)}/{total} valid, {fail_count} errors")

    if not results:
        print("ERROR: No results!")
        return

    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)

    # Top 15
    print("\nTOP 15 RSI_DIV_MR BTC (by Sharpe)")
    for i, r in enumerate(results[:15]):
        div = "DIV" if r["require_divergence"] else "PLAIN"
        print(f"  {i+1:2d}. [{div}] RSIp={r['rsi_period']} OS={r['oversold']} OB={r['overbought']} "
              f"Exit={r['exit_rsi']} SL={r['stop_loss_pct']*100:.1f}% TP={r['take_profit_pct']*100:.1f}% "
              f"CD={r['cooldown_bars']} LB={r['div_lookback']} Lev={r['leverage']}x "
              f"Net={r['total_return_pct']:+.2f}% Sharpe={r['sharpe_ratio']:+.3f} "
              f"DD={r['max_drawdown_pct']:.1f}% WR={r['win_rate']:.0f}% #T={r['total_trades']}")

    # Compare divergence vs plain
    div_results = [r for r in results if r["require_divergence"]]
    plain_results = [r for r in results if not r["require_divergence"]]
    if div_results and plain_results:
        best_div = max(div_results, key=lambda x: x["sharpe_ratio"])
        best_plain = max(plain_results, key=lambda x: x["sharpe_ratio"])
        print(f"\n=== Divergence vs Plain Comparison ===")
        print(f"  Best DIV:   SR={best_div['sharpe_ratio']:+.3f} DD={best_div['max_drawdown_pct']:.1f}% "
              f"WR={best_div['win_rate']:.0f}% T={best_div['total_trades']}")
        print(f"  Best PLAIN: SR={best_plain['sharpe_ratio']:+.3f} DD={best_plain['max_drawdown_pct']:.1f}% "
              f"WR={best_plain['win_rate']:.0f}% T={best_plain['total_trades']}")
        delta = best_div["sharpe_ratio"] - best_plain["sharpe_ratio"]
        print(f"  Delta: {delta:+.3f} ({'✅ IMPROVED' if delta > 0.02 else '❌ NO IMPROVEMENT' if delta <= 0 else '⚠️ MARGINAL'})")

    # Best overall
    best = results[0]
    qual = [r for r in results if r["sharpe_ratio"] > 0.3 and r["total_trades"] >= 20
            and r["max_drawdown_pct"] < 25 and r["win_rate"] > 35]
    print(f"\n  Qualified (SR>0.3, T>=20, DD<25%, WR>35%): {len(qual)}")

    if best["sharpe_ratio"] > 0.5 and best["max_drawdown_pct"] < 20 and best["total_trades"] >= 30:
        v = "BREAKTHROUGH"
    elif best["sharpe_ratio"] > baseline_sharpe + 0.02:
        v = "IMPROVED"
    elif best["sharpe_ratio"] > baseline_sharpe:
        v = "MARGINAL"
    else:
        v = "NO_IMPROVEMENT"

    pk = [k for k in param_grid.keys()]
    verdict = {
        "verdict": v,
        "best_params": {k: best[k] for k in pk},
        "metrics": {k: best[k] for k in ["total_return_pct", "sharpe_ratio",
                     "max_drawdown_pct", "win_rate", "profit_factor", "total_trades"]},
        "qualified_count": len(qual),
        "total_combos": len(results),
        "total_attempted": total,
        "errors": fail_count,
        "baseline_sharpe": baseline_sharpe,
        "improvement": round(best["sharpe_ratio"] - baseline_sharpe, 3),
    }
    print(f"\n{symbol}: {v}")
    print(f"  Best: {'DIV' if best['require_divergence'] else 'PLAIN'} "
          f"RSIp={best['rsi_period']} OS={best['oversold']} OB={best['overbought']} "
          f"Exit={best['exit_rsi']} SL={best['stop_loss_pct']*100:.1f}% TP={best['take_profit_pct']*100:.1f}% "
          f"CD={best['cooldown_bars']} LB={best['div_lookback']} Lev={best['leverage']}x")
    print(f"  Sharpe={best['sharpe_ratio']:+.3f} (baseline={baseline_sharpe:+.3f}, "
          f"Δ={best['sharpe_ratio']-baseline_sharpe:+.3f})")
    print(f"  Net={best['total_return_pct']:+.2f}% DD={best['max_drawdown_pct']:.1f}% "
          f"WR={best['win_rate']:.0f}% #T={best['total_trades']}")

    # Save
    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "RSI_Divergence_MR",
        "run_type": "rsi_div_mr_sweep",
        "perf_id": "PERF-076",
        "lookback_days": 365,
        "timeframe": "1h",
        "symbol": symbol,
        "total_combos": total,
        "results_count": len(results),
        "errors": fail_count,
        "backtest_completed": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "top15_by_sharpe": results[:15],
        "elapsed_seconds": round(elapsed, 1),
    }

    ex_path = "/home/rinnen/binance_quant/.aether/state/backtest_results.json"
    existing = {}
    if os.path.exists(ex_path):
        with open(ex_path) as f:
            existing = json.load(f)
    existing["rsi_div_mr_sweep"] = out
    with open(ex_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print(f"\nSaved to .aether/state/backtest_results.json → rsi_div_mr_sweep")

    return verdict

if __name__ == "__main__":
    run()
