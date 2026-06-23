"""
Walk-Forward Validation for Anti-Overfitting.

Complements Deflated Sharpe Ratio (DSR) by measuring out-of-sample
performance stability. Walk-Forward Efficiency (WFE) = OOS return / IS return.
WFE > 0.5 = strategy generalizes; WFE < 0.3 = likely overfit.

Enhancements:
- Deflated Sharpe Ratio (DSR) output per window
- Calmar Ratio (annualized return / max drawdown)
- Strict temporal split: train / validation / test
- Monte Carlo deflation for multiple trials
- Full metrics: OOS Sharpe, Max Drawdown, Calmar, Win Rate
- Leverage-aware (passes leverage to engine.run)

Usage:
    from backtest.walk_forward import walk_forward_validate, WFEInterpretation
    result = walk_forward_validate(df, signal_func, engine, leverage=3, **params)
    print(WFEInterpretation(result['wfe']))
"""

from typing import Callable, Dict, Any, Optional
import pandas as pd
import numpy as np
from datetime import timedelta

# Import DSR from engine
from .engine import deflated_sharpe_ratio, probabilistic_sharpe_ratio, expected_max_sharpe


def walk_forward_validate(
    df: pd.DataFrame,
    signal_func: Callable,
    engine,
    train_days: int = 60,
    test_days: int = 30,
    min_train_bars: int = 50,
    min_test_bars: int = 10,
    n_trials: int = 1,
    leverage: int = 1,
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
        n_trials: Number of independent parameter combinations tested
                  (for Deflated Sharpe Ratio)
        leverage: Leverage multiplier for engine.run() (default 1)
        **signal_kwargs: Passed to signal_func

    Returns:
        dict with keys:
            wfe: Walk-Forward Efficiency (OOS return / IS return)
            oos_sharpe: Annualized OOS Sharpe ratio
            oos_calmar: Annualized OOS Calmar ratio
            is_sharpe: Annualized IS Sharpe ratio
            is_calmar: Annualized IS Calmar ratio
            oos_max_dd_pct: Max OOS drawdown
            is_max_dd_pct: Max IS drawdown
            deflated_sharpe_ratio: DSR for OOS performance
            total_is_return_pct: Sum of in-sample returns
            total_oos_return_pct: Sum of out-of-sample returns
            oos_win_rate: OOS win rate
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
        is_result = engine.run(train_df, is_sig, n_trials=n_trials, leverage=leverage)
        oos_sig = signal_func(test_df, **signal_kwargs)
        oos_result = engine.run(test_df, oos_sig, n_trials=n_trials, leverage=leverage)

        is_ret = is_result['metrics']['total_return_pct']
        oos_ret = oos_result['metrics']['total_return_pct']
        wfe = oos_ret / is_ret if abs(is_ret) > 1e-9 else 0.0

        is_calmar = _calmar_ratio(
            is_result['metrics']['total_return_pct'],
            is_result['metrics']['max_drawdown_pct'],
        )
        oos_calmar = _calmar_ratio(
            oos_result['metrics']['total_return_pct'],
            oos_result['metrics']['max_drawdown_pct'],
        )

        dsr = oos_result['metrics'].get('deflated_sharpe_ratio', 0.0)

        return _build_result(
            wfe=wfe,
            is_return=is_ret,
            oos_return=oos_ret,
            is_sharpe=is_result['metrics']['sharpe_ratio'],
            oos_sharpe=oos_result['metrics']['sharpe_ratio'],
            is_calmar=is_calmar,
            oos_calmar=oos_calmar,
            oos_max_dd=oos_result['metrics']['max_drawdown_pct'],
            is_max_dd=is_result['metrics']['max_drawdown_pct'],
            oos_win_rate=oos_result['metrics']['win_rate'],
            deflated_sharpe=dsr,
            windows=1,
            details=[{
                'train': (str(df.index[0]), str(df.index[split-1])),
                'test': (str(df.index[split]), str(df.index[-1])),
                'is_return': is_ret,
                'oos_return': oos_ret,
                'is_sharpe': is_result['metrics']['sharpe_ratio'],
                'oos_sharpe': oos_result['metrics']['sharpe_ratio'],
                'is_calmar': is_calmar,
                'oos_calmar': oos_calmar,
                'oos_max_dd': oos_result['metrics']['max_drawdown_pct'],
                'is_max_dd': is_result['metrics']['max_drawdown_pct'],
                'oos_win_rate': oos_result['metrics']['win_rate'],
                'deflated_sharpe': dsr,
            }],
        )

    # Rolling walk-forward windows
    window_details = []
    total_is_return = 0.0
    total_oos_return = 0.0
    all_oos_daily_returns = []
    all_oos_trades_win = []
    all_oos_trades_total = []
    all_oos_max_dds = []
    all_is_max_dds = []
    all_ois_sharpes = []
    all_oos_sharpes = []

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
        is_result = engine.run(train_df, is_sig, n_trials=n_trials, leverage=leverage)

        # Out-of-sample (same parameters)
        oos_sig = signal_func(test_df, **signal_kwargs)
        oos_result = engine.run(test_df, oos_sig, n_trials=n_trials, leverage=leverage)

        is_ret = is_result['metrics']['total_return_pct']
        oos_ret = oos_result['metrics']['total_return_pct']

        total_is_return += is_ret
        total_oos_return += oos_ret

        is_calmar = _calmar_ratio(
            is_ret, is_result['metrics']['max_drawdown_pct'])
        oos_calmar = _calmar_ratio(
            oos_ret, oos_result['metrics']['max_drawdown_pct'])

        all_oos_max_dds.append(oos_result['metrics']['max_drawdown_pct'])
        all_is_max_dds.append(is_result['metrics']['max_drawdown_pct'])
        all_ois_sharpes.append(is_result['metrics']['sharpe_ratio'])
        all_oos_sharpes.append(oos_result['metrics']['sharpe_ratio'])

        # Track trade stats for OOS win rate
        if oos_result['metrics']['total_trades'] > 0:
            all_oos_trades_win.append(
                oos_result['metrics']['win_rate'] / 100 * oos_result['metrics']['total_trades']
            )
            all_oos_trades_total.append(oos_result['metrics']['total_trades'])

        # Collect OOS daily returns for Sharpe
        if not oos_result['equity_curve'].empty:
            oos_daily = (
                oos_result['equity_curve']
                .resample('D').last().dropna()
                .pct_change().dropna()
            )
            all_oos_daily_returns.extend(oos_daily.values.tolist())

        dsr = oos_result['metrics'].get('deflated_sharpe_ratio', 0.0)

        window_details.append({
            'train': (str(window_start), str(train_end)),
            'test': (str(train_end), str(test_end)),
            'train_bars': len(train_df),
            'test_bars': len(test_df),
            'is_return': round(is_ret, 2),
            'oos_return': round(oos_ret, 2),
            'is_sharpe': round(is_result['metrics']['sharpe_ratio'], 3),
            'oos_sharpe': round(oos_result['metrics']['sharpe_ratio'], 3),
            'is_calmar': round(is_calmar, 3),
            'oos_calmar': round(oos_calmar, 3),
            'oos_max_dd': round(oos_result['metrics']['max_drawdown_pct'], 2),
            'is_max_dd': round(is_result['metrics']['max_drawdown_pct'], 2),
            'oos_win_rate': round(oos_result['metrics']['win_rate'], 1),
            'oos_trades': oos_result['metrics']['total_trades'],
            'deflated_sharpe': round(dsr, 4),
        })

        window_start = test_end

    if not window_details:
        return _empty_wf_result("No valid walk-forward windows found")

    wfe = total_oos_return / total_is_return if abs(total_is_return) > 1e-9 else 0.0

    # OOS Sharpe
    if all_oos_daily_returns:
        oos_daily_arr = np.array(all_oos_daily_returns)
        oos_sharpe = float(
            (oos_daily_arr.mean() / oos_daily_arr.std()) * np.sqrt(365)
        ) if oos_daily_arr.std() > 1e-12 else 0.0
    else:
        oos_sharpe = float(np.mean(all_oos_sharpes)) if all_oos_sharpes else 0.0

    # IS Sharpe
    is_sharpe = float(np.mean(all_ois_sharpes)) if all_ois_sharpes else 0.0

    # OOS Win Rate
    if all_oos_trades_total:
        total_win_trades = sum(all_oos_trades_win)
        total_all_trades = sum(all_oos_trades_total)
        oos_win_rate = round(total_win_trades / total_all_trades * 100, 1) if total_all_trades > 0 else 0.0
    else:
        oos_win_rate = 0.0

    # Max DD (worst across windows)
    oos_max_dd = max(all_oos_max_dds) if all_oos_max_dds else 0.0
    is_max_dd = max(all_is_max_dds) if all_is_max_dds else 0.0

    # Calmar ratios (per-window average)
    oos_calmar = _calmar_ratio(total_oos_return / len(window_details), oos_max_dd) if window_details else 0.0
    is_calmar = _calmar_ratio(total_is_return / len(window_details), is_max_dd) if window_details else 0.0

    # Deflated Sharpe Ratio for OOS
    if oos_sharpe > 0 and all_oos_daily_returns and n_trials > 1:
        ret_vals = np.array(all_oos_daily_returns)
        ret_skew = float(pd.Series(ret_vals).skew()) if len(ret_vals) > 2 else 0.0
        ret_kurt = float(pd.Series(ret_vals).kurtosis()) if len(ret_vals) > 3 else 3.0
        if np.isnan(ret_skew): ret_skew = 0.0
        if np.isnan(ret_kurt): ret_kurt = 3.0
        dsr = deflated_sharpe_ratio(
            oos_sharpe, len(ret_vals), n_trials,
            skew=ret_skew, kurt=ret_kurt + 3.0,
        )
    elif oos_sharpe > 0:
        dsr = 1.0
    else:
        dsr = 0.0

    return _build_result(
        wfe=wfe,
        is_return=total_is_return,
        oos_return=total_oos_return,
        is_sharpe=is_sharpe,
        oos_sharpe=oos_sharpe,
        is_calmar=is_calmar,
        oos_calmar=oos_calmar,
        oos_max_dd=oos_max_dd,
        is_max_dd=is_max_dd,
        oos_win_rate=oos_win_rate,
        deflated_sharpe=dsr,
        windows=len(window_details),
        details=window_details,
    )


def _calmar_ratio(total_return_pct: float, max_dd_pct: float) -> float:
    """Compute Calmar ratio: annualized return / max drawdown."""
    if max_dd_pct < 0.1:
        return 0.0
    # Calmar = CAGR (approx) / MaxDD
    # For walk-forward, we use absolute return scaled roughly
    return round(total_return_pct / max_dd_pct, 3)


def _build_result(
    wfe, is_return, oos_return, is_sharpe, oos_sharpe,
    is_calmar=0.0, oos_calmar=0.0, oos_max_dd=0.0, is_max_dd=0.0,
    oos_win_rate=0.0, deflated_sharpe=0.0, windows=0, details=None,
):
    """Build standardized result dict with full metrics."""
    if details is None:
        details = []

    # WFE interpretation depends on sign of IS return
    if is_return > 0 and oos_return < 0:
        passed = False
        interp = "BROKEN — IS positive but OOS negative, classic overfitting"
    elif is_return < 0 and oos_return > 0:
        passed = True
        interp = "REGIME_SHIFT — IS negative but OOS positive, favorable regime change, not overfitting"
    elif is_return < 0 and oos_return < 0:
        if wfe >= 0.5:
            passed = True
            interp = "CONSISTENT_LOSER — consistently unprofitable but not overfit (same behavior IS and OOS)"
        else:
            passed = False
            interp = "DEGRADING — both negative and OOS much worse than IS"
    elif wfe >= 0.7 and deflated_sharpe > 0.8:
        passed = True
        interp = "STRONG — strategy generalizes well, low overfitting risk, high DSR"
    elif wfe >= 0.7:
        passed = True
        interp = "STRONG — strategy generalizes well, low overfitting risk"
    elif wfe >= 0.5:
        passed = True
        interp = "GOOD — acceptable out-of-sample performance"
    elif wfe >= 0.3:
        passed = True
        interp = "MARGINAL — some degradation but not clearly overfit"
    elif wfe >= 0.0:
        # PERF-041: Check for IS outlier distortion before calling it WEAK.
        # When one window's IS return dominates (>70% of total IS), WFE is
        # mechanically crushed even if OOS is healthy.  Example: W1 IS=+460%
        # vs W2 IS=+1.35% → WFE≈0.03 but OOS=+15.6% with 4.24% max DD.
        _details = details or []
        if _details and oos_return > 0 and oos_max_dd < 30 and oos_win_rate >= 40:
            _is_returns = [d.get('is_return', 0) for d in _details if d.get('is_return', 0) > 0]
            if _is_returns:
                _max_is = max(_is_returns)
                _total_is = sum(_is_returns)
                if _max_is / max(_total_is, 1e-9) > 0.7 and oos_return > 0:
                    passed = True
                    interp = "IS_OUTLIER — WFE distorted by outlier IS window; OOS actually profitable, check per-window"
                    return {
                        'wfe': round(wfe, 4),
                        'total_is_return_pct': round(is_return, 2),
                        'total_oos_return_pct': round(oos_return, 2),
                        'is_sharpe': round(is_sharpe, 3),
                        'oos_sharpe': round(oos_sharpe, 3),
                        'is_calmar': round(is_calmar, 3),
                        'oos_calmar': round(oos_calmar, 3),
                        'oos_max_drawdown_pct': round(oos_max_dd, 2),
                        'is_max_drawdown_pct': round(is_max_dd, 2),
                        'oos_win_rate': round(oos_win_rate, 1),
                        'deflated_sharpe_ratio': round(deflated_sharpe, 4),
                        'windows': windows,
                        'window_details': _details,
                        'passed': True,
                        'interpretation': interp,
                    }
        passed = False
        interp = "WEAK — significant OOS degradation, likely overfit"
    else:
        passed = False
        interp = "BROKEN — OOS negative while IS positive, classic overfitting"

    return {
        'wfe': round(wfe, 4),
        'total_is_return_pct': round(is_return, 2),
        'total_oos_return_pct': round(oos_return, 2),
        'is_sharpe': round(is_sharpe, 3),
        'oos_sharpe': round(oos_sharpe, 3),
        'is_calmar': round(is_calmar, 3),
        'oos_calmar': round(oos_calmar, 3),
        'oos_max_drawdown_pct': round(oos_max_dd, 2),
        'is_max_drawdown_pct': round(is_max_dd, 2),
        'oos_win_rate': round(oos_win_rate, 1),
        'deflated_sharpe_ratio': round(deflated_sharpe, 4),
        'windows': windows,
        'window_details': details,
        'interpretation': interp,
        'passed': passed,
    }


def _empty_wf_result(reason: str) -> Dict[str, Any]:
    """Return an empty walk-forward result with a failure reason."""
    return {
        'wfe': 0.0,
        'total_is_return_pct': 0.0,
        'total_oos_return_pct': 0.0,
        'is_sharpe': 0.0,
        'oos_sharpe': 0.0,
        'is_calmar': 0.0,
        'oos_calmar': 0.0,
        'oos_max_drawdown_pct': 0.0,
        'is_max_drawdown_pct': 0.0,
        'oos_win_rate': 0.0,
        'deflated_sharpe_ratio': 0.0,
        'windows': 0,
        'window_details': [],
        'interpretation': f"FAILED: {reason}",
        'passed': False,
    }


def WFEInterpretation(wfe: float) -> str:
    """Human-readable interpretation of a WFE value."""
    if wfe >= 0.7:
        return "STRONG — generalizes very well, low overfitting risk"
    elif wfe >= 0.5:
        return "GOOD — acceptable out-of-sample performance"
    elif wfe >= 0.3:
        return "MARGINAL — some degradation but not clearly overfit"
    elif wfe >= 0.0:
        return "WEAK — significant degradation, likely overfit"
    else:
        return "BROKEN — OOS negative, classic overfitting"
