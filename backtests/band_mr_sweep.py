#!/usr/bin/env python3
"""BandMR Mean Reversion strategy parameter sweep.

Athena Request #175: Donchian MR variant with relaxed RSI<30 (vs <20),
tighter SL=1%, smaller TP=2.5%, longer cooldown=8, volume filter.

Param space (324 combos):
  donchian_period=[10,15,20,25] × oversold=[25,30,35] × SL=[0.5,1,1.5]% × TP=[2,2.5,3]% × CD=[5,8,10]
  × 2 symbols (BTC/USDT, ETH/USDT) × 3 windows (90d, 180d, 365d) = 1,944 total runs.

Note: volume_filter=1.2 is applied via signal_gen.donchian_mr_signals (added 2026-06-22).
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
from backtest.signal_gen import donchian_mr_signals


@dataclass
class SweepResult:
    symbol: str
    donchian_period: int
    oversold: float
    overbought: float
    stop_loss_pct: float
    take_profit_pct: float
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


def verdict(net_pct, sharpe, dsr, max_dd, win_rate, trades):
    """PASS / CONDITIONAL / DO_NOT_ENABLE."""
    if trades < 5:
        return "INSUFFICIENT", "不足5笔交易"
    reasons = []
    if net_pct <= 0 and sharpe <= 0:
        return "DO_NOT_ENABLE", f"净收益{net_pct:+.2f}%+Sharpe{sharpe:+.2f}"
    passes = 0
    if sharpe >= 0.5: passes += 1
    else: reasons.append(f"Sharpe={sharpe:.3f}<0.5")
    if max_dd <= 20: passes += 1
    else: reasons.append(f"DD={max_dd:.1f}%>20%")
    if win_rate >= 40: passes += 1
    else: reasons.append(f"WR={win_rate:.0f}%<40%")
    if net_pct > 0: passes += 1
    else: reasons.append(f"Net={net_pct:+.2f}%")
    if dsr >= 0.80: passes += 1
    else: reasons.append(f"DSR={dsr:.4f}<0.80")
    if passes >= 4: return "PASS", "|".join(reasons) if reasons else ""
    elif passes >= 2: return "CONDITIONAL", "|".join(reasons)
    else: return "DO_NOT_ENABLE", "|".join(reasons)


if __name__ == '__main__':
    t0 = datetime.now(timezone.utc)
    t_start = _time.time()
    print(f"🔥 Prometheus — BandMR Parameter Sweep (vectorized)")
    print(f"{'═' * 70}")
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Request: #175 (Athena — BandMR DRAFT→BACKTEST)")

    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    windows = [90, 180, 365]
    data_cache = {}
    timeframe = '1h'

    for symbol in ['BTC/USDT', 'ETH/USDT']:
        for days in windows:
            df = storage.load_klines(symbol, timeframe)
            if df.empty:
                continue
            df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
            df.set_index('open_time', inplace=True)
            df.sort_index(inplace=True)
            cutoff = df.index[-1] - pd.Timedelta(days=days)
            df = df[df.index >= cutoff]
            key = (symbol, days)
            if len(df) > 0:
                data_cache[key] = df
                span = (df.index[-1] - df.index[0]).days
                print(f"  📊 {symbol:10s} {timeframe} {days:3d}d: {len(df):5d} bars, "
                      f"~{span}d [{df.index[0].strftime('%m/%d')}→{df.index[-1].strftime('%m/%d')}]")
    print()

    # Param grid per Athena request #175
    donchian_periods = [10, 15, 20, 25]
    oversolds = [25, 30, 35]
    stop_loss_pcts = [0.005, 0.01, 0.015]   # 0.5%, 1%, 1.5%
    take_profit_pcts = [0.02, 0.025, 0.03]  # 2%, 2.5%, 3%
    cooldowns = [5, 8, 10]
    overbought = 75.0  # fixed
    rsi_period = 14    # fixed
    exit_level = 50.0  # fixed

    param_combos = list(itertools.product(
        donchian_periods, oversolds,
        stop_loss_pcts, take_profit_pcts, cooldowns
    ))
    n_combos = len(param_combos)
    total_runs = n_combos * len(windows) * 2
    print(f"Param combos: {n_combos} × {len(windows)} windows × 2 symbols = {total_runs} total runs")
    print(f"Grid: DP[{donchian_periods}] × OS[{oversolds}] × "
          f"SL[{stop_loss_pcts}] × TP[{take_profit_pcts}] × CD[{cooldowns}]")
    print()

    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    all_results = []

    for sym_idx, symbol in enumerate(['BTC/USDT', 'ETH/USDT']):
        print(f"▶ Backtesting {symbol}...")
        for idx, (dp, os_val, sl, tp, cd) in enumerate(param_combos):
            for days in windows:
                df = data_cache.get((symbol, days))
                if df is None or len(df) < 100:
                    continue

                signals = donchian_mr_signals(
                    df,
                    donchian_period=dp,
                    rsi_period=rsi_period,
                    oversold=float(os_val),
                    overbought=overbought,
                    exit_level=exit_level,
                    stop_loss_pct=sl,
                    take_profit_pct=tp,
                    cooldown_bars=cd,
                    volume_filter=1.2,
                )

                result = engine.run(df, signals, leverage=3, n_trials=total_runs)
                m = result['metrics']

                all_results.append(SweepResult(
                    symbol=symbol,
                    donchian_period=dp, oversold=float(os_val),
                    overbought=overbought,
                    stop_loss_pct=sl, take_profit_pct=tp, cooldown_bars=cd,
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

    results_df = pd.DataFrame([asdict(r) for r in all_results])

    # ─── Per-symbol TOP 10 ───
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym_results = results_df[results_df['symbol'] == symbol]
        print(f"{'═' * 70}")
        print(f"📊 {symbol} — TOP 10 by Sharpe (365d)")
        print(f"{'═' * 70}")
        top365 = sym_results[sym_results['window_days'] == 365].nlargest(10, 'sharpe')
        for _, r in top365.iterrows():
            v, reason = verdict(r['net_pct'], r['sharpe'], r['dsr'],
                                r['max_dd'], r['win_rate'], r['trades'])
            print(f"  DP={r['donchian_period']:2d} OS={r['oversold']:.0f} "
                  f"SL={r['stop_loss_pct']:.3f} TP={r['take_profit_pct']:.3f} "
                  f"CD={r['cooldown_bars']:2d} | "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={r['trades']:3d} "
                  f"| {v}")

        print(f"\n  Best by window:")
        for days in windows:
            w = sym_results[sym_results['window_days'] == days].nlargest(1, 'sharpe')
            if len(w) == 0:
                continue
            r = w.iloc[0]
            v, reason = verdict(r['net_pct'], r['sharpe'], r['dsr'],
                                r['max_dd'], r['win_rate'], r['trades'])
            print(f"    {days:3d}d: DP={r['donchian_period']:2d} OS={r['oversold']:.0f} "
                  f"SL={r['stop_loss_pct']:.3f} TP={r['take_profit_pct']:.3f} "
                  f"CD={r['cooldown_bars']:2d} → "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={r['trades']:3.0f} | {v}")

    # ─── FINAL VERDICT ───
    print(f"\n{'═' * 70}")
    print("🏁 FINAL VERDICT")
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym = results_df[results_df['symbol'] == symbol]
        passing = sym[(sym['window_days'] == 365) &
                      (sym['sharpe'] >= 0.5) &
                      (sym['max_dd'] <= 20) &
                      (sym['win_rate'] >= 40) &
                      (sym['trades'] >= 30)]
        if len(passing) > 0:
            best = passing.nlargest(1, 'sharpe').iloc[0]
            print(f"  {symbol}: ✅ PASS — LIVE candidate")
            print(f"    Best: DP={int(best['donchian_period'])} OS={best['oversold']:.0f} "
                  f"SL={best['stop_loss_pct']:.3f} TP={best['take_profit_pct']:.3f} CD={int(best['cooldown_bars'])} "
                  f"→ Net={best['net_pct']:+.2f}% SR={best['sharpe']:.3f} DD={best['max_dd']:.1f}% "
                  f"WR={best['win_rate']:.0f}% T={int(best['trades'])}")
        else:
            close = sym[(sym['window_days'] == 365)].nlargest(1, 'sharpe').iloc[0]
            reasons = []
            if close['sharpe'] < 0.5: reasons.append(f"SR={close['sharpe']:.3f}<0.5")
            if close['max_dd'] > 20: reasons.append(f"DD={close['max_dd']:.1f}%>20%")
            if close['win_rate'] < 40: reasons.append(f"WR={close['win_rate']:.0f}%<40%")
            if close['trades'] < 30: reasons.append(f"T={int(close['trades'])}<30")
            print(f"  {symbol}: ❌ DO_NOT_ENABLE — {', '.join(reasons)}")
            print(f"    Best: DP={int(close['donchian_period'])} OS={close['oversold']:.0f} "
                  f"SL={close['stop_loss_pct']:.3f} TP={close['take_profit_pct']:.3f} CD={int(close['cooldown_bars'])} "
                  f"→ Net={close['net_pct']:+.2f}% SR={close['sharpe']:.3f} DD={close['max_dd']:.1f}% "
                  f"WR={close['win_rate']:.0f}% T={int(close['trades'])}")

    # ─── Write backtest_results.json ───
    print(f"\n{'═' * 70}")
    print("💾 Updating backtest_results.json...")
    bt_path = '/home/rinnen/binance_quant/.aether/state/backtest_results.json'
    with open(bt_path, 'r') as f:
        bt_data = json.load(f)

    for symbol, strategy_key in [('BTC/USDT', 'BandMR_BTC'), ('ETH/USDT', 'BandMR_ETH')]:
        sym = results_df[results_df['symbol'] == symbol]
        best365 = sym[sym['window_days'] == 365].nlargest(1, 'sharpe')
        if len(best365) == 0:
            continue
        r = best365.iloc[0]
        v, reason = verdict(r['net_pct'], r['sharpe'], r['dsr'],
                            r['max_dd'], r['win_rate'], r['trades'])
        final_verdict = 'DO_NOT_ENABLE'
        if r['sharpe'] >= 0.5 and r['max_dd'] <= 20 and r['win_rate'] >= 40 and r['trades'] >= 30:
            final_verdict = 'LIVE' if v == 'PASS' else 'PAPER'
        elif v in ('PASS', 'CONDITIONAL') and r['trades'] >= 10 and r['net_pct'] > 0:
            final_verdict = 'PAPER'

        bt_data[strategy_key] = {
            "enabled": False,
            "verdict": final_verdict,
            "params": {
                "donchian_period": int(r['donchian_period']),
                "rsi_period": 14,
                "oversold": float(r['oversold']),
                "overbought": float(r['overbought']),
                "exit_level": 50.0,
                "stop_loss_pct": float(r['stop_loss_pct']),
                "take_profit_pct": float(r['take_profit_pct']),
                "cooldown_bars": int(r['cooldown_bars']),
                "volume_filter": 1.2,
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
            "retired_reason": reason if final_verdict == 'DO_NOT_ENABLE' else None,
            "sweep": {
                "total_runs": total_runs,
                "total_combos": n_combos,
                "windows": windows,
                "time": t0.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "request_id": 175
            }
        }
        print(f"  {strategy_key}: verdict={final_verdict} | "
              f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.3f} DD={r['max_dd']:.1f}% T={int(r['trades'])}")

        # ─── Also sync to strategies section (AUDIT-094 root cause fix) ───
        if 'strategies' not in bt_data:
            bt_data['strategies'] = {}
        bt_data['strategies'][strategy_key] = {
            "enabled": final_verdict in ('LIVE', 'PAPER'),
            "verdict": final_verdict,
            "metrics": {
                "sharpe_ratio": round(r['sharpe'], 3),
                "dd_pct": round(r['max_dd'], 1),
                "return_pct": round(r['net_pct'], 2),
                "win_rate": round(r['win_rate'], 1),
                "trades": int(r['trades']),
                "profit_factor": round(r['pf'], 3)
            },
            "retired_reason": reason if final_verdict == 'DO_NOT_ENABLE' else None,
            "params": {
                "donchian_period": int(r['donchian_period']),
                "oversold": float(r['oversold']),
                "stop_loss_pct": float(r['stop_loss_pct']),
                "take_profit_pct": float(r['take_profit_pct']),
                "cooldown_bars": int(r['cooldown_bars'])
            }
        }

    bt_data['_updated_at'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(bt_path, 'w') as f:
        json.dump(bt_data, f, indent=2, ensure_ascii=False)

    # ─── Update athena.json ───
    print("💾 Updating athena.json...")
    athena_path = '/home/rinnen/binance_quant/.aether/state/athena.json'
    with open(athena_path, 'r') as f:
        athena_data = json.load(f)

    for symbol, strategy_key in [('BTC/USDT', 'BandMR_BTC'), ('ETH/USDT', 'BandMR_ETH')]:
        sym = results_df[results_df['symbol'] == symbol]
        best365 = sym[sym['window_days'] == 365].nlargest(1, 'sharpe')
        if len(best365) == 0:
            continue
        r = best365.iloc[0]
        # Re-compute verdict for THIS row (don't leak from backtest_results loop)
        v2, _reason2 = verdict(r['net_pct'], r['sharpe'], r['dsr'],
                               r['max_dd'], r['win_rate'], r['trades'])
        final_verdict = 'DO_NOT_ENABLE'
        if r['sharpe'] >= 0.5 and r['max_dd'] <= 20 and r['win_rate'] >= 40 and r['trades'] >= 30:
            final_verdict = 'LIVE' if v2 == 'PASS' else 'PAPER'
        elif v2 in ('PASS', 'CONDITIONAL') and r['trades'] >= 10 and r['net_pct'] > 0:
            final_verdict = 'PAPER'

        athena_data['strategies'][strategy_key] = {
            "signals": 0,
            "status": "ok" if final_verdict in ('LIVE', 'PAPER') else "disabled",
            "return_pct": round(r['net_pct'], 2),
            "sharpe": round(r['sharpe'], 3),
            "win_rate": round(r['win_rate'], 1),
            "trades": int(r['trades']),
            "max_dd": round(r['max_dd'], 1),
            "verdict": final_verdict,
            "best_params": {
                "donchian_period": int(r['donchian_period']),
                "oversold": float(r['oversold']),
                "stop_loss_pct": float(r['stop_loss_pct']),
                "take_profit_pct": float(r['take_profit_pct']),
                "cooldown_bars": int(r['cooldown_bars']),
            }
        }
        print(f"  {strategy_key}: {final_verdict}")

    athena_data['_updated_at'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    athena_data['_band_mr_sweep'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(athena_path, 'w') as f:
        json.dump(athena_data, f, indent=2, ensure_ascii=False)

    # ─── Update prometheus.json ───
    print("💾 Updating prometheus.json...")
    prom_path = '/home/rinnen/binance_quant/.aether/state/prometheus.json'
    with open(prom_path, 'r') as f:
        prom_data = json.load(f)

    prom_data['active_research']['PERF-058'] = {
        "title": "BandMR backtest sweep (request #175)",
        "status": "completed",
        "target": "backtests/band_mr_sweep.py → strategies BandMR_BTC/BandMR_ETH",
        "description": f"324-combo Donchian MR sweep (RSI<25-35 vs <20). BTC best SR={results_df[(results_df['symbol']=='BTC/USDT')&(results_df['window_days']==365)]['sharpe'].max():.3f}, ETH best SR={results_df[(results_df['symbol']=='ETH/USDT')&(results_df['window_days']==365)]['sharpe'].max():.3f}. Volume filter not applied in backtest (signal_gen limitation).",
        "deployed": t0.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "request_id": 175,
        "total_runs": total_runs,
        "elapsed_s": round(elapsed, 1)
    }
    prom_data['last_run'] = t0.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    prom_data['_updated_at'] = t0.isoformat()
    with open(prom_path, 'w') as f:
        json.dump(prom_data, f, indent=2, ensure_ascii=False)

    # ─── Fulfill request #175 ───
    print("✅ Fulfilling request #175...")

    print(f"\n✅ Done. State files updated. Elapsed: {elapsed:.1f}s")
