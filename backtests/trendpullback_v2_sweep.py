#!/usr/bin/env python3
"""TrendPullback v2 Parameters Sweep — Athena Request #126

v1 Bug: near_ema filter computed but never used in entry condition — 
        strategy degraded to pure TrendFollow (DD=45.9%).
v2 Fix: EMA dual-line alignment + near_ema pullback + RSI [rsi_low, rsi_high] filter.

Param grid: ema_fast[30,50,75] × ema_slow[100,150] × atr_sl[1.0,1.5,2.0] ×
            atr_tp[2.0,3.0,4.0] × rsi_low[25,30,35] × rsi_high[65,70,75] × cd[5,8,12]
            = 1,458 combos × 2 symbols = 2,916 total runs.
Target: SR>0.3, DD<25%.
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
from backtest.signal_gen import _compute_rsi, _compute_atr


def trend_pullback_v2_signals(
    df: pd.DataFrame,
    ema_fast: int = 50,
    ema_slow: int = 100,
    atr_period: int = 14,
    atr_sl_mult: float = 1.5,
    atr_tp_mult: float = 3.0,
    rsi_period: int = 14,
    rsi_low: float = 30.0,
    rsi_high: float = 70.0,
    cooldown_bars: int = 8,
) -> np.ndarray:
    """Generate TrendPullback v2 signals.

    Vectorized indicator computation + bar-by-bar state tracking
    for cooldown and position management.

    Returns: np.ndarray of int8: 1=LONG, -1=SHORT, 0=FLAT
    """
    close = df['close'].values.astype(np.float64)
    high = df['high'].values.astype(np.float64)
    low = df['low'].values.astype(np.float64)
    n = len(close)

    # ── Vectorized indicators ──
    ema_f = pd.Series(close).ewm(span=ema_fast, adjust=False).mean().values
    ema_s = pd.Series(close).ewm(span=ema_slow, adjust=False).mean().values
    ema_f_slope = np.diff(ema_f, prepend=ema_f[0])  # simplified 1-bar slope
    # 5-bar slope
    ema_f_slope5 = np.zeros(n)
    ema_f_slope5[5:] = ema_f[5:] - ema_f[:-5]

    aligned_up = (ema_f > ema_s) & (ema_f_slope5 > 0)
    aligned_down = (ema_f < ema_s) & (ema_f_slope5 < 0)

    atr = _compute_atr(high, low, close, atr_period)
    dist_from_ema = np.abs(close - ema_f)
    near_ema = dist_from_ema < atr

    rsi = _compute_rsi(close, rsi_period)
    rsi_ok = (rsi > rsi_low) & (rsi < rsi_high)

    # ── Bar-by-bar state tracking for cooldown ──
    signals = np.zeros(n, dtype=np.int8)
    bars_since_trade = cooldown_bars + 1
    in_position = False
    pos_side = 0  # 1=LONG, -1=SHORT
    entry_price = 0.0

    for i in range(max(ema_slow, atr_period, rsi_period) + 10, n):
        bars_since_trade += 1
        price = close[i]
        cur_atr = atr[i]

        # Position management
        if in_position:
            # Trend break → close
            if pos_side == 1 and not aligned_up[i]:
                signals[i] = 0  # close — engine handles this
                in_position = False
                bars_since_trade = 0
                continue
            if pos_side == -1 and not aligned_down[i]:
                signals[i] = 0
                in_position = False
                bars_since_trade = 0
                continue

            # RSI extreme → close
            if pos_side == 1 and rsi[i] > 75:
                signals[i] = 0
                in_position = False
                bars_since_trade = 0
                continue
            if pos_side == -1 and rsi[i] < 25:
                signals[i] = 0
                in_position = False
                bars_since_trade = 0
                continue

            # ATR stop loss / take profit
            atr_capped = min(cur_atr, price * 0.05) if cur_atr > 0 else price * 0.01
            sl_dist = atr_capped * atr_sl_mult
            tp_dist = atr_capped * atr_tp_mult
            if pos_side == 1:
                if price <= entry_price - sl_dist:
                    signals[i] = 0
                    in_position = False
                    bars_since_trade = 0
                    continue
                if price >= entry_price + tp_dist:
                    signals[i] = 0
                    in_position = False
                    bars_since_trade = 0
                    continue
            else:
                if price >= entry_price + sl_dist:
                    signals[i] = 0
                    in_position = False
                    bars_since_trade = 0
                    continue
                if price <= entry_price - tp_dist:
                    signals[i] = 0
                    in_position = False
                    bars_since_trade = 0
                    continue

            # Hold
            signals[i] = pos_side
            continue

        # Entry: trend + pullback + RSI filter (after cooldown)
        if bars_since_trade <= cooldown_bars:
            signals[i] = 0
            continue

        if not (np.isfinite(rsi[i]) and rsi_ok[i] and np.isfinite(cur_atr) and cur_atr > 0):
            signals[i] = 0
            continue

        if aligned_up[i] and near_ema[i]:
            signals[i] = 1
            in_position = True
            pos_side = 1
            entry_price = price
            bars_since_trade = 0
        elif aligned_down[i] and near_ema[i]:
            signals[i] = -1
            in_position = True
            pos_side = -1
            entry_price = price
            bars_since_trade = 0
        else:
            signals[i] = 0

    return signals


@dataclass
class SweepResult:
    symbol: str
    ema_fast: int
    ema_slow: int
    atr_sl_mult: float
    atr_tp_mult: float
    rsi_low: float
    rsi_high: float
    cooldown_bars: int
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
    print(f"🔥 Prometheus — TrendPullback v2 Parameter Sweep")
    print(f"{'═' * 70}")
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Request: #126 (Athena — TrendPullback v2 DRAFT→BACKTEST)")
    print(f"v1 Bug: near_ema filter unused → strategy degraded to TrendFollow (DD=45.9%)")
    print(f"v2 Fix: EMA dual-line alignment + near_ema pullback + RSI filter")
    print()

    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    timeframe = '1h'

    # Load data
    data_cache = {}
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        df = storage.load_klines(symbol, timeframe)
        if df.empty:
            print(f"  ⚠ {symbol}: no data")
            continue
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('open_time', inplace=True)
        df.sort_index(inplace=True)
        # Use last 365 days
        cutoff = df.index[-1] - pd.Timedelta(days=365)
        df = df[df.index >= cutoff]
        if len(df) > 0:
            data_cache[symbol] = df
            span = (df.index[-1] - df.index[0]).days
            print(f"  📊 {symbol:10s} {timeframe} 365d: {len(df):5d} bars, "
                  f"~{span}d [{df.index[0].strftime('%m/%d')}→{df.index[-1].strftime('%m/%d')}]")

    if not data_cache:
        print("FATAL: No data loaded")
        sys.exit(1)

    # Param grid per Athena request #126
    ema_fasts = [30, 50, 75]
    ema_slows = [100, 150]
    atr_sls = [1.0, 1.5, 2.0]
    atr_tps = [2.0, 3.0, 4.0]
    rsi_lows = [25, 30, 35]
    rsi_highs = [65, 70, 75]
    cooldowns = [5, 8, 12]

    param_combos = list(itertools.product(
        ema_fasts, ema_slows, atr_sls, atr_tps, rsi_lows, rsi_highs, cooldowns
    ))
    n_combos = len(param_combos)
    total_runs = n_combos * len(data_cache)
    print(f"\nParam combos: {n_combos} × {len(data_cache)} symbols = {total_runs} total runs")
    print(f"Grid: EMAf{ema_fasts} × EMAs{ema_slows} × "
          f"ATRsl{atr_sls} × ATRtp{atr_tps} × "
          f"RSIl{rsi_lows} × RSIh{rsi_highs} × CD{cooldowns}")
    print()

    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    all_results = []

    for symbol, df in data_cache.items():
        print(f"▶ Backtesting {symbol}...")
        cnt = 0
        for (ef, es, sl, tp, rl, rh, cd) in param_combos:
            cnt += 1
            try:
                sig_arr = trend_pullback_v2_signals(
                    df, ema_fast=ef, ema_slow=es, atr_sl_mult=sl,
                    atr_tp_mult=tp, rsi_low=rl, rsi_high=rh,
                    cooldown_bars=cd)
                # Convert to pd.Series with df.index — engine expects aligned series
                sig = pd.Series(sig_arr, index=df.index).fillna(0).astype(int)
                res = engine.run(df, sig, leverage=3, n_trials=total_runs)
                m = res['metrics']
            except Exception as e:
                if cnt <= 5:
                    print(f"  [{cnt}] ERROR: {e}")
                continue

            all_results.append(SweepResult(
                symbol=symbol,
                ema_fast=ef, ema_slow=es,
                atr_sl_mult=sl, atr_tp_mult=tp,
                rsi_low=float(rl), rsi_high=float(rh),
                cooldown_bars=cd,
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
            if cnt % 200 == 0:
                print(f"  [{cnt}/{n_combos}] {symbol} ({_time.time()-t_start:.1f}s)")
        print(f"  ✓ {symbol} done ({len([r for r in all_results if r.symbol == symbol])} runs)")

    elapsed = _time.time() - t_start
    print(f"\n⏱ Sweep complete: {elapsed:.1f}s\n")

    if not all_results:
        print("FATAL: No results generated")
        sys.exit(1)

    results_df = pd.DataFrame([asdict(r) for r in all_results])

    # ─── Per-symbol TOP 10 ───
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym_results = results_df[results_df['symbol'] == symbol]
        if len(sym_results) == 0:
            continue
        print(f"{'═' * 70}")
        print(f"📊 {symbol} — TOP 10 by Sharpe (365d)")
        print(f"{'═' * 70}")
        top10 = sym_results.nlargest(10, 'sharpe')
        for _, r in top10.iterrows():
            print(f"  EMAf={int(r['ema_fast']):2d} EMAs={int(r['ema_slow']):3d} "
                  f"SL={r['atr_sl_mult']:.1f}x TP={r['atr_tp_mult']:.1f}x "
                  f"RSIl={r['rsi_low']:.0f} RSIh={r['rsi_high']:.0f} CD={int(r['cooldown_bars']):2d} | "
                  f"Net={r['net_pct']:+.2f}% SR={r['sharpe']:.4f} DSR={r['dsr']:.4f} "
                  f"DD={r['max_dd']:.1f}% WR={r['win_rate']:.0f}% T={int(r['trades']):3d}")

    # ─── FINAL VERDICT ───
    print(f"\n{'═' * 70}")
    print("🏁 FINAL VERDICT (Target: SR>0.3 DD<25%)")
    for symbol in ['BTC/USDT', 'ETH/USDT']:
        sym = results_df[results_df['symbol'] == symbol]
        if len(sym) == 0:
            print(f"  {symbol}: ❌ NO DATA")
            continue
        best = sym.nlargest(1, 'sharpe').iloc[0]
        passing = sym[(sym['sharpe'] >= 0.3) &
                      (sym['max_dd'] <= 25) &
                      (sym['trades'] >= 20)]
        if len(passing) > 0:
            top = passing.nlargest(1, 'sharpe').iloc[0]
            print(f"  {symbol}: ✅ PASS — {len(passing)} combos meet criteria")
            print(f"    Best: EMAf={int(top['ema_fast'])} EMAs={int(top['ema_slow'])} "
                  f"SL={top['atr_sl_mult']:.1f}x TP={top['atr_tp_mult']:.1f}x "
                  f"RSIl={top['rsi_low']:.0f} RSIh={top['rsi_high']:.0f} CD={int(top['cooldown_bars'])} "
                  f"→ Net={top['net_pct']:+.2f}% SR={top['sharpe']:.3f} DD={top['max_dd']:.1f}% "
                  f"WR={top['win_rate']:.0f}% T={int(top['trades'])}")
        else:
            print(f"  {symbol}: ❌ DO_NOT_ENABLE — no config meets SR>0.3 DD<25% T≥20")
            print(f"    Best overall: EMAf={int(best['ema_fast'])} EMAs={int(best['ema_slow'])} "
                  f"SL={best['atr_sl_mult']:.1f}x TP={best['atr_tp_mult']:.1f}x "
                  f"RSIl={best['rsi_low']:.0f} RSIh={best['rsi_high']:.0f} CD={int(best['cooldown_bars'])} "
                  f"→ Net={best['net_pct']:+.2f}% SR={best['sharpe']:.3f} DD={best['max_dd']:.1f}% "
                  f"WR={best['win_rate']:.0f}% T={int(best['trades'])}")
            reasons = []
            if best['sharpe'] < 0.3: reasons.append(f"SR={best['sharpe']:.3f}<0.3")
            if best['max_dd'] > 25: reasons.append(f"DD={best['max_dd']:.1f}%>25%")
            if best['trades'] < 20: reasons.append(f"T={int(best['trades'])}<20")
            print(f"    Reasons: {', '.join(reasons) if reasons else 'none'}")

    # ─── Save to backtest_results.json ───
    print(f"\n{'═' * 70}")
    print("💾 Updating backtest_results.json...")
    bt_path = '/home/rinnen/binance_quant/.aether/state/backtest_results.json'
    with open(bt_path, 'r') as f:
        bt_data = json.load(f)

    for symbol, strategy_key in [('BTC/USDT', 'TrendPullback_BTC'), ('ETH/USDT', 'TrendPullback_ETH')]:
        sym = results_df[results_df['symbol'] == symbol]
        if len(sym) == 0:
            continue
        best = sym.nlargest(1, 'sharpe').iloc[0]
        passing = sym[(sym['sharpe'] >= 0.3) &
                      (sym['max_dd'] <= 25) &
                      (sym['trades'] >= 20)]
        if len(passing) > 0:
            top = passing.nlargest(1, 'sharpe').iloc[0]
            final_verdict = 'PAPER' if top['sharpe'] < 0.5 else 'LIVE'
        else:
            top = best
            final_verdict = 'DO_NOT_ENABLE'

        bt_data['strategies'][strategy_key] = {
            "enabled": final_verdict != 'DO_NOT_ENABLE',
            "verdict": final_verdict,
            "metrics": {
                "sharpe_ratio": round(top['sharpe'], 3),
                "dd_pct": round(top['max_dd'], 1),
                "return_pct": round(top['net_pct'], 2),
                "win_rate": round(top['win_rate'], 1),
                "trades": int(top['trades']),
                "profit_factor": round(top['pf'], 3),
            },
            "retired_reason": f"v2 sweep: SR={top['sharpe']:.3f} DD={top['max_dd']:.1f}% T={int(top['trades'])} — {'below threshold' if final_verdict == 'DO_NOT_ENABLE' else 'PAPER candidate'}",
            "params": {
                "ema_fast": int(top['ema_fast']),
                "ema_slow": int(top['ema_slow']),
                "atr_sl_mult": float(top['atr_sl_mult']),
                "atr_tp_mult": float(top['atr_tp_mult']),
                "rsi_low": float(top['rsi_low']),
                "rsi_high": float(top['rsi_high']),
                "cooldown_bars": int(top['cooldown_bars']),
            }
        }
        print(f"  {strategy_key}: verdict={final_verdict} | "
              f"Net={top['net_pct']:+.2f}% SR={top['sharpe']:.3f} DD={top['max_dd']:.1f}% T={int(top['trades'])}")

    # Save sweep data
    bt_data['trendpullback_v2_sweep'] = {
        "updated_at": t0.isoformat(),
        "strategy": "TrendPullback_v2",
        "request_id": 126,
        "lookback_days": 365,
        "timeframe": "1h",
        "symbols": ["BTC/USDT", "ETH/USDT"],
        "total_combos": n_combos,
        "total_runs": len(all_results),
        "elapsed_seconds": round(elapsed, 1),
        "verdicts": {
            sym: {
                "verdict": "PAPER" if len(p) > 0 else "DO_NOT_ENABLE",
                "passing_combos": len(p),
                "best_sharpe": float(p['sharpe'].max()) if len(p) > 0 else float(sym_data['sharpe'].max()),
                "best_dd": float(p['max_dd'].min()) if len(p) > 0 else float(sym_data['max_dd'].min()),
            }
            for sym in ['BTC/USDT', 'ETH/USDT']
            if (sym_data := results_df[results_df['symbol'] == sym]).__len__() > 0
            for p in [sym_data[(sym_data['sharpe'] >= 0.3) & (sym_data['max_dd'] <= 25) & (sym_data['trades'] >= 20)]]
        },
        "top10_by_sharpe": [
            {k: (float(v) if isinstance(v, (np.floating, np.integer)) else
                 int(v) if isinstance(v, np.integer) else v)
             for k, v in row.items()}
            for _, row in results_df.nlargest(10, 'sharpe').iterrows()
        ],
    }

    bt_data['_updated_at'] = t0.strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(bt_path, 'w') as f:
        json.dump(bt_data, f, indent=2, ensure_ascii=False, default=str)

    # ─── Update prometheus.json ───
    print("💾 Updating prometheus.json...")
    prom_path = '/home/rinnen/binance_quant/.aether/state/prometheus.json'
    with open(prom_path, 'r') as f:
        prom_data = json.load(f)

    # Add to active_research
    prom_data['active_research']['PERF-077'] = {
        "title": "TrendPullback v2 backtest sweep (request #126)",
        "status": "completed",
        "target": "backtests/trendpullback_v2_sweep.py → TrendPullback_BTC/ETH v2",
        "description": f"{n_combos}-combo TrendPullback v2 sweep. "
                       f"BTC best SR={results_df[results_df['symbol']=='BTC/USDT']['sharpe'].max():.3f}, "
                       f"ETH best SR={results_df[results_df['symbol']=='ETH/USDT']['sharpe'].max():.3f}. "
                       f"v1 DD=45.9%. v2 fixes: EMA dual-line alignment + near_ema pullback + RSI[rsi_low,rsi_high] filter.",
        "deployed": t0.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "request_id": 126,
        "total_runs": len(all_results),
        "elapsed_s": round(elapsed, 1),
    }
    prom_data['last_run'] = t0.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    prom_data['_updated_at'] = t0.isoformat()
    with open(prom_path, 'w') as f:
        json.dump(prom_data, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Done. Elapsed: {elapsed:.1f}s. Results saved.")
