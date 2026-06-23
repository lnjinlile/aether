#!/usr/bin/env python3
"""RSI Mean Reversion Parameters Sweep — standalone module.
Athena REQ #174 Phase 2: RSI_MR_BTC full parameter sweep for BTC diversification.
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
from backtest.signal_gen import rsi_mr_signals

def run():
    # REQ #174 Phase 2: Full parameter sweep for RSI_MR_BTC
    # BTC uses different ranges than ETH — wider exploration needed
    param_grid = {
        "rsi_period": [14, 20],
        "oversold": [20, 25, 30],
        "overbought": [65, 70, 75, 80],
        "exit_rsi": [45, 50, 55],
        "stop_loss_pct": [0.01, 0.015, 0.02, 0.03],
        "take_profit_pct": [0.02, 0.03, 0.04, 0.06],
        "cooldown_bars": [3, 5, 8],
        "leverage": [2, 3],
    }
    # 2 × 3 × 4 × 3 × 4 × 4 × 3 × 2 = 6912 combos
    # But many will fail (no trades), so practical throughput is lower
    
    print(f"Running RSI_MR full sweep (REQ #174 Phase 2): "
          f"{'×'.join(str(len(v)) for v in param_grid.values())} = "
          f"{np.prod([len(v) for v in param_grid.values()])} combos")
    print("Target: BTC/USDT only (ETH already has LIVE config)")

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
    total = np.prod([len(v) for v in vals])
    cnt = 0
    fail_count = 0
    
    for combo in itertools.product(*vals):
        p = dict(zip(keys, combo))
        cnt += 1
        try:
            sig = rsi_mr_signals(
                df, 
                rsi_period=p["rsi_period"],
                oversold=p["oversold"],
                overbought=p["overbought"],
                exit_rsi=p["exit_rsi"],
                sl_pct=p["stop_loss_pct"],
                tp_pct=p["take_profit_pct"],
                cooldown_bars=p["cooldown_bars"])
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
            rate = cnt / elapsed
            remaining = (total - cnt) / rate
            print(f"  [{cnt}/{total}] {(elapsed):.0f}s elapsed, ~{remaining:.0f}s remaining, "
                  f"{len(results)} valid results, {fail_count} errors")

    elapsed = time.time() - t0
    print(f"\nDone: {elapsed:.1f}s, {len(results)}/{total} valid results, {fail_count} errors")

    if not results:
        print("ERROR: No results generated!")
        return

    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)

    # Top 10
    print("\nTOP 10 RSI_MR_BTC CONFIGS (by Sharpe)")
    for i, r in enumerate(results[:10]):
        print(f"  {i+1:2d}. RSIp={r['rsi_period']} OS={r['oversold']} OB={r['overbought']} "
              f"Exit={r['exit_rsi']} SL={r['stop_loss_pct']*100:.1f}% TP={r['take_profit_pct']*100:.1f}% "
              f"CD={r['cooldown_bars']} Lev={r['leverage']}x "
              f"Net={r['total_return_pct']:+.2f}% Sharpe={r['sharpe_ratio']:+.3f} "
              f"DD={r['max_drawdown_pct']:.1f}% WR={r['win_rate']:.0f}% #T={r['total_trades']}")

    # Verdict
    pk = list(param_grid.keys())
    sr = results
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

    verdict = {
        "verdict": v,
        "best_params": {k: best[k] for k in pk},
        "metrics": {k: best[k] for k in ["total_return_pct", "sharpe_ratio",
                     "max_drawdown_pct", "win_rate", "profit_factor", "total_trades"]},
        "qualified_count": len(qual),
        "total_combos": len(sr),
        "total_attempted": total,
        "errors": fail_count,
    }
    print(f"\n{symbol}: {v}")
    print(f"  Best params: RSIp={best['rsi_period']} OS={best['oversold']} OB={best['overbought']} "
          f"Exit={best['exit_rsi']} SL={best['stop_loss_pct']*100:.1f}% TP={best['take_profit_pct']*100:.1f}% "
          f"CD={best['cooldown_bars']} Lev={best['leverage']}x")
    print(f"  Sharpe={best['sharpe_ratio']:+.3f} Net={best['total_return_pct']:+.2f}% "
          f"DD={best['max_drawdown_pct']:.1f}% WR={best['win_rate']:.0f}% #T={best['total_trades']}")
    if v == "DO_NOT_ENABLE":
        print(f"  ⚠️ No config meets minimum criteria. {len(qual)} qualified (SR>0.3, T>=20, DD<25%, WR>35%).")
        # Show best qualified if any
        if qual:
            qbest = qual[0]
            print(f"  Best qualified: RSIp={qbest['rsi_period']} OS={qbest['oversold']} OB={qbest['overbought']} "
                  f"Sharpe={qbest['sharpe_ratio']:+.3f} DD={qbest['max_drawdown_pct']:.1f}% "
                  f"WR={qbest['win_rate']:.0f}% T={qbest['total_trades']}")

    # Save
    out = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "RSI_MR",
        "request_id": 174,
        "run_type": "full_sweep_phase2",
        "lookback_days": 365,
        "timeframe": "1h",
        "symbol": symbol,
        "total_combos": total,
        "results_count": len(results),
        "errors": fail_count,
        "backtest_completed": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "top10_by_sharpe": results[:10],
        "all_results_summary": {"count": len(results), "elapsed_seconds": round(elapsed, 1)},
    }

    ex_path = "/home/rinnen/binance_quant/.aether/state/backtest_results.json"
    existing = {}
    if os.path.exists(ex_path):
        with open(ex_path) as f:
            existing = json.load(f)
    existing["rsi_mr_sweep"] = out
    with open(ex_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    print("\nSaved to .aether/state/backtest_results.json")

if __name__ == "__main__":
    run()
