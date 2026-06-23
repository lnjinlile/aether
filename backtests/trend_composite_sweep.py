#!/usr/bin/env python3
"""PERF-051: Trend Composite Strategy — Parallel Parameter Sweep.

AND-combines ADX, Donchian breakout, EMA slope, and DI confirmation for entry.
Exit on any trigger (OR logic): ADX exhaustion, reverse breakout, slope reversal,
ATR trailing stop, or TP.

Compares against baseline ADXTrend to measure improvement from multi-indicator
consensus filtering.
"""
import sys, os, json, warnings
os.chdir("/home/rinnen/binance_quant")
sys.path.insert(0, "/home/rinnen/binance_quant")
warnings.filterwarnings("ignore")

from backtest.parallel_sweep import parallel_sweep, SweepConfig
from backtest.signal_gen import trend_composite_signals

# ── Sweep grid (144 combos × 2 symbols = 288 jobs, ~45s on 8 cores) ──
config = SweepConfig(
    signal_fn=trend_composite_signals,
    param_grid={
        "adx_threshold": [20, 22, 25, 28, 30],
        "adx_exit": [18, 20],
        "donchian_period": [15, 20],
        "ema_period": [30, 50],
        "atr_sl_mult": [2.0, 2.5],
        "atr_tp_mult": [4.0, 5.0],
        "cooldown_bars": [2, 3],
        "require_all": [True, False],
    },
    fixed_params={
        "adx_period": 14,
        "atr_period": 14,
    },
    symbols=["BTC/USDT", "ETH/USDT"],
    timeframe="1h",
    leverage=3,
    lookback_days=365,
)

if __name__ == "__main__":
    results = parallel_sweep(config, max_workers=8)

    # Save results
    os.makedirs("data/results", exist_ok=True)
    out_path = "data/results/trend_composite_sweep.json"
    with open(out_path, "w") as f:
        json.dump(
            [{"symbol": r.symbol, "params": r.params,
              "sharpe": r.sharpe_ratio, "dd_pct": r.max_drawdown_pct,
              "return_pct": r.total_return_pct, "trades": r.total_trades,
              "win_rate": r.win_rate, "error": r.error}
             for r in results if r.error is None],
            f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")

    # Summary
    valid = [r for r in results if r.error is None and r.total_trades >= 5]
    valid.sort(key=lambda r: -r.sharpe_ratio)

    print("\n═══ TOP 10 (BTC+ETH, ≥5 trades) ═══")
    for i, r in enumerate(valid[:10]):
        print(f"  #{i+1} {r.symbol} SR={r.sharpe_ratio:.3f} "
              f"DD={r.max_drawdown_pct:.1f}% Ret={r.total_return_pct:+.1f}% "
              f"Trades={r.total_trades} WR={r.win_rate:.0f}% "
              f"ADX>{r.params['adx_threshold']} "
              f"DC={r.params['donchian_period']} "
              f"EMA={r.params['ema_period']} "
              f"SLx={r.params['atr_sl_mult']} "
              f"ALL={r.params['require_all']}")

    # Best by DD constraint
    dd_filtered = [r for r in valid if r.max_drawdown_pct < 20.0 and r.sharpe_ratio > 0]
    dd_filtered.sort(key=lambda r: -r.sharpe_ratio)
    print(f"\n═══ DD < 20% + SR > 0: {len(dd_filtered)} configs ═══")
    for i, r in enumerate(dd_filtered[:5]):
        print(f"  {r.symbol} SR={r.sharpe_ratio:.3f} DD={r.max_drawdown_pct:.1f}% "
              f"Ret={r.total_return_pct:+.1f}% Trades={r.total_trades} "
              f"ADX>{r.params['adx_threshold']} ALL={r.params['require_all']}")

    # Count ALL vs ANY-2
    all_configs = [r for r in valid if r.params['require_all']]
    any2_configs = [r for r in valid if not r.params['require_all']]
    avg_sr_all = avg_dd_all = avg_sr_any2 = avg_dd_any2 = 0.0
    if all_configs:
        avg_sr_all = sum(r.sharpe_ratio for r in all_configs) / len(all_configs)
        avg_dd_all = sum(r.max_drawdown_pct for r in all_configs) / len(all_configs)
    if any2_configs:
        avg_sr_any2 = sum(r.sharpe_ratio for r in any2_configs) / len(any2_configs)
        avg_dd_any2 = sum(r.max_drawdown_pct for r in any2_configs) / len(any2_configs)

    print(f"\n═══ require_all=True: avg SR={avg_sr_all:.3f} DD={avg_dd_all:.1f}% ({len(all_configs)} configs)")
    print(f"═══ require_all=False: avg SR={avg_sr_any2:.3f} DD={avg_dd_any2:.1f}% ({len(any2_configs)} configs)")

    print("\n✅ PERF-051 sweep complete.")
