#!/usr/bin/env python3
"""
Athena — Strategy Brain backtest engine.
Loads strategies.yaml → pulls data from DB → backtests → scores → writes athena.json
Usage: python3 athena_backtest.py [--days N]  (default: 30 days)
"""
import argparse, sys, os, json, yaml, warnings
from datetime import datetime, timezone
from collections import defaultdict

# Suppress sklearn feature-name warnings (LightGBM 4.6.0 bug: predict_disable_shape_check ignored)
warnings.filterwarnings('ignore', message='X does not have valid feature names')
warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

sys.path.insert(0, '/home/rinnen/binance_quant')

import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.signal_gen import (
    trendfollow_signals, rsi_mr_signals,
    dynamic_grid_signals, ma_cross_signals,
    bband_rsi_signals, adx_trend_signals,
    momentum_signals, trend_pullback_signals,
    vol_breakout_signals, supertrend_signals,
    macd_crossover_signals, donchian_mr_signals,
    dispatch_signals,
)

from backtest.sweep_utils import load_data

# ══════════════════════════════════════════════════════════════════════
# Load data
# ══════════════════════════════════════════════════════════════════════


if __name__ == '__main__':
    # ── CLI args ──
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=30, help='Lookback days (default: 30)')
    args = parser.parse_args()
    lookback_days = args.days

    # ── Load config ──
    cfg = get_config()
    storage = MarketStorage(cfg.db_path)

    with open('config/strategies.yaml') as f:
        strat_cfg = yaml.safe_load(f)

    strategies_list = strat_cfg['strategies']

    print("🦉 Athena — Strategy Brain Backtest")
    print("=" * 70)
    t0 = datetime.now(timezone.utc)
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')} | Lookback: {lookback_days}d")
    print()

    # Load all needed data
    data = {}
    for sym in ['BTC/USDT', 'ETH/USDT']:
        for tf in ['15m', '1h']:
            df = load_data(sym, tf, lookback_days=lookback_days, storage=storage)
            if df is not None and len(df) > 0:
                data[(sym, tf)] = df
                days_span = (df.index[-1] - df.index[0]).days + (df.index[-1] - df.index[0]).seconds / 86400
                print(f"  📊 {sym:10s} {tf:4s}: {len(df):5d} bars, {days_span:.1f}d "
                      f"[{df.index[0].strftime('%m/%d %H:%M')} → {df.index[-1].strftime('%m/%d %H:%M')}]")


    # ══════════════════════════════════════════════════════════════════════
    # Backtest each strategy
    # ══════════════════════════════════════════════════════════════════════

    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    results_summary = []

    print()
    print("=" * 80)
    print(f"STRATEGY BACKTEST RESULTS (last {lookback_days} days)")
    print("=" * 80)

    for s in strategies_list:
        name = s['name']
        enabled = s['enabled']
        p = s['params']
        sym = p['symbols'][0]
        tf = p['timeframes'][0]
        key = (sym, tf)
        df = data.get(key)

        if df is None or len(df) < 50:
            print(f"\n  ⚠️ {name}: insufficient data ({len(df) if df is not None else 0} bars) — skipping")
            results_summary.append({
                'name': name, 'enabled': enabled, 'symbol': sym, 'tf': tf,
                'status': 'SKIPPED', 'reason': 'insufficient data'
            })
            continue

        # Generate signals
        strategy_type = s['class'].split('.')[-1]

        # Generate signals via shared dispatch (PERF-021)
        try:
            signals = dispatch_signals(df, strategy_type, p)
        except KeyError:
            print(f"\n  ⚠️ {name}: unknown strategy type {strategy_type} — skipping")
            results_summary.append({
                'name': name, 'enabled': enabled, 'symbol': sym, 'tf': tf,
                'status': 'SKIPPED', 'reason': f'unknown type: {strategy_type}'
            })
            continue

        # Leverage from strategy config (default 1 if not specified)
        leverage = p.get('leverage', 1)
        result = engine.run(df, signals, leverage=leverage)
        m = result['metrics']

        status_icon = "✅" if enabled else "⏸️"
        print(f"\n  {status_icon} {name} ({sym} {tf})")
        print(f"     Return: {m['total_return_pct']:+.2f}% | Sharpe: {m['sharpe_ratio']:+.3f} | "
              f"MaxDD: {m['max_drawdown_pct']:.2f}%")
        print(f"     Trades: {m['total_trades']} | WinRate: {m['win_rate']:.1f}% | "
              f"PF: {m['profit_factor']:.3f}")
        print(f"     AvgWin: {m['avg_win_pct']:+.2f}% | AvgLoss: {m['avg_loss_pct']:+.2f}% | "
              f"Best: {m['best_trade_pct']:+.2f}% | Worst: {m['worst_trade_pct']:+.2f}%")
        print(f"     Final Equity: ${m['final_equity']:,.2f}")

        # Flag issues
        flags = []
        if m['win_rate'] < 30 and m['total_trades'] >= 3:
            flags.append(f"⚠️ LOW WINRATE ({m['win_rate']:.0f}% < 30%)")
        if m['sharpe_ratio'] < 0 and m['total_trades'] >= 3:
            flags.append(f"⚠️ NEGATIVE SHARPE ({m['sharpe_ratio']:+.3f})")
        if m['total_return_pct'] < -5:
            flags.append(f"⚠️ LARGE LOSS ({m['total_return_pct']:+.2f}%)")
        if m['max_drawdown_pct'] > 10:
            flags.append(f"⚠️ HIGH DRAWDOWN ({m['max_drawdown_pct']:.1f}%)")

        if flags:
            for f in flags:
                print(f"     {f}")

        # Trade log preview
        if not result['trade_log'].empty:
            recent = result['trade_log'].tail(5)
            print(f"     Recent trades:")
            for _, t in recent.iterrows():
                print(f"       {t['direction']:5s} | {t['entry_price']:>10,.2f} → {t['exit_price']:>10,.2f} "
                      f"| PnL: {t['pnl_pct']:+.2f}%")

        results_summary.append({
            'name': name,
            'enabled': enabled,
            'symbol': sym,
            'tf': tf,
            'class': s['class'],
            'params': p,
            'status': 'OK',
            'metrics': m,
            'flags': flags,
            'bars': len(df),
            'data_start': str(df.index[0]),
            'data_end': str(df.index[-1]),
        })


    # ══════════════════════════════════════════════════════════════════════
    # Additional: test MA_Cross with different configs on BTC/ETH 1h
    # ══════════════════════════════════════════════════════════════════════

    print()
    print("=" * 80)
    print("MA_CROSS PARAMETER SWEEP (disabled strategy — evaluation)")
    print("=" * 80)

    ma_cross_sweep = []
    for sym in ['BTC/USDT', 'ETH/USDT']:
        key = (sym, '1h')
        df = data.get(key)
        if df is None or len(df) < 50:
            continue

        for fp, sp in [(7, 25), (5, 20), (10, 30), (12, 26), (5, 13)]:
            for slm, tpm in [(2.0, 3.0), (1.5, 3.0), (2.0, 4.0), (2.5, 4.0)]:
                signals = ma_cross_signals(df, fp, sp, 14, slm, tpm, 5)
                result = engine.run(df, signals, leverage=5)
                m = result['metrics']
                ma_cross_sweep.append({
                    'symbol': sym, 'fast': fp, 'slow': sp,
                    'atr_sl': slm, 'atr_tp': tpm,
                    'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                    'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                    'pf': m['profit_factor'], 'trades': m['total_trades'],
                })

    # Sort by net return
    ma_cross_sweep.sort(key=lambda x: x['net'], reverse=True)
    if ma_cross_sweep:
        print(f"\n  Top 10 MA_Cross configs (by net%):")
        print(f"  {'Sym':8s} {'Fast':>4s} {'Slow':>4s} {'SLm':>5s} {'TPm':>5s} "
              f"{'Net%':>7s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
        for r in ma_cross_sweep[:10]:
            print(f"  {r['symbol']:8s} {r['fast']:4d} {r['slow']:4d} "
                  f"{r['atr_sl']:4.1f}x {r['atr_tp']:4.1f}x "
                  f"{r['net']:+7.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d}")

        best_btc = max([r for r in ma_cross_sweep if r['symbol'] == 'BTC/USDT'], key=lambda x: x['net'])
        best_eth = max([r for r in ma_cross_sweep if r['symbol'] == 'ETH/USDT'], key=lambda x: x['net'])
        print(f"\n  ▶ Best BTC: fast={best_btc['fast']} slow={best_btc['slow']} "
              f"slm={best_btc['atr_sl']}x tpm={best_btc['atr_tp']}x → "
              f"net={best_btc['net']:+.2f}% sharpe={best_btc['sharpe']:+.2f} "
              f"dd={best_btc['dd']:.1f}% wr={best_btc['wr']:.0f}%")
        print(f"  ▶ Best ETH: fast={best_eth['fast']} slow={best_eth['slow']} "
              f"slm={best_eth['atr_sl']}x tpm={best_eth['atr_tp']}x → "
              f"net={best_eth['net']:+.2f}% sharpe={best_eth['sharpe']:+.2f} "
              f"dd={best_eth['dd']:.1f}% wr={best_eth['wr']:.0f}%")


    # ══════════════════════════════════════════════════════════════════════
    # Also test new strategy ideas
    # ══════════════════════════════════════════════════════════════════════

    print()
    print("=" * 80)
    print("NEW STRATEGY IDEAS — EXPLORATION")
    print("=" * 80)

    # Idea 1: EMA20 on BTC 1h (from Prometheus finding: EMA20 beats EMA100)
    if ('BTC/USDT', '1h') in data:
        df = data[('BTC/USDT', '1h')]
        # Test EMA20, SL=1%, TP=3%
        for ema, sl, tp, cd in [(20, 0.01, 0.03, 5), (20, 0.015, 0.04, 8), (50, 0.01, 0.03, 5), (100, 0.015, 0.04, 8)]:
            sig = trendfollow_signals(df, ema, sl, tp, cd)
            res = engine.run(df, sig, leverage=3)
            m = res['metrics']
            print(f"  TF EMA{ema} SL={sl*100:.1f}% TP={tp*100:.1f}% CD={cd} "
                  f"(BTC 1h): net={m['total_return_pct']:+.2f}% "
                  f"sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% "
                  f"wr={m['win_rate']:.0f}% #T={m['total_trades']}")

    # Idea 2: RSI_MR on ETH 1h
    if ('ETH/USDT', '1h') in data:
        df = data[('ETH/USDT', '1h')]
        for rsi_p, os_level, ob_level, sl, tp in [(14, 30, 70, 0.03, 0.06), (14, 25, 75, 0.02, 0.05), (7, 35, 65, 0.03, 0.06)]:
            sig = rsi_mr_signals(df, rsi_p, os_level, ob_level, 50, sl, tp, 5)
            res = engine.run(df, sig, leverage=3)
            m = res['metrics']
            print(f"  RSI_MR(rsi={rsi_p} os={os_level} ob={ob_level}) ETH 1h: net={m['total_return_pct']:+.2f}% "
                  f"sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% "
                  f"wr={m['win_rate']:.0f}% #T={m['total_trades']}")


    # ══════════════════════════════════════════════════════════════════════
    # Recommendations
    # ══════════════════════════════════════════════════════════════════════

    print()
    print("=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)

    recommendations = []

    for r in results_summary:
        if r['status'] != 'OK':
            continue
        m = r['metrics']
        name = r['name']

        # Check pause conditions
        should_pause = False
        pause_reasons = []
        if m['total_trades'] >= 3 and m['win_rate'] < 30:
            should_pause = True
            pause_reasons.append(f"win rate {m['win_rate']:.0f}% < 30%")
        if m['total_trades'] >= 3 and m['sharpe_ratio'] < 0:
            should_pause = True
            pause_reasons.append(f"Sharpe {m['sharpe_ratio']:+.3f} < 0")
        if m['total_trades'] >= 3 and m['total_return_pct'] < -5:
            should_pause = True
            pause_reasons.append(f"return {m['total_return_pct']:+.2f}% < -5%")

        if should_pause and r['enabled']:
            rec = f"⛔ PAUSE {name}: {', '.join(pause_reasons)}"
            print(f"  {rec}")
            recommendations.append(rec)
        elif should_pause:
            print(f"  ✓ {name}: already disabled ({', '.join(pause_reasons)})")
        elif r['enabled']:
            rec = f"✓ KEEP {name}: Sharpe={m['sharpe_ratio']:+.2f} WR={m['win_rate']:.0f}% Net={m['total_return_pct']:+.2f}%"
            print(f"  {rec}")
            recommendations.append(rec)

    # MA_Cross re-enable?
    if ma_cross_sweep:
        best_btc = max([r for r in ma_cross_sweep if r['symbol'] == 'BTC/USDT'], key=lambda x: x['net'])
        if best_btc['net'] > 1 and best_btc['sharpe'] > 0.5:
            rec = (f"💡 CONSIDER MA_Cross on BTC 1h: fast={best_btc['fast']} slow={best_btc['slow']} "
                   f"slm={best_btc['atr_sl']}x tpm={best_btc['atr_tp']}x → net={best_btc['net']:+.2f}% "
                   f"sharpe={best_btc['sharpe']:+.2f}")
            print(f"  {rec}")
            recommendations.append(rec)
        else:
            print(f"  ✗ MA_Cross not recommended — best BTC net={best_btc['net']:+.2f}% "
                  f"sharpe={best_btc['sharpe']:+.2f} (below threshold)")

    if not recommendations:
        recommendations.append("✓ All strategies performing within acceptable parameters.")


    # ══════════════════════════════════════════════════════════════════════
    # Save athena.json
    # ══════════════════════════════════════════════════════════════════════

    os.makedirs('.aether', exist_ok=True)

    athena_data = {
        'run_time': t0.isoformat(),
        'data_range_days': lookback_days,
        'db_total_klines': storage.get_db_stats()['tables']['klines'],
        'strategies': [],
        'ma_cross_sweep_top5': ma_cross_sweep[:5] if ma_cross_sweep else [],
        'recommendations': recommendations,
        'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
    }

    for r in results_summary:
        entry = {
            'name': r['name'],
            'enabled': r['enabled'],
            'symbol': r['symbol'],
            'tf': r['tf'],
            'status': r['status'],
        }
        if r['status'] == 'OK':
            m = r['metrics']
            entry['metrics'] = {
                'total_return_pct': m['total_return_pct'],
                'sharpe_ratio': m['sharpe_ratio'],
                'max_drawdown_pct': m['max_drawdown_pct'],
                'win_rate': m['win_rate'],
                'profit_factor': m['profit_factor'],
                'total_trades': m['total_trades'],
                'avg_win_pct': m['avg_win_pct'],
                'avg_loss_pct': m['avg_loss_pct'],
            }
            entry['flags'] = r['flags']
            entry['bars'] = r['bars']
        athena_data['strategies'].append(entry)

    with open('.aether/athena.json', 'w') as f:
        json.dump(athena_data, f, indent=2, default=str)

    print(f"\n💾 athena.json written ({len(results_summary)} strategies evaluated)")

    # Print summary for bulletin
    print()
    print("═══ BULLETIN SUMMARY ═══")
    now_str = t0.strftime('%m-%d %H:%M')
    for r in results_summary:
        if r['status'] != 'OK':
            continue
        m = r['metrics']
        icon = "🟢" if r['enabled'] and not r['flags'] else "🔴" if r['flags'] and r['enabled'] else "⏸️"
        print(f"{icon} {r['name']}: net={m['total_return_pct']:+.2f}% "
              f"sharpe={m['sharpe_ratio']:+.2f} wr={m['win_rate']:.0f}% "
              f"dd={m['max_drawdown_pct']:.1f}% #T={m['total_trades']}")
    print(f"📋 {len(recommendations)} recommendation(s)")
    print(f"🦉 Athena pulse #{1} — {now_str} UTC")
