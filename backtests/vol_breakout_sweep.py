#!/usr/bin/env python3
"""VolBreakout strategy parameter sweep — VECTORIZED version.
Usage: python3 backtests/vol_breakout_sweep.py
Request #149: Backtest VolBreakout_BTC + VolBreakout_ETH.
Param space: atr_period=[14,20], atr_mult=[1.5,2.0,2.5], ema_period=[20,50],
             atr_sl_mult=[1.0,1.5], atr_tp_mult=[2.0,3.0]
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
from backtest.signal_gen import vol_breakout_signals
from backtest.sweep_utils import load_data, SweepVerdict, verdict


@dataclass
class SweepResult:
    symbol: str
    atr_period: int
    atr_mult: float
    ema_period: int
    atr_sl_mult: float
    atr_tp_mult: float
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
    print(f"🔥 Prometheus — VolBreakout Parameter Sweep (vectorized)")
    print(f"═" * 70)
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
    print()

    # ── Load data ──
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

    # ── Param grid ──
    atr_periods = [14, 20]
    atr_mults = [1.5, 2.0, 2.5]
    ema_periods = [20, 50]
    atr_sl_mults = [1.0, 1.5]
    atr_tp_mults = [2.0, 3.0]

    param_combos = list(itertools.product(
        atr_periods, atr_mults, ema_periods, atr_sl_mults, atr_tp_mults
    ))
    n_combos = len(param_combos)
    total_runs = n_combos * len(windows) * 2  # 2 symbols
    print(f"Param combos: {n_combos} × {len(windows)} windows × 2 symbols = {total_runs} total runs")
    print()

    # ── Engine ──
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    # ── Run sweep ──
    all_results = []

    for sym_idx, symbol in enumerate(['BTC/USDT', 'ETH/USDT']):
        print(f"▶ Backtesting {symbol}...")
        for idx, (ap, am, ep, slm, tpm) in enumerate(param_combos):
            for days in windows:
                df = data_cache.get((symbol, days))
                if df is None or len(df) < 100:
                    continue

                signals = vol_breakout_signals(
                    df,
                    atr_period=ap, atr_mult=am, ema_period=ep,
                    atr_sl_mult=slm, atr_tp_mult=tpm,
                    cooldown_bars=5, volume_filter=True, vol_ma_period=20,
                )

                result = engine.run(df, signals, leverage=3, n_trials=total_runs)
                m = result['metrics']

                all_results.append(SweepResult(
                    symbol=symbol,
                    atr_period=ap, atr_mult=am, ema_period=ep,
                    atr_sl_mult=slm, atr_tp_mult=tpm,
                    window_days=days,
                    net_pct=m['total_return_pct'],
                    sharpe=m['sharpe_ratio'],
                    dsr=m['deflated_sharpe_ratio'],
                    max_dd=m['max_drawdown_pct'],
                    win_rate=m['win_rate'],
                    pf=m['profit_factor'],
                    trades=m['total_trades'],
                    avg_win=m['avg_win_pct'],
                    avg_loss=m['avg_loss_pct'],
                ))

            if (idx + 1) % 12 == 0:
                elapsed = _time.time() - t_start
                print(f"  ... {idx+1}/{n_combos} combos ({elapsed:.1f}s)")

        print(f"  ✓ {symbol}: done")

    print()
    print("═" * 70)
    print("RESULTS SUMMARY")
    print("═" * 70)

    # ── Best per symbol/window ──
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        for days in windows:
            subset = [r for r in all_results if r.symbol == symbol and r.window_days == days]
            if not subset:
                continue
            subset.sort(key=lambda x: x.sharpe, reverse=True)
            top5 = subset[:5]
            print(f"\n── {symbol} {days}d (top 5 by Sharpe) ──")
            print(f"  {'AP':>3s} {'AM':>4s} {'EP':>3s} {'SL':>4s} {'TP':>4s} "
                  f"{'Net%':>7s} {'SR':>7s} {'DSR':>6s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
            for r in top5:
                print(f"  {r.atr_period:3d} {r.atr_mult:3.1f}x {r.ema_period:3d} "
                      f"{r.atr_sl_mult:3.1f}x {r.atr_tp_mult:3.1f}x "
                      f"{r.net_pct:+7.2f}% {r.sharpe:+7.3f} {r.dsr:6.4f} "
                      f"{r.max_dd:5.1f}% {r.win_rate:4.0f}% {r.pf:5.2f} {r.trades:4d}")

    # ── Cross-window consistency ──
    print()
    print("═" * 70)
    print("CROSS-WINDOW CONSISTENCY")
    print("═" * 70)

    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym_results = [r for r in all_results if r.symbol == symbol]
        if not sym_results:
            continue

        # Find params that appear in top 20% of multiple windows
        best_params = {}
        for days in windows:
            wr = [r for r in sym_results if r.window_days == days]
            if not wr:
                continue
            wr.sort(key=lambda x: x.sharpe, reverse=True)
            top20 = wr[:max(1, len(wr) // 5)]
            for r in top20:
                key = (r.atr_period, r.atr_mult, r.ema_period, r.atr_sl_mult, r.atr_tp_mult)
                if key not in best_params:
                    best_params[key] = []
                best_params[key].append(r)

        consistent = {k: v for k, v in best_params.items() if len(v) >= 2}
        if consistent:
            print(f"\n  {symbol} — consistent across 2+ windows:")
            for params, results in sorted(consistent.items(), key=lambda x: -len(x[1])):
                avg_sharpe = np.mean([r.sharpe for r in results])
                avg_net = np.mean([r.net_pct for r in results])
                windows_str = ','.join([str(r.window_days) for r in results])
                print(f"    AP={params[0]} AM={params[1]:.1f}x EP={params[2]} "
                      f"SL={params[3]:.1f}x TP={params[4]:.1f}x "
                      f"→ avg SR={avg_sharpe:+.3f} Net={avg_net:+.2f}% ({len(results)}/{len(windows)})")
        else:
            print(f"\n  {symbol}: No params consistent across 2+ windows → likely overfit")

    # ── FINAL VERDICT ──
    print()
    print("═" * 70)
    print("FINAL VERDICT")
    print("═" * 70)

    verdicts = {}

    for symbol in ['BTC/USDT', 'ETH/USDT']:
        r365 = [r for r in all_results if r.symbol == symbol and r.window_days == 365]
        r180 = [r for r in all_results if r.symbol == symbol and r.window_days == 180]
        r90  = [r for r in all_results if r.symbol == symbol and r.window_days == 90]

        if not r365:
            print(f"\n  {symbol}: No 365d data — INSUFFICIENT")
            verdicts[symbol] = ("INSUFFICIENT", "无365天数据")
            continue

        r365.sort(key=lambda x: x.sharpe, reverse=True)
        best = r365[0]

        sv = verdict(best.net_pct, best.sharpe, max_dd=best.max_dd,
                     win_rate=best.win_rate, trades=best.trades, dsr=best.dsr)
        verdicts[symbol] = (sv.verdict, sv.reason)

        # Cross-window check for best 365d params
        key = (best.atr_period, best.atr_mult, best.ema_period,
               best.atr_sl_mult, best.atr_tp_mult)
        r180_match = [r for r in r180 if (r.atr_period, r.atr_mult, r.ema_period,
                        r.atr_sl_mult, r.atr_tp_mult) == key]
        r90_match  = [r for r in r90 if (r.atr_period, r.atr_mult, r.ema_period,
                       r.atr_sl_mult, r.atr_tp_mult) == key]

        print(f"\n  📐 {symbol}")
        print(f"     Best 365d: AP={best.atr_period} AM={best.atr_mult:.1f}x "
              f"EP={best.ema_period} SL={best.atr_sl_mult:.1f}x TP={best.atr_tp_mult:.1f}x")
        print(f"     365d: Net={best.net_pct:+.2f}% SR={best.sharpe:+.3f} "
              f"DSR={best.dsr:.4f} DD={best.max_dd:.1f}% WR={best.win_rate:.0f}% "
              f"#T={best.trades} PF={best.pf:.2f}")

        if r180_match:
            r = r180_match[0]
            print(f"     180d: Net={r.net_pct:+.2f}% SR={r.sharpe:+.3f} "
                  f"DD={r.max_dd:.1f}% WR={r.win_rate:.0f}% #T={r.trades}")
        else:
            print(f"     180d: (different best params)")

        if r90_match:
            r = r90_match[0]
            print(f"      90d: Net={r.net_pct:+.2f}% SR={r.sharpe:+.3f} "
                  f"DD={r.max_dd:.1f}% WR={r.win_rate:.0f}% #T={r.trades}")
        else:
            print(f"      90d: (different best params)")

        print(f"     Verdict: {sv.verdict} ({sv.reason})")

        if r180_match and r90_match:
            sr_180 = r180_match[0].sharpe
            sr_90 = r90_match[0].sharpe
            if best.sharpe > 0 and sr_90 < best.sharpe * 0.3:
                print(f"     ⚠️  SHARPE DEGRADATION: 365d={best.sharpe:+.3f}→90d={sr_90:+.3f}")

    # ── Save results + update athena.json ──
    os.makedirs('.aether', exist_ok=True)
    output_path = '.aether/vol_breakout_sweep.json'
    output_data = {
        'run_time': t0.isoformat(),
        'strategy': 'VolBreakout',
        'param_space': {
            'atr_period': atr_periods, 'atr_mult': atr_mults,
            'ema_period': ema_periods, 'atr_sl_mult': atr_sl_mults,
            'atr_tp_mult': atr_tp_mults,
        },
        'windows': windows,
        'results': [asdict(r) for r in all_results],
        'verdicts': verdicts,
    }

    with open(output_path, 'w') as f:
        json.dump(output_data, f, indent=2, default=str)
    print(f"\n💾 Results saved to {output_path} ({len(all_results)} runs)")

    # ── Update athena.json with VolBreakout verdicts ──
    athena_path = '.aether/state/athena.json'
    if os.path.exists(athena_path):
        with open(athena_path) as f:
            athena_state = json.load(f)

        for symbol, (v, reason) in verdicts.items():
            strategy_name = f"VolBreakout_{symbol.split('/')[0]}"
            if 'strategies' in athena_state and strategy_name in athena_state['strategies']:
                athena_state['strategies'][strategy_name]['verdict'] = v
                athena_state['strategies'][strategy_name]['status'] = 'disabled'

        athena_state['_updated_at'] = t0.isoformat()
        with open(athena_path, 'w') as f:
            json.dump(athena_state, f, indent=2, default=str)
        print(f"📋 athena.json updated with VolBreakout verdicts")

    elapsed = _time.time() - t_start
    print(f"\n⏱️  Total: {elapsed:.1f}s ({len(all_results)} runs @ {elapsed/max(len(all_results),1)*1000:.0f}ms/run)")
