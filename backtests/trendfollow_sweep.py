#!/usr/bin/env python3
"""TrendFollow strategy parameter sweep — VECTORIZED version.
Usage: python3 backtests/trendfollow_sweep.py
Param space: ema_period=[20,50,100,200], sl_pct=[2,3,5], tp_pct=[3,5,8],
             cooldown_bars=[0,3,5,8]
Time windows: 90d, 180d, 365d. Leverage: 3x.
"""
import sys, os, json, itertools, time as _time
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
sys.path.insert(0, '/home/rinnen/binance_quant')

import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.signal_gen import trendfollow_signals
from backtest.sweep_utils import load_data, SweepVerdict, verdict


@dataclass
class SweepResult:
    symbol: str
    ema_period: int
    sl_pct: float
    tp_pct: float
    cooldown_bars: int
    window_days: int
    net_pct: float
    sharpe: float
    dsr: float
    max_dd: float
    win_rate: float
    pf: float
    trades: int
    avg_win: float
    avg_loss: float


if __name__ == '__main__':
    t0 = datetime.now(timezone.utc)
    t_start = _time.time()
    print(f"🔥 Prometheus — TrendFollow Parameter Sweep (vectorized)")
    print(f"═" * 70)
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")

    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    windows = [90, 180, 365]
    data_cache = {}
    timeframe = '1h'

    for symbol in ['BTC/USDT', 'ETH/USDT']:
        for days in windows:
            df = load_data(symbol=symbol, timeframe=timeframe, lookback_days=days, storage=storage)
            key = (symbol, days)
            if df is not None and len(df) > 0:
                data_cache[key] = df
                span = (df.index[-1] - df.index[0]).days
                print(f"  📊 {symbol:10s} {timeframe} {days:3d}d: {len(df):5d} bars, "
                      f"~{span}d [{df.index[0].strftime('%m/%d')}→{df.index[-1].strftime('%m/%d')}]")
    print()

    # Param grid
    ema_periods = [20, 50, 100, 200]
    sl_pcts = [0.02, 0.03, 0.05]
    tp_pcts = [0.03, 0.05, 0.08]
    cooldowns = [0, 3, 5, 8]

    param_combos = list(itertools.product(ema_periods, sl_pcts, tp_pcts, cooldowns))
    n_combos = len(param_combos)
    total_runs = n_combos * len(windows) * 2
    print(f"Param combos: {n_combos} × {len(windows)} windows × 2 symbols = {total_runs} total runs\n")

    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    all_results = []

    for sym_idx, symbol in enumerate(['BTC/USDT', 'ETH/USDT']):
        print(f"▶ Backtesting {symbol}...")
        for idx, (ep, sl, tp, cd) in enumerate(param_combos):
            for days in windows:
                df = data_cache.get((symbol, days))
                if df is None or len(df) < 100:
                    continue

                signals = trendfollow_signals(
                    df,
                    ema_period=ep,
                    sl_pct=sl,
                    tp_pct=tp,
                    cooldown_bars=cd,
                )

                result = engine.run(df, signals, leverage=3, n_trials=total_runs)
                m = result['metrics']

                all_results.append(SweepResult(
                    symbol=symbol,
                    ema_period=ep, sl_pct=sl, tp_pct=tp, cooldown_bars=cd,
                    window_days=days,
                    net_pct=m['total_return_pct'],
                    sharpe=m['sharpe_ratio'],
                    dsr=m['deflated_sharpe_ratio'],
                    max_dd=m['max_drawdown_pct'],
                    win_rate=m['win_rate'],
                    pf=m['profit_factor'],
                    trades=m['total_trades'],
                    avg_win=m.get('avg_win_pct', 0.0),
                    avg_loss=m.get('avg_loss_pct', 0.0),
                ))
        print(f"  ✓ {symbol} done ({len(param_combos) * len(windows)} runs)")

    elapsed = _time.time() - t_start
    print(f"\n⏱ Sweep complete: {elapsed:.1f}s\n")

    # ─── Report ───
    results_df = pd.DataFrame([asdict(r) for r in all_results])

    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym_results = results_df[results_df['symbol'] == symbol]
        print(f"═" * 70)
        print(f"📊 {symbol} — TOP 10 by Sharpe (365d)")
        print(f"═" * 70)
        top365 = sym_results[sym_results['window_days'] == 365].nlargest(10, 'sharpe')
        for _, r in top365.iterrows():
            sv = verdict(r['net_pct'], r['sharpe'], max_dd=r['max_dd'],
                         win_rate=r['win_rate'], trades=r['trades'], dsr=r['dsr'])
            print(f"  EP={r['ema_period']:3d} SL={r['sl_pct']:.0%} TP={r['tp_pct']:.0%} "
                  f"CD={r['cooldown_bars']:1d} | "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={r['trades']:3d} "
                  f"| {sv.verdict}")

        # Best by verdict for each window
        print(f"\n  Best by window:")
        for days in windows:
            w = sym_results[sym_results['window_days'] == days].nlargest(1, 'sharpe')
            if len(w) == 0:
                continue
            r = w.iloc[0]
            sv = verdict(r['net_pct'], r['sharpe'], max_dd=r['max_dd'],
                         win_rate=r['win_rate'], trades=r['trades'], dsr=r['dsr'])
            print(f"    {days:3d}d: EP={r['ema_period']:3d} SL={r['sl_pct']:.0%} TP={r['tp_pct']:.0%} "
                  f"CD={r['cooldown_bars']:1d} → "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={r['trades']:.0f} | {sv.verdict}")

    # ─── Final verdict ───
    print(f"\n{'═' * 70}")
    print("🏁 FINAL VERDICT")
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym = results_df[results_df['symbol'] == symbol]
        passing = sym[(sym['window_days'] == 365) & (sym['sharpe'] >= 0.5) & (sym['max_dd'] <= 20)]
        if len(passing) > 0:
            best = passing.nlargest(1, 'sharpe').iloc[0]
            print(f"  {symbol}: ✅ PASS (best: SR={best['sharpe']:.3f} DD={best['max_dd']:.1f}%)")
        else:
            close = sym[(sym['window_days'] == 365)].nlargest(1, 'sharpe').iloc[0]
            print(f"  {symbol}: ❌ DO_NOT_ENABLE (best: SR={close['sharpe']:.3f} DD={close['max_dd']:.1f}%)")
