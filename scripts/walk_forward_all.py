#!/usr/bin/env python3
"""PERF-035: Walk-Forward Validation for all 5 LIVE strategies."""
import sys
sys.path.insert(0, '/home/rinnen/binance_quant')
import json
import pandas as pd
import numpy as np
from datetime import datetime

from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.walk_forward import walk_forward_validate, WFEInterpretation
from backtest.signal_gen import rsi_mr_signals, donchian_mr_signals, keltner_mr_signals

# ── Strategy definitions (from strategies.yaml) ──
STRATEGIES = {
    "RSI_MR_ETH": {
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "signal_func": "rsi_mr",
        "params": {
            "rsi_period": 14, "oversold": 20, "overbought": 75,
            "exit_rsi": 50, "sl_pct": 0.02, "tp_pct": 0.04,
            "cooldown_bars": 5
        },
        "leverage": 3,
    },
    "DonchianMR_ETH": {
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "signal_func": "donchian_mr",
        "params": {
            "donchian_period": 10, "rsi_period": 14,
            "oversold": 20, "overbought": 80, "exit_level": 50,
            "stop_loss_pct": 0.02, "take_profit_pct": 0.04,
            "cooldown_bars": 5
        },
        "leverage": 3,
    },
    "DonchianMR_BTC": {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "signal_func": "donchian_mr",
        "params": {
            "donchian_period": 8, "rsi_period": 14,
            "oversold": 20, "overbought": 80, "exit_level": 50,
            "stop_loss_pct": 0.015, "take_profit_pct": 0.04,
            "cooldown_bars": 9
        },
        "leverage": 3,
    },
    "KeltnerMR_BTC": {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "signal_func": "keltner_mr",
        "params": {
            "kc_period": 20, "atr_mult": 1.75, "atr_period": 14,
            "rsi_period": 14, "oversold": 20, "overbought": 75,
            "exit_level": 50, "stop_loss_pct": 0.015,
            "take_profit_pct": 0.02, "cooldown_bars": 3
        },
        "leverage": 3,
    },
    "KeltnerMR_ETH": {
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "signal_func": "keltner_mr",
        "params": {
            "kc_period": 24, "atr_mult": 2.0, "atr_period": 14,
            "rsi_period": 14, "oversold": 20, "overbought": 75,
            "exit_level": 50, "stop_loss_pct": 0.01,
            "take_profit_pct": 0.02, "cooldown_bars": 3
        },
        "leverage": 3,
    },
}

SIGNAL_FUNCS = {
    "rsi_mr": rsi_mr_signals,
    "donchian_mr": donchian_mr_signals,
    "keltner_mr": keltner_mr_signals,
}

def make_signal_wrapper(func, params):
    """Create a signal function that matches walk_forward's expected signature."""
    def wrapper(df, **overrides):
        merged = {**params, **overrides}
        return func(df, **merged)
    return wrapper

def load_data(symbol, timeframe, lookback_days=365):
    """Load OHLCV data from market.db with datetime index."""
    storage = MarketStorage()
    df = storage.load_klines(symbol, timeframe)
    if df.empty:
        return df
    # Convert open_time (ms) to datetime and set as index
    df['datetime'] = pd.to_datetime(df['open_time'], unit='ms')
    df = df.set_index('datetime').sort_index()
    # Keep last N days
    cutoff = df.index.max() - pd.Timedelta(days=lookback_days)
    df = df[df.index >= cutoff]
    return df[['open', 'high', 'low', 'close', 'volume']]

def main():
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)
    results = {}

    # Preload data
    data_cache = {}
    for sname, cfg in STRATEGIES.items():
        key = f"{cfg['symbol']}_{cfg['timeframe']}"
        if key not in data_cache:
            print(f"Loading {key} ...")
            data_cache[key] = load_data(cfg['symbol'], cfg['timeframe'])
            print(f"  {len(data_cache[key])} bars from {data_cache[key].index.min()} to {data_cache[key].index.max()}")

    for sname, cfg in STRATEGIES.items():
        key = f"{cfg['symbol']}_{cfg['timeframe']}"
        df = data_cache[key]
        if df.empty:
            results[sname] = {"error": "No data"}
            continue

        signal_func = make_signal_wrapper(SIGNAL_FUNCS[cfg['signal_func']], cfg['params'])

        print(f"\n{'='*60}")
        print(f"Walk-Forward: {sname} ({cfg['symbol']}, {cfg['timeframe']})")
        print(f"Params: {cfg['params']}")
        print(f"Data: {len(df)} bars, {df.index.min()} → {df.index.max()}")
        print(f"{'='*60}")

        try:
            wf = walk_forward_validate(
                df=df,
                signal_func=signal_func,
                engine=engine,
                train_days=90,
                test_days=30,
                n_trials=1,
                leverage=cfg['leverage'],
            )
            # Print per-window details
            details = wf.get('window_details', [])
            if details:
                print(f"  Windows: {len(details)}")
                for i, w in enumerate(details):
                    oos_ret = w.get('oos_return', 0)
                    is_ret = w.get('is_return', 0)
                    trades = w.get('oos_trades', 0)
                    print(f"    W{i+1}: IS={is_ret:+.1f}%  OOS={oos_ret:+.1f}%  trades={trades}  bars={w.get('test_bars','?')}")

            r = {
                "wfe": round(wf.get('wfe', 0), 4),
                "oos_sharpe": round(wf.get('oos_sharpe', 0), 4),
                "oos_calmar": round(wf.get('oos_calmar', 0), 4),
                "is_sharpe": round(wf.get('is_sharpe', 0), 4),
                "is_calmar": round(wf.get('is_calmar', 0), 4),
                "oos_max_dd_pct": round(wf.get('oos_max_drawdown_pct', 0), 2),
                "is_max_dd_pct": round(wf.get('is_max_drawdown_pct', 0), 2),
                "oos_win_rate": round(wf.get('oos_win_rate', 0), 2),
                "total_is_return_pct": round(wf.get('total_is_return_pct', 0), 2),
                "total_oos_return_pct": round(wf.get('total_oos_return_pct', 0), 2),
                "windows": wf.get('windows', 0),
                "interpretation": wf.get('interpretation', 'N/A'),
                "passed": wf.get('passed', False),
                "dsr": round(wf.get('deflated_sharpe_ratio', 0), 4),
                "window_details": details,
            }
            results[sname] = r
            print(f"\n  WFE={r['wfe']:.3f}  OOS_SR={r['oos_sharpe']:.3f}  IS_SR={r['is_sharpe']:.3f}")
            print(f"  OOS_Ret={r['total_oos_return_pct']:+.1f}%  IS_Ret={r['total_is_return_pct']:+.1f}%")
            print(f"  OOS_DD={r['oos_max_dd_pct']:.1f}%  OOS_WR={r['oos_win_rate']:.2f}")
            print(f"  Verdict: {r['interpretation']}")
            print(f"  PASSED: {r['passed']}")
        except Exception as e:
            results[sname] = {"error": str(e)}
            print(f"  ERROR: {e}")

    # ── Summary ──
    print(f"\n{'='*80}")
    print(f"WALK-FORWARD SUMMARY — {datetime.now().isoformat()}")
    print(f"{'='*80}")
    print(f"{'Strategy':22s} {'WFE':>7s} {'OOS_SR':>7s} {'IS_SR':>7s} {'OOS_Ret':>8s} {'OOS_DD':>7s} {'Windows':>7s} {'Passed':>6s}")
    print(f"{'-'*80}")
    for sname, r in results.items():
        if 'error' in r:
            print(f"{sname:22s} ERROR: {r['error'][:50]}")
        else:
            print(f"{sname:22s} {r['wfe']:7.3f} {r['oos_sharpe']:7.3f} {r['is_sharpe']:7.3f} "
                  f"{r['total_oos_return_pct']:+7.1f}% {r['oos_max_dd_pct']:6.1f}% {r['windows']:7d} {str(r['passed']):>6s}")

    # ── Save results ──
    output = {
        "run_at": datetime.now().isoformat(),
        "method": "anchored_walk_forward",
        "train_days": 90,
        "test_days": 30,
        "leverage": 3,
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results.values() if isinstance(r, dict) and r.get('passed')),
            "failed": sum(1 for r in results.values() if isinstance(r, dict) and not r.get('passed')),
            "errors": sum(1 for r in results.values() if 'error' in r),
        }
    }
    with open('.aether/state/walk_forward_results.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to .aether/state/walk_forward_results.json")
    return output

if __name__ == '__main__':
    main()
