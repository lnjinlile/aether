#!/usr/bin/env python3
"""PERF-044: TrendFollow_BTC_1h Parameter Sweep — target DD<20%, SR>0.
Athena REQ: BTC trend strategy for regime diversification.
Closest candidate: TrendFollow_BTC_1h (+38.45%, SR=0.39, DD=28.3%, 50t).
Goal: find params that push DD under 20% while keeping positive SR.
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
from backtest.signal_gen import trendfollow_signals

def run():
    # ── Parameter grid ──
    # Current config: ema=50, sl=1.5%, tp=5%, cd=8, lev=3 → SR=0.39, DD=28.3%
    # Strategy: reduce DD by:
    #  1) tighter stops (0.01) → limit per-trade loss
    #  2) longer ema (75-100) → filter noise/whiplash
    #  3) longer cooldown → prevent re-entry after exit
    #  4) lower leverage (2x) → halve exposure
    param_grid = {
        "ema_period":       [40, 50, 60, 75, 100],
        "stop_loss_pct":    [0.01, 0.015, 0.02],
        "take_profit_pct":  [0.04, 0.05, 0.06, 0.07],
        "cooldown_bars":    [5, 8, 12, 16],
        "leverage":         [2, 3],
    }
    # 5 × 3 × 4 × 4 × 2 = 480 combos

    total = np.prod([len(v) for v in param_grid.values()])
    print(f"PERF-044: TrendFollow_BTC_1h Parameter Sweep")
    print(f"  Grid: {'×'.join(str(len(v)) for v in param_grid.values())} = {total} combos")
    print(f"  Target: DD < 20%, SR > 0")
    print(f"  Baseline: ema=50 sl=1.5% tp=5% cd=8 lev=3 → SR=0.39 DD=28.3%")
    print()

    t0 = time.time()
    results = []
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    symbol = "BTC/USDT"
    print(f"Loading {symbol} 1h (365d)...")
    df = load_data(symbol, "1h", 365)
    if df is None or len(df) < 100:
        print(f"  SKIP: insufficient data ({len(df) if df is not None else 0} bars)")
        return
    print(f"  {len(df)} bars: {df.index[0]} -> {df.index[-1]}")

    keys, vals = zip(*param_grid.items())
    cnt = 0
    fail_count = 0

    for combo in itertools.product(*vals):
        p = dict(zip(keys, combo))
        cnt += 1
        try:
            sig = trendfollow_signals(
                df,
                ema_period=p["ema_period"],
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
        if cnt % 200 == 0:
            elapsed = time.time() - t0
            rate = cnt / elapsed
            remaining = (total - cnt) / rate if rate > 0 else 0
            print(f"  [{cnt}/{total}] {elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining, "
                  f"{len(results)} valid, {fail_count} errors")

    elapsed = time.time() - t0
    print(f"\nDone: {len(results)} valid, {fail_count} errors, {elapsed:.0f}s total\n")

    if not results:
        print("ERROR: No valid results!")
        return

    # ── Sort by DD ascending, then SR descending ──
    results.sort(key=lambda r: (r["max_drawdown_pct"], -r["sharpe_ratio"]))

    # ── Top 20 lowest DD configs ──
    print("═" * 90)
    print("TOP 20 — Lowest Drawdown (target: DD < 20% + SR > 0)")
    print("═" * 90)
    header = f"{'ema':>4} {'sl%':>5} {'tp%':>5} {'cd':>3} {'lev':>3} | {'Ret%':>8} {'SR':>6} {'DD%':>6} {'WR%':>5} {'PF':>5} {'Tr':>4}"
    print(header)
    print("-" * 90)

    shown = 0
    best_return = None
    best_dd = None
    for r in results:
        if r["sharpe_ratio"] <= 0:
            continue
        if shown >= 20:
            break
        line = (f"{r['ema_period']:>4} {r['stop_loss_pct']:>5} {r['take_profit_pct']:>5} "
                f"{r['cooldown_bars']:>3} {r['leverage']:>3} | "
                f"{r['total_return_pct']:>8.1f} {r['sharpe_ratio']:>6.3f} {r['max_drawdown_pct']:>6.1f} "
                f"{r['win_rate']:>5.0f} {r['profit_factor']:>5.2f} {r['total_trades']:>4}")
        print(line)
        shown += 1
        if best_return is None:
            best_return = r
        if r["max_drawdown_pct"] < 20 and best_dd is None:
            best_dd = r

    print()

    # ── Best Sharpe configs ──
    results_sr = sorted(results, key=lambda r: -r["sharpe_ratio"])
    print("═" * 90)
    print("TOP 10 — Best Sharpe Ratio")
    print("═" * 90)
    print(header)
    print("-" * 90)
    for r in results_sr[:10]:
        line = (f"{r['ema_period']:>4} {r['stop_loss_pct']:>5} {r['take_profit_pct']:>5} "
                f"{r['cooldown_bars']:>3} {r['leverage']:>3} | "
                f"{r['total_return_pct']:>8.1f} {r['sharpe_ratio']:>6.3f} {r['max_drawdown_pct']:>6.1f} "
                f"{r['win_rate']:>5.0f} {r['profit_factor']:>5.2f} {r['total_trades']:>4}")
        print(line)
    print()

    # ── Summary ──
    dd_under_20 = [r for r in results if r["max_drawdown_pct"] < 20 and r["sharpe_ratio"] > 0]
    dd_under_15 = [r for r in results if r["max_drawdown_pct"] < 15 and r["sharpe_ratio"] > 0]
    print(f"Configs with DD < 20% AND SR > 0: {len(dd_under_20)}/{len(results)}")
    print(f"Configs with DD < 15% AND SR > 0: {len(dd_under_15)}/{len(results)}")

    # ── Pre-compute top lists (needed in both branches) ──
    top10_dd_raw = results[:10]
    top10_sr_raw = results_sr[:10]
    
    top10_dd = []
    for r in top10_dd_raw:
        top10_dd.append({k: (int(v) if isinstance(v, (np.integer,)) else
                             float(v) if isinstance(v, (np.floating,)) else v)
                        for k, v in r.items()})
    top10_sr = []
    for r in top10_sr_raw:
        top10_sr.append({k: (int(v) if isinstance(v, (np.integer,)) else
                             float(v) if isinstance(v, (np.floating,)) else v)
                        for k, v in r.items()})

    if dd_under_20:
        best = sorted(dd_under_20, key=lambda r: -r["sharpe_ratio"])[0]
        print(f"\n★ Best DD<20% candidate:")
        print(f"  ema={best['ema_period']} sl={best['stop_loss_pct']} tp={best['take_profit_pct']} "
              f"cd={best['cooldown_bars']} lev={best['leverage']}")
        print(f"  Ret={best['total_return_pct']:.1f}% SR={best['sharpe_ratio']:.3f} "
              f"DD={best['max_drawdown_pct']:.1f}% WR={best['win_rate']:.0f}% "
              f"Tr={best['total_trades']} PF={best['profit_factor']:.2f}")

        # ── Save results ──
        out = {
            "perf_id": "PERF-044",
            "strategy": "TrendFollow_BTC_1h",
            "baseline": {"ema": 50, "sl": 0.015, "tp": 0.05, "cd": 8, "lev": 3,
                        "SR": 0.39, "DD": 28.3, "Ret": 38.45, "WR": 34, "Tr": 50},
            "grid": {k: [int(x) if isinstance(x, (np.integer,)) else x for x in v] for k, v in param_grid.items()},
            "total_combos": int(total),
            "valid_results": len(results),
            "dd_under_20_count": len(dd_under_20),
            "dd_min": float(min(r["max_drawdown_pct"] for r in results)),
            "sr_max": float(max(r["sharpe_ratio"] for r in results)),
            "verdict": "NO_VIABLE_CONFIG" if not dd_under_20 else "PROMISING",
            "top_10_dd": top10_dd,
            "top_10_sr": top10_sr,
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(".aether/state/perf_044_trendfollow_sweep.json", "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nResults saved to .aether/state/perf_044_trendfollow_sweep.json")
    else:
        print("\n⚠️  NO config with DD < 20% AND SR > 0 found!")
        print("   TrendFollow_BTC_1h cannot meet PAPER threshold via param tuning alone.")
        # Still save results for documentation
        out = {
            "perf_id": "PERF-044",
            "strategy": "TrendFollow_BTC_1h",
            "verdict": "NO_VIABLE_CONFIG",
            "baseline": {"ema": 50, "sl": 0.015, "tp": 0.05, "cd": 8, "lev": 3,
                        "SR": 0.39, "DD": 28.3, "Ret": 38.45, "WR": 34, "Tr": 50},
            "grid": {k: [int(x) if isinstance(x, (np.integer,)) else x for x in v] for k, v in param_grid.items()},
            "total_combos": int(total),
            "valid_results": len(results),
            "dd_min": float(min(r["max_drawdown_pct"] for r in results)),
            "sr_max": float(max(r["sharpe_ratio"] for r in results)),
            "top_10_dd": top10_dd[:10],
            "top_10_sr": top10_sr[:10],
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(".aether/state/perf_044_trendfollow_sweep.json", "w") as f:
            json.dump(out, f, indent=2)
        print(f"Results saved to .aether/state/perf_044_trendfollow_sweep.json")

if __name__ == "__main__":
    run()
