"""
Walk-Forward Validation for Anti-Overfitting.

Complements Deflated Sharpe Ratio (DSR) by measuring out-of-sample
performance stability. Walk-Forward Efficiency (WFE) = OOS return / IS return.
WFE > 0.5 = strategy generalizes; WFE < 0.3 = likely overfit.

Usage:
    from backtest.walk_forward import walk_forward_validate, WFEInterpretation
    result = walk_forward_validate(df, signal_func, engine, **params)
    print(WFEInterpretation(result['wfe']))
"""

from typing import Callable, Dict, Any
import pandas as pd
import numpy as np
from datetime import timedelta


def walk_forward_validate(
    df: pd.DataFrame,
    signal_func: Callable,
    engine,
    train_days: int = 60,
    test_days: int = 30,
    min_train_bars: int = 50,
    min_test_bars: int = 10,
    **signal_kwargs,
) -> Dict[str, Any]:
    """Run walk-forward cross-validation on a strategy.

    Splits data into rolling train/test windows. Strategy parameters are
    held constant across windows (this is "anchored" walk-forward — tests
    whether a fixed parameter set generalizes, not whether re-optimization
    helps).

    Args:
        df: OHLCV DataFrame with datetime index, sorted ascending
        signal_func: Function(df, **kwargs) -> pd.Series of signals (1/-1/0)
        engine: BacktestEngine instance
        train_days: Days per training (in-sample) window
        test_days: Days per test (out-of-sample) window
        min_train_bars: Minimum bars required for a training window
        min_test_bars: Minimum bars required for a test window
        **signal_kwargs: Passed to signal_func

    Returns:
        dict with keys:
            wfe: Walk-Forward Efficiency (OOS return / IS return)
            oos_sharpe: Annualized OOS Sharpe ratio
            is_sharpe: Annualized IS Sharpe ratio
            total_is_return_pct: Sum of in-sample returns
            total_oos_return_pct: Sum of out-of-sample returns
            windows: Number of valid walk-forward windows
            window_details: List of per-window results
            interpretation: String interpretation of WFE
            passed: bool — True if WFE >= 0.3 (not clearly overfit)
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("DataFrame must have DatetimeIndex")

    if len(df) < train_days + test_days:
        # Not enough data for rolling windows — single split
        split = max(int(len(df) * 0.7), min_train_bars)
        if split >= len(df) - min_test_bars:
            return _empty_wf_result("Insufficient data for train/test split")

        train_df = df.iloc[:split]
        test_df = df.iloc[split:]

        is_sig = signal_func(train_df, **signal_kwargs)
        is_result = engine.run(train_df, is_sig)
        oos_sig = signal_func(test_df, **signal_kwargs)
        oos_result = engine.run(test_df, oos_sig)

        is_ret = is_result['metrics']['total_return_pct']
        oos_ret = oos_result['metrics']['total_return_pct']
        wfe = oos_ret / is_ret if abs(is_ret) > 1e-9 else 0.0

        return _build_result(
            wfe=wfe,
            is_return=is_ret,
            oos_return=oos_ret,
            is_sharpe=is_result['metrics']['sharpe_ratio'],
            oos_sharpe=oos_result['metrics']['sharpe_ratio'],
            windows=1,
            details=[{
                'train': (str(df.index[0]), str(df.index[split-1])),
                'test': (str(df.index[split]), str(df.index[-1])),
                'is_return': is_ret,
                'oos_return': oos_ret,
                'is_sharpe': is_result['metrics']['sharpe_ratio'],
                'oos_sharpe': oos_result['metrics']['sharpe_ratio'],
            }],
        )

    # Rolling walk-forward windows
    window_details = []
    total_is_return = 0.0
    total_oos_return = 0.0
    all_oos_daily_returns = []

    start = df.index.min()
    end = df.index.max()
    window_start = start

    while window_start + timedelta(days=train_days + test_days) <= end:
        train_end = window_start + timedelta(days=train_days)
        test_end = train_end + timedelta(days=test_days)

        train_df = df.loc[window_start:train_end].copy()
        test_df = df.loc[train_end:test_end].copy()

        if len(train_df) < min_train_bars or len(test_df) < min_test_bars:
            window_start = test_end
            continue

        # In-sample
        is_sig = signal_func(train_df, **signal_kwargs)
        is_result = engine.run(train_df, is_sig)

        # Out-of-sample (same parameters)
        oos_sig = signal_func(test_df, **signal_kwargs)
        oos_result = engine.run(test_df, oos_sig)

        is_ret = is_result['metrics']['total_return_pct']
        oos_ret = oos_result['metrics']['total_return_pct']

        total_is_return += is_ret
        total_oos_return += oos_ret

        # Collect OOS daily returns for Sharpe
        if not oos_result['equity_curve'].empty:
            oos_daily = oos_result['equity_curve'].resample('D').last().dropna().pct_change().dropna()
            all_oos_daily_returns.extend(oos_daily.values.tolist())

        window_details.append({
            'train': (str(window_start), str(train_end)),
            'test': (str(train_end), str(test_end)),
            'train_bars': len(train_df),
            'test_bars': len(test_df),
            'is_return': round(is_ret, 2),
            'oos_return': round(oos_ret, 2),
            'is_sharpe': round(is_result['metrics']['sharpe_ratio'], 3),
            'oos_sharpe': round(oos_result['metrics']['sharpe_ratio'], 3),
        })

        window_start = test_end

    if not window_details:
        return _empty_wf_result("No valid walk-forward windows found")

    wfe = total_oos_return / total_is_return if abs(total_is_return) > 1e-9 else 0.0

    # OOS Sharpe
    if all_oos_daily_returns:
        oos_daily_arr = np.array(all_oos_daily_returns)
        oos_sharpe = float((oos_daily_arr.mean() / oos_daily_arr.std()) * np.sqrt(365)) if oos_daily_arr.std() > 1e-12 else 0.0
    else:
        # Fallback: average of per-window OOS Sharpes
        oos_sharpes = [d['oos_sharpe'] for d in window_details if d['oos_sharpe'] != 0]
        oos_sharpe = float(np.mean(oos_sharpes)) if oos_sharpes else 0.0

    # IS Sharpe (average)
    is_sharpes = [d['is_sharpe'] for d in window_details if d['is_sharpe'] != 0]
    is_sharpe = float(np.mean(is_sharpes)) if is_sharpes else 0.0

    return _build_result(
        wfe=wfe,
        is_return=total_is_return,
        oos_return=total_oos_return,
        is_sharpe=is_sharpe,
        oos_sharpe=oos_sharpe,
        windows=len(window_details),
        details=window_details,
    )


def _build_result(wfe, is_return, oos_return, is_sharpe, oos_sharpe, windows, details):
    """Build standardized result dict."""
    passed = wfe >= 0.3
    if wfe >= 0.7:
        interp = "STRONG — strategy generalizes well, low overfitting risk"
    elif wfe >= 0.5:
        interp = "GOOD — acceptable out-of-sample performance"
    elif wfe >= 0.3:
        interp = "MARGINAL — some degradation but not clearly overfit"
    elif wfe >= 0.0:
        interp = "WEAK — significant OOS degradation, likely overfit"
    else:
        interp = "BROKEN — OOS performance is negative while IS is positive, classic overfitting"

    return {
        'wfe': round(wfe, 4),
        'total_is_return_pct': round(is_return, 2),
        'total_oos_return_pct': round(oos_return, 2),
        'is_sharpe': round(is_sharpe, 3),
        'oos_sharpe': round(oos_sharpe, 3),
        'windows': windows,
        'window_details': details,
        'interpretation': f"WFE={wfe:.3f} — {interp}",
        'passed': passed,
    }


def _empty_wf_result(reason: str) -> Dict[str, Any]:
    return {
        'wfe': 0.0,
        'total_is_return_pct': 0.0,
        'total_oos_return_pct': 0.0,
        'is_sharpe': 0.0,
        'oos_sharpe': 0.0,
        'windows': 0,
        'window_details': [],
        'interpretation': f"Skipped: {reason}",
        'passed': False,
    }


def WFEInterpretation(wfe: float) -> str:
    """Human-readable interpretation of Walk-Forward Efficiency."""
    if wfe >= 0.7:
        return f"🟢 WFE={wfe:.3f}: Strong generalization (low overfit risk)"
    elif wfe >= 0.5:
        return f"🟡 WFE={wfe:.3f}: Acceptable generalization"
    elif wfe >= 0.3:
        return f"🟠 WFE={wfe:.3f}: Marginal — some OOS degradation"
    elif wfe >= 0.0:
        return f"🔴 WFE={wfe:.3f}: Weak — likely overfit"
    else:
        return f"⛔ WFE={wfe:.3f}: Broken — classic overfitting pattern"
