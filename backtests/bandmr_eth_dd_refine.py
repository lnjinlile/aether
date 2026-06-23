#!/usr/bin/env python3
"""BandMR_ETH DD Refinement Sweep — PERF-064
Athena request: DD=30.4%→<20%, keep SR>0.5.
Targeted param space: DP=[8,12,14], OS=[25,28,30], SL=[1.0%,1.5%], TP=[2.0%,2.5%,3.0%], CD=[3,5,8]
3×3×2×3×3 = 162 combos × 365d window. ETH only.
"""
import sys, os, json, itertools, time as _time
from datetime import datetime, timezone
from dataclasses import dataclass
sys.path.insert(0, '/home/rinnen/binance_quant')

import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.signal_gen import donchian_mr_signals


@dataclass
class SweepResult:
    symbol: str
    donchian_period: int
    oversold: float
    stop_loss_pct: float
    take_profit_pct: float
    cooldown_bars: int
    window_days: int
    net_pct: float
    sharpe: float
    max_dd: float
    win_rate: float
    pf: float
    trades: int


def verdict(net_pct, sharpe, max_dd, win_rate, trades):
    if trades < 5:
        return "INSUFFICIENT"
    if max_dd > 20:
        return "DD_FAIL"
    if sharpe < 0.5:
        return "SR_FAIL"
    if win_rate < 40:
        return "WR_FAIL"
    return "PASS"


def main():
    storage = MarketStorage()
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    sym = "ETH/USDT"
    tf = "1h"
    df_raw = storage.load_klines(sym, tf)
    if df_raw is None or df_raw.empty:
        print("No data!")
        return

    df_raw['open_time'] = pd.to_datetime(df_raw['open_time'], unit='ms')
    df_raw.set_index('open_time', inplace=True)
    df_raw.sort_index(inplace=True)

    # Param grid — refined per Athena request
    donchian_periods = [8, 12, 14]
    oversolds = [25, 28, 30]
    stop_loss_pcts = [0.01, 0.015]       # 1.0%, 1.5%
    take_profit_pcts = [0.02, 0.025, 0.03]  # 2.0%, 2.5%, 3.0%
    cooldowns = [3, 5, 8]

    window_days = 365
    cutoff = df_raw.index[-1] - pd.Timedelta(days=window_days)
    df = df_raw[df_raw.index >= cutoff].copy()
    if len(df) < 50:
        print(f"Insufficient data: {len(df)} bars")
        return

    total = len(donchian_periods) * len(oversolds) * len(stop_loss_pcts) * len(take_profit_pcts) * len(cooldowns)
    print(f"BandMR_ETH DD Refine: {total} combos, {window_days}d window")
    print(f"Grid: DP{donchian_periods} × OS{oversolds} × SL{[f'{x*100:.0f}%' for x in stop_loss_pcts]} × TP{[f'{x*100:.0f}%' for x in take_profit_pcts]} × CD{cooldowns}")

    results = []
    t0 = _time.time()
    n = 0
    for dp, os_val, sl, tp, cd in itertools.product(
        donchian_periods, oversolds, stop_loss_pcts, take_profit_pcts, cooldowns
    ):
        try:
            signals = donchian_mr_signals(
                df,
                donchian_period=dp,
                oversold=float(os_val),
                overbought=80.0,
                stop_loss_pct=sl,
                take_profit_pct=tp,
                cooldown_bars=cd,
                volume_filter=1.2,
            )
            bt_result = engine.run(df, signals, leverage=3)
            m = bt_result['metrics']
            r = SweepResult(
                symbol=sym,
                donchian_period=dp,
                oversold=float(os_val),
                stop_loss_pct=sl,
                take_profit_pct=tp,
                cooldown_bars=cd,
                window_days=window_days,
                net_pct=m['total_return_pct'],
                sharpe=m['sharpe_ratio'],
                max_dd=m['max_drawdown_pct'],
                win_rate=m['win_rate'],
                pf=m['profit_factor'],
                trades=m['total_trades'],
            )
            results.append(r)
            n += 1
        except Exception as e:
            n += 1
            continue

    elapsed = _time.time() - t0
    print(f"Ran {n}/{total} combos in {elapsed:.1f}s")

    # Sort by max_dd ascending (lower DD is better), then by sharpe descending
    results.sort(key=lambda r: (r.max_dd, -r.sharpe))

    # Print top candidates
    print("\n=== Top DD candidates (DD<20%, SR>0.5 preferred) ===")
    printed = 0
    passing = []
    for r in results:
        v = verdict(r.net_pct, r.sharpe, r.max_dd, r.win_rate, r.trades)
        if v == "PASS":
            passing.append(r)
        if printed < 15:
            print(f"  DP={r.donchian_period:2d} OS={r.oversold:.0f} "
                  f"SL={r.stop_loss_pct*100:.1f}% TP={r.take_profit_pct*100:.1f}% CD={r.cooldown_bars} | "
                  f"Net={r.net_pct:+.1f}% SR={r.sharpe:.3f} DD={r.max_dd:.1f}% "
                  f"WR={r.win_rate:.1f}% T={r.trades} PF={r.pf:.2f}  [{v}]")
            printed += 1

    print(f"\nPASS (DD<20% & SR>0.5 & WR>40%): {len(passing)}/{len(results)}")

    # Best overall (prefer PASS, fallback to lowest DD)
    if passing:
        best = min(passing, key=lambda r: r.max_dd)
        print(f"\n★ BEST: DP={best.donchian_period} OS={best.oversold:.0f} "
              f"SL={best.stop_loss_pct*100:.1f}% TP={best.take_profit_pct*100:.1f}% CD={best.cooldown_bars} "
              f"→ Net={best.net_pct:+.1f}% SR={best.sharpe:.3f} DD={best.max_dd:.1f}% "
              f"WR={best.win_rate:.1f}% T={best.trades} PF={best.pf:.2f}")
    else:
        # No pass — show best DD
        best = results[0]
        print(f"\n★ BEST (no PASS): DP={best.donchian_period} OS={best.oversold:.0f} "
              f"SL={best.stop_loss_pct*100:.1f}% TP={best.take_profit_pct*100:.1f}% CD={best.cooldown_bars} "
              f"→ Net={best.net_pct:+.1f}% SR={best.sharpe:.3f} DD={best.max_dd:.1f}% "
              f"WR={best.win_rate:.1f}% T={best.trades} PF={best.pf:.2f}")

    # Write results
    out_path = os.path.join(os.path.dirname(__file__), '..', '.aether', 'results', 'bandmr_eth_dd_refine.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump({
            'sweep': 'PERF-064',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'params': {
                'donchian_periods': donchian_periods,
                'oversolds': oversolds,
                'stop_loss_pcts': stop_loss_pcts,
                'take_profit_pcts': take_profit_pcts,
                'cooldowns': cooldowns,
                'window_days': window_days,
                'leverage': 3,
            },
            'total_combos': total,
            'ran': n,
            'elapsed_sec': round(elapsed, 1),
            'passing': len(passing),
            'best': {
                'donchian_period': best.donchian_period,
                'oversold': best.oversold,
                'stop_loss_pct': best.stop_loss_pct,
                'take_profit_pct': best.take_profit_pct,
                'cooldown_bars': best.cooldown_bars,
                'net_pct': best.net_pct,
                'sharpe': best.sharpe,
                'max_dd': best.max_dd,
                'win_rate': best.win_rate,
                'pf': best.pf,
                'trades': best.trades,
            },
            'all_results': [{
                'dp': r.donchian_period, 'os': r.oversold,
                'sl': r.stop_loss_pct, 'tp': r.take_profit_pct, 'cd': r.cooldown_bars,
                'net': r.net_pct, 'sr': r.sharpe, 'dd': r.max_dd,
                'wr': r.win_rate, 'pf': r.pf, 't': r.trades,
                'verdict': verdict(r.net_pct, r.sharpe, r.max_dd, r.win_rate, r.trades),
            } for r in results],
        }, f, indent=2)

    print(f"\nResults saved to {out_path}")


if __name__ == '__main__':
    main()
