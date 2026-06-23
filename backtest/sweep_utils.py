"""Shared utilities for backtest sweep scripts.

PERF-053: Consolidates 8 duplicate load_data() implementations across
sweep scripts into a single, documented utility. Also provides a shared
verdict() function for consistent LIVE/PAPER/NEEDS_IMPROVEMENT decisions.

Usage:
    from backtest.sweep_utils import load_data, SweepVerdict, verdict
"""

import pandas as pd
from dataclasses import dataclass
from data.storage import MarketStorage


def load_data(symbol: str, timeframe: str, lookback_days: int = 365,
              db_path: str = None, storage=None) -> pd.DataFrame | None:
    """Load OHLCV data for a symbol/timeframe, returning a DatetimeIndex-sorted DF.

    Args:
        symbol: e.g. 'BTC/USDT', 'ETH/USDT'
        timeframe: e.g. '1h', '15m', '4h'
        lookback_days: number of days of data to load (default: 365)
        db_path: path to market.db (default: autodetect, ignored if storage given)
        storage: pre-created MarketStorage instance (optional — avoids redundant init)

    Returns:
        pd.DataFrame with DatetimeIndex (sorted), or None if empty/unavailable.
    """
    if storage is None:
        if db_path is None:
            import os
            db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   'data', 'market.db')
        storage = MarketStorage(db_path)
    df = storage.load_klines(symbol, timeframe)
    if df is None or df.empty:
        return None

    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)

    cutoff = df.index[-1] - pd.Timedelta(days=lookback_days)
    df = df[df.index >= cutoff]
    return df


@dataclass
class SweepVerdict:
    """Standardized sweep verdict with reason."""
    verdict: str  # LIVE, PAPER_READY, NEEDS_IMPROVEMENT, DO_NOT_ENABLE, INSUFFICIENT
    reason: str


def verdict(net_pct: float, sharpe: float, max_dd: float,
            win_rate: float, trades: int,
            min_trades: int = 10,
            live_sr: float = 0.5,
            live_dd: float = 20.0,
            paper_sr: float = 0.3,
            paper_dd: float = 30.0,
            live_min_trades: int = 30,
            dsr: float | None = None,
            n_trials: int = 1) -> SweepVerdict:
    """Standardized strategy verdict based on backtest metrics.

    Criteria:
        LIVE:       SR >= live_sr, DD <= live_dd, WR >= 40%, trades >= live_min_trades
        PAPER_READY: SR >= paper_sr, DD <= paper_dd, WR >= 40%, trades >= min_trades
        NEEDS_IMPROVEMENT: SR > 0, DD <= paper_dd, WR >= 30%, trades >= min_trades
        DO_NOT_ENABLE: otherwise

    PERF-064: Now accepts optional DSR (Deflated Sharpe Ratio) and n_trials.
    When n_trials > 1, DSR < 0.80 downgrades LIVE→PAPER_READY with overfitting warning.

    Args:
        net_pct: total net return percentage
        sharpe: Sharpe ratio
        max_dd: maximum drawdown percentage (positive number, e.g. 20.0 = 20%)
        win_rate: win rate percentage (e.g. 65.0 = 65%)
        trades: total number of trades
        min_trades: minimum trades for any verdict (default: 10)
        live_sr: Sharpe threshold for LIVE (default: 0.5)
        live_dd: max DD threshold for LIVE (default: 20.0)
        paper_sr: Sharpe threshold for PAPER (default: 0.3)
        paper_dd: max DD threshold for PAPER (default: 30.0)
        live_min_trades: minimum trades for LIVE (default: 30)
        dsr: Deflated Sharpe Ratio (None if not computed)
        n_trials: number of parameter combinations tried (for DSR context)

    Returns:
        SweepVerdict with verdict and reason strings.
    """
    if trades < min_trades:
        return SweepVerdict("INSUFFICIENT",
                            f"仅{trades}笔交易 (需≥{min_trades})")

    if trades < 5:
        return SweepVerdict("INSUFFICIENT",
                            f"交易不足{trades}笔")

    # PERF-064: DSR overfitting detection
    overfit_warning = ""
    if dsr is not None and n_trials > 1 and dsr < 0.80:
        overfit_warning = f" ⚠️DSR={dsr:.3f}<0.80(N={n_trials} trials)—可能过拟合"

    # LIVE criteria
    if (sharpe >= live_sr and max_dd <= live_dd
            and win_rate >= 40 and trades >= live_min_trades):
        # DSR downgrade: if overfit, demote to PAPER_READY
        if dsr is not None and n_trials > 1 and dsr < 0.80:
            return SweepVerdict("PAPER_READY",
                                f"SR={sharpe:.2f} DD={max_dd:.1f}%但{overfit_warning.strip()}")
        return SweepVerdict("LIVE",
                            f"SR={sharpe:.2f} DD={max_dd:.1f}% WR={win_rate:.0f}% T={trades}{overfit_warning}")

    # PAPER_READY criteria
    if (sharpe >= paper_sr and max_dd <= paper_dd
            and win_rate >= 40 and trades >= min_trades):
        reason_parts = []
        if trades < live_min_trades:
            reason_parts.append(f"需≥{live_min_trades}笔交易(当前{trades})")
        if sharpe < live_sr:
            reason_parts.append(f"SR={sharpe:.2f}<{live_sr}")
        if max_dd > live_dd:
            reason_parts.append(f"DD={max_dd:.1f}%>{live_dd}%")
        if overfit_warning:
            reason_parts.append(overfit_warning.strip())
        return SweepVerdict("PAPER_READY", "; ".join(reason_parts))

    # NEEDS_IMPROVEMENT
    if (sharpe > 0 and max_dd <= paper_dd
            and win_rate >= 30 and trades >= min_trades):
        return SweepVerdict("NEEDS_IMPROVEMENT",
                            f"SR={sharpe:.2f} DD={max_dd:.1f}% WR={win_rate:.0f}% T={trades}")

    # DO_NOT_ENABLE
    fail_reasons = []
    if sharpe <= 0:
        fail_reasons.append(f"SR={sharpe:.2f}≤0")
    if max_dd > paper_dd:
        fail_reasons.append(f"DD={max_dd:.1f}%>{paper_dd}%")
    if win_rate < 30:
        fail_reasons.append(f"WR={win_rate:.0f}%<30%")
    return SweepVerdict("DO_NOT_ENABLE",
                        "; ".join(fail_reasons) if fail_reasons else "不满足PAPER标准")
