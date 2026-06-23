#!/usr/bin/env python3
"""Donchian Trend Following strategy parameter sweep — VECTORIZED version.

Usage: python3 backtests/donchian_trend_sweep.py

Param space:
  donchian_period=[10,20,30,40]
  adx_threshold=[20,25,30]
  atr_sl_mult=[1.5,2.0,2.5]
  atr_tp_mult=[3.0,4.0,5.0]
  cooldown_bars=[3,5,8]

Fixed: adx_period=14, atr_period=14, leverage=3x
Time windows: 90d, 180d, 365d. Symbols: BTC/USDT, ETH/USDT. Timeframe: 1h.

Request: athena-donchian-trend-001
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
from backtest.signal_gen import donchian_trend_signals
from backtest.sweep_utils import load_data, SweepVerdict, verdict


@dataclass
class SweepResult:
    symbol: str
    donchian_period: int
    adx_threshold: float
    atr_sl_mult: float
    atr_tp_mult: float
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
    print(f"🔥 Prometheus — DonchianTrend Parameter Sweep (vectorized)")
    print(f"{'═' * 70}")
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Request: athena-donchian-trend-001 (Athena 06-22 17:55 UTC)")

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

    # Param grid (from Athena request)
    donchian_periods = [10, 20, 30, 40]
    adx_thresholds = [20, 25, 30]
    atr_sl_mults = [1.5, 2.0, 2.5]
    atr_tp_mults = [3.0, 4.0, 5.0]
    cooldowns = [3, 5, 8]

    param_combos = list(itertools.product(
        donchian_periods, adx_thresholds,
        atr_sl_mults, atr_tp_mults, cooldowns
    ))
    n_combos = len(param_combos)
    total_runs = n_combos * len(windows) * 2  # 2 symbols
    print(f"Param combos: {n_combos} × {len(windows)} windows × 2 symbols = {total_runs} total runs")
    print(f"Grid: DP(donchian_period)[{donchian_periods}] × AT(adx_threshold)[{adx_thresholds}] × "
          f"SL(ATR)[{atr_sl_mults}] × TP(ATR)[{atr_tp_mults}] × CD(cooldown)[{cooldowns}]")
    print()

    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    all_results = []

    for sym_idx, symbol in enumerate(['BTC/USDT', 'ETH/USDT']):
        print(f"▶ Backtesting {symbol}...")
        for idx, (dp, at, sl, tp, cd) in enumerate(param_combos):
            for days in windows:
                df = data_cache.get((symbol, days))
                if df is None or len(df) < 100:
                    continue

                signals = donchian_trend_signals(
                    df,
                    donchian_period=dp,
                    adx_threshold=at,
                    atr_sl_mult=sl,
                    atr_tp_mult=tp,
                    cooldown_bars=cd,
                )

                result = engine.run(df, signals, leverage=3, n_trials=total_runs)
                m = result['metrics']

                all_results.append(SweepResult(
                    symbol=symbol,
                    donchian_period=dp, adx_threshold=at,
                    atr_sl_mult=sl, atr_tp_mult=tp, cooldown_bars=cd,
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
        print(f"{'═' * 70}")
        print(f"📊 {symbol} — TOP 10 by Sharpe (365d)")
        print(f"{'═' * 70}")
        top365 = sym_results[sym_results['window_days'] == 365].nlargest(10, 'sharpe')
        for _, r in top365.iterrows():
            sv = verdict(r['net_pct'], r['sharpe'], max_dd=r['max_dd'],
                         win_rate=r['win_rate'], trades=r['trades'], dsr=r['dsr'])
            print(f"  DP={r['donchian_period']:2d} AT={r['adx_threshold']:2.0f} "
                  f"SL={r['atr_sl_mult']:.1f} TP={r['atr_tp_mult']:.1f} "
                  f"CD={r['cooldown_bars']:1d} | "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={r['trades']:3d} "
                  f"| {sv.verdict}")

        # Best by window
        print(f"\n  Best by window:")
        for days in windows:
            w = sym_results[sym_results['window_days'] == days].nlargest(1, 'sharpe')
            if len(w) == 0:
                continue
            r = w.iloc[0]
            sv = verdict(r['net_pct'], r['sharpe'], max_dd=r['max_dd'],
                         win_rate=r['win_rate'], trades=r['trades'], dsr=r['dsr'])
            print(f"    {days:3d}d: DP={r['donchian_period']:2d} AT={r['adx_threshold']:2.0f} "
                  f"SL={r['atr_sl_mult']:.1f} TP={r['atr_tp_mult']:.1f} "
                  f"CD={r['cooldown_bars']:1d} → "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={r['trades']:.0f} | {sv.verdict}")

    # ─── Final verdict ───
    print(f"\n{'═' * 70}")
    print("🏁 FINAL VERDICT")
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym = results_df[results_df['symbol'] == symbol]
        # Athena criteria: SR>=0.5, DD<=20%, WR>=40%, trades>=30
        passing = sym[(sym['window_days'] == 365) &
                      (sym['sharpe'] >= 0.5) &
                      (sym['max_dd'] <= 20) &
                      (sym['win_rate'] >= 40) &
                      (sym['trades'] >= 30)]
        if len(passing) > 0:
            best = passing.nlargest(1, 'sharpe').iloc[0]
            print(f"  {symbol}: ✅ PASS — LIVE candidate")
            print(f"    Best: DP={best['donchian_period']} AT={best['adx_threshold']:.0f} "
                  f"SL={best['atr_sl_mult']}x TP={best['atr_tp_mult']}x CD={best['cooldown_bars']} "
                  f"→ Net={best['net_pct']:+.2f}% SR={best['sharpe']:.3f} DD={best['max_dd']:.1f}% "
                  f"WR={best['win_rate']:.0f}% T={best['trades']}")
        else:
            close = sym[(sym['window_days'] == 365)].nlargest(1, 'sharpe').iloc[0]
            reasons = []
            if close['sharpe'] < 0.5: reasons.append(f"SR={close['sharpe']:.3f}<0.5")
            if close['max_dd'] > 20: reasons.append(f"DD={close['max_dd']:.1f}%>20%")
            if close['win_rate'] < 40: reasons.append(f"WR={close['win_rate']:.0f}%<40%")
            if close['trades'] < 30: reasons.append(f"T={close['trades']}<30")
            print(f"  {symbol}: ❌ DO_NOT_ENABLE — {', '.join(reasons)}")
            print(f"    Best: DP={close['donchian_period']} AT={close['adx_threshold']:.0f} "
                  f"SL={close['atr_sl_mult']}x TP={close['atr_tp_mult']}x CD={close['cooldown_bars']} "
                  f"→ Net={close['net_pct']:+.2f}% SR={close['sharpe']:.3f} DD={close['max_dd']:.1f}% "
                  f"WR={close['win_rate']:.0f}% T={close['trades']}")

    # ─── Write backtest_results.json ───
    print(f"\n{'═' * 70}")
    print("💾 Updating backtest_results.json...")
    bt_path = '/home/rinnen/binance_quant/.aether/state/backtest_results.json'
    with open(bt_path, 'r') as f:
        bt_data = json.load(f)

    for symbol, strategy_key in [('BTC/USDT', 'DonchianTrend_BTC'), ('ETH/USDT', 'DonchianTrend_ETH')]:
        sym = results_df[results_df['symbol'] == symbol]
        best365 = sym[sym['window_days'] == 365].nlargest(1, 'sharpe')
        if len(best365) == 0:
            continue
        r = best365.iloc[0]
        sv = verdict(r['net_pct'], r['sharpe'], max_dd=r['max_dd'],
                     win_rate=r['win_rate'], trades=r['trades'], dsr=r['dsr'])
        final_verdict = {True: 'DO_NOT_ENABLE', False: 'DO_NOT_ENABLE'}[True]
        if sv.verdict == 'LIVE' or sv.verdict == 'PAPER_READY':
            # Check Athena criteria specifically
            if r['sharpe'] >= 0.5 and r['max_dd'] <= 20 and r['win_rate'] >= 40 and r['trades'] >= 30:
                final_verdict = 'LIVE' if sv.verdict == 'LIVE' else 'PAPER'
            else:
                final_verdict = 'DO_NOT_ENABLE'
        else:
            final_verdict = 'DO_NOT_ENABLE'

        bt_data[strategy_key] = {
            "enabled": False,
            "verdict": final_verdict,
            "params": {
                "donchian_period": int(r['donchian_period']),
                "adx_threshold": float(r['adx_threshold']),
                "atr_sl_mult": float(r['atr_sl_mult']),
                "atr_tp_mult": float(r['atr_tp_mult']),
                "cooldown_bars": int(r['cooldown_bars']),
                "adx_period": 14,
                "atr_period": 14,
                "leverage": 3,
                "timeframe": "1h"
            },
            "365d": {
                "return_pct": round(r['net_pct'], 2),
                "sharpe": round(r['sharpe'], 3),
                "dsr": round(r['dsr'], 4),
                "max_dd": round(r['max_dd'], 1),
                "win_rate": round(r['win_rate'], 1),
                "trades": int(r['trades']),
                "profit_factor": round(r['pf'], 3)
            },
            "retired_reason": sv.reason if final_verdict == 'DO_NOT_ENABLE' else None,
            "sweep": {
                "total_runs": total_runs,
                "total_combos": n_combos,
                "windows": windows,
                "time": t0.strftime('%Y-%m-%dT%H:%M:%SZ')
            }
        }
        print(f"  {strategy_key}: verdict={final_verdict} | {r['net_pct']:+.2f}% SR={r['sharpe']:.3f} DD={r['max_dd']:.1f}%")

    bt_data['_updated_at'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(bt_path, 'w') as f:
        json.dump(bt_data, f, indent=2, ensure_ascii=False)

    # ─── Update athena.json ───
    print("💾 Updating athena.json...")
    athena_path = '/home/rinnen/binance_quant/.aether/state/athena.json'
    with open(athena_path, 'r') as f:
        athena_data = json.load(f)

    for symbol, strategy_key in [('BTC/USDT', 'DonchianTrend_BTC'), ('ETH/USDT', 'DonchianTrend_ETH')]:
        sym = results_df[results_df['symbol'] == symbol]
        best365 = sym[sym['window_days'] == 365].nlargest(1, 'sharpe')
        if len(best365) == 0:
            continue
        r = best365.iloc[0]
        sv = verdict(r['net_pct'], r['sharpe'], max_dd=r['max_dd'],
                     win_rate=r['win_rate'], trades=r['trades'], dsr=r['dsr'])
        final_verdict = 'DO_NOT_ENABLE'
        if r['sharpe'] >= 0.5 and r['max_dd'] <= 20 and r['win_rate'] >= 40 and r['trades'] >= 30:
            final_verdict = 'LIVE' if sv.verdict == 'LIVE' else 'PAPER'

        athena_data['strategies'][strategy_key] = {
            "signals": 0,
            "status": "ok" if final_verdict == 'LIVE' else "disabled",
            "return_pct": round(r['net_pct'], 2),
            "sharpe": round(r['sharpe'], 3),
            "win_rate": round(r['win_rate'], 1),
            "trades": int(r['trades']),
            "max_dd": round(r['max_dd'], 1),
            "verdict": final_verdict
        }
        print(f"  {strategy_key}: {final_verdict}")

    athena_data['_updated_at'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    athena_data['_donchian_trend_sweep'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(athena_path, 'w') as f:
        json.dump(athena_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Done. State files updated. Elapsed: {elapsed:.1f}s")
