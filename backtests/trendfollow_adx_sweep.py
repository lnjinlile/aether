#!/usr/bin/env python3
"""PERF-045: TrendFollow_BTC_1h with ADX Regime Filter — Sweep.
Adds ADX-based regime gating to TrendFollow: only take signals when ADX > threshold.
Goal: reduce DD by filtering out RANGING-period whipsaws.
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
from backtest.signal_gen import _compute_adx  # PERF-074: dedup — use centralized ADX

def _check_sl_tp(price, entry, pos, sl_pct, tp_pct):
    if pos == 1:  # Long
        return price <= entry * (1 - sl_pct) or price >= entry * (1 + tp_pct)
    elif pos == -1:  # Short
        return price >= entry * (1 + sl_pct) or price <= entry * (1 - tp_pct)
    return False

def trendfollow_adx_signals(df, ema_period, sl_pct, tp_pct, cooldown_bars,
                             adx_threshold=25, adx_period=14):
    """TrendFollow signals with ADX regime filter.
    Only take signals when ADX > adx_threshold (trending market).
    """
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(close)
    
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]
    
    adx, plus_di, minus_di = _compute_adx(high, low, close, adx_period)
    
    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = max(ema_period * 2, adx_period * 3, 100)
    
    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        slope = ema_slope[i]
        trending = adx[i] > adx_threshold  # Regime gate
        
        if pos == 1:
            exit_trigger = ((not (slope > 0)) or
                            _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct))
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = ((slope > 0) or
                            _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct))
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        
        if pos != 0:
            signals[i] = pos
        
        if pos == 0 and bars_since_trade > cooldown_bars and trending:
            if slope > 0:
                pos = 1
                entry_price = price
                signals[i] = 1
                bars_since_trade = 0
            elif slope < 0:
                pos = -1
                entry_price = price
                signals[i] = -1
                bars_since_trade = 0
    
    return pd.Series(signals, index=df.index)

def run():
    # ── Parameter grid ──
    # From PERF-044, best TrendFollow (without filter): ema=100, sl=0.01-0.015, tp=0.06-0.07, cd=8
    # Add ADX regime filter to reduce DD
    param_grid = {
        "ema_period":       [75, 100],
        "stop_loss_pct":    [0.01, 0.015],
        "take_profit_pct":  [0.05, 0.06, 0.07],
        "cooldown_bars":    [8, 12, 16],
        "adx_threshold":    [20, 25, 30],
        "adx_period":       [14],
        "leverage":         [2, 3],
    }
    # 2 × 2 × 3 × 3 × 3 × 1 × 2 = 216 combos

    total = np.prod([len(v) for v in param_grid.values()])
    print(f"PERF-045: TrendFollow_BTC_1h + ADX Regime Filter")
    print(f"  Grid: {'×'.join(str(len(v)) for v in param_grid.values())} = {total} combos")
    print(f"  Target: DD < 20%, SR > 0")
    print()

    t0 = time.time()
    results = []
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    symbol = "BTC/USDT"
    print(f"Loading {symbol} 1h (365d)...")
    df = load_data(symbol, "1h", 365)
    if df is None or len(df) < 100:
        print(f"  SKIP: insufficient data")
        return
    print(f"  {len(df)} bars: {df.index[0]} -> {df.index[-1]}")

    keys, vals = zip(*param_grid.items())
    cnt = 0
    fail_count = 0

    for combo in itertools.product(*vals):
        p = dict(zip(keys, combo))
        cnt += 1
        try:
            sig = trendfollow_adx_signals(
                df,
                ema_period=p["ema_period"],
                sl_pct=p["stop_loss_pct"],
                tp_pct=p["take_profit_pct"],
                cooldown_bars=p["cooldown_bars"],
                adx_threshold=p["adx_threshold"],
                adx_period=p["adx_period"])
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
        if cnt % 100 == 0:
            elapsed = time.time() - t0
            rate = cnt / elapsed
            remaining = (total - cnt) / rate if rate > 0 else 0
            print(f"  [{cnt}/{total}] {elapsed:.0f}s, ~{remaining:.0f}s left, "
                  f"{len(results)} valid, {fail_count} errors")

    elapsed = time.time() - t0
    print(f"\nDone: {len(results)} valid, {fail_count} errors, {elapsed:.0f}s\n")

    if not results:
        print("ERROR: No valid results!")
        return

    # ── Sort by DD ascending ──
    results.sort(key=lambda r: (r["max_drawdown_pct"], -r["sharpe_ratio"]))

    print("═" * 100)
    print("TOP 20 — Lowest Drawdown (ADX-filtered)")
    print("═" * 100)
    header = f"{'ema':>4} {'sl%':>5} {'tp%':>5} {'cd':>3} {'adx':>4} {'lev':>3} | {'Ret%':>8} {'SR':>6} {'DD%':>6} {'WR%':>4} {'Tr':>4}"
    print(header)
    print("-" * 100)

    dd_under_20 = []
    for i, r in enumerate(results):
        if r["sharpe_ratio"] <= 0:
            continue
        if r["max_drawdown_pct"] < 20:
            dd_under_20.append(r)
        if i >= 20:
            continue
        line = (f"{r['ema_period']:>4} {r['stop_loss_pct']:>5} {r['take_profit_pct']:>5} "
                f"{r['cooldown_bars']:>3} {r['adx_threshold']:>4} {r['leverage']:>3} | "
                f"{r['total_return_pct']:>8.1f} {r['sharpe_ratio']:>6.3f} {r['max_drawdown_pct']:>6.1f} "
                f"{r['win_rate']:>4.0f} {r['total_trades']:>4}")
        print(line)

    print(f"\nConfigs with DD < 20% AND SR > 0: {len(dd_under_20)}/{len(results)}")

    if dd_under_20:
        best = sorted(dd_under_20, key=lambda r: -r["sharpe_ratio"])[0]
        print(f"\n★ Best DD<20% candidate (ADX-filtered):")
        print(f"  ema={best['ema_period']} sl={best['stop_loss_pct']} tp={best['take_profit_pct']} "
              f"cd={best['cooldown_bars']} adx_thresh={best['adx_threshold']} lev={best['leverage']}")
        print(f"  Ret={best['total_return_pct']:.1f}% SR={best['sharpe_ratio']:.3f} "
              f"DD={best['max_drawdown_pct']:.1f}% WR={best['win_rate']:.0f}% Tr={best['total_trades']}")

        out = {
            "perf_id": "PERF-045",
            "strategy": "TrendFollow_BTC_1h_ADX",
            "baseline_no_filter": {"DD_min": 48, "SR": "best 0.29"},
            "grid": {k: list(v) for k, v in param_grid.items()},
            "total_combos": total,
            "valid_results": len(results),
            "dd_under_20_count": len(dd_under_20),
            "best_dd_under_20": best,
            "top_20": results[:20],
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(".aether/state/perf_045_trendfollow_adx_sweep.json", "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nResults saved to .aether/state/perf_045_trendfollow_adx_sweep.json")
    else:
        print("\n⚠️  NO config with DD < 20% AND SR > 0 with ADX filter!")
        # Convert numpy types to native Python for JSON
        top10_clean = []
        for r in results[:10]:
            top10_clean.append({k: (int(v) if isinstance(v, (np.integer,)) else
                                    float(v) if isinstance(v, (np.floating,)) else v)
                               for k, v in r.items()})
        out = {
            "perf_id": "PERF-045",
            "strategy": "TrendFollow_BTC_1h_ADX",
            "verdict": "NO_VIABLE_CONFIG",
            "grid": {k: [int(x) for x in v] for k, v in param_grid.items()},
            "total_combos": int(total),
            "valid_results": len(results),
            "dd_min": float(min(r["max_drawdown_pct"] for r in results)),
            "sr_max": float(max(r["sharpe_ratio"] for r in results)),
            "top_10": top10_clean,
            "run_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(".aether/state/perf_045_trendfollow_adx_sweep.json", "w") as f:
            json.dump(out, f, indent=2)

if __name__ == "__main__":
    run()
