"""Backtesting Engine with full metrics and walk-forward analysis.

Provides:
- Event-driven backtesting from OHLCV data + signal series
- Equity curve, Sharpe ratio, max drawdown, win rate, profit factor
- Walk-forward optimization
- Formatted reporting and equity curve plotting
"""

import logging
from math import sqrt, log
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import norm

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
# ------------------------------------------------------------------

def expected_max_sharpe(n_trials: int, T: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Expected maximum Sharpe ratio from N independent trials.

    Uses extreme value theory approximation for large N:
        E[max SR] ≈ sqrt(2 * log(N)) * sqrt(Var[SR])

    Adjusted by the distributional correction:
        Var[SR] = (1 - γ₃×SR + (γ₄-1)/4 × SR²) / (T - 1)

    Args:
        n_trials: Number of independent trials (parameter combinations tested)
        T: Number of return observations
        skew: Skewness of returns (default 0)
        kurt: Excess kurtosis of returns (default 3 for normal)

    Returns:
        Expected maximum annualized Sharpe ratio under the null (SR=0)
    """
    if n_trials <= 1:
        return 0.0
    # Var[SR] under the null (SR=0) simplifies to 1/(T-1)
    # E[max] = sqrt(2*log(N)) * sqrt(Var[SR])
    var_sr = 1.0 / max(1, T - 1)
    em = sqrt(2.0 * log(max(n_trials, 2))) * sqrt(var_sr)
    return em


def probabilistic_sharpe_ratio(
    sr: float,
    T: int,
    sr_benchmark: float = 0.0,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Probabilistic Sharpe Ratio — probability SR exceeds benchmark.

    PSR = Φ[ (ŜR - SR*) × sqrt(T-1) / sqrt(1 - γ₃×ŜR + (γ₄-1)/4 × ŜR²) ]

    Args:
        sr: Estimated annualized Sharpe ratio
        T: Number of return observations
        sr_benchmark: Benchmark Sharpe (default 0)
        skew: Skewness of returns
        kurt: Excess kurtosis (3 = normal)

    Returns:
        Probability (0-1) that true SR exceeds benchmark
    """
    if T < 2:
        return 0.0
    numerator = (sr - sr_benchmark) * sqrt(T - 1)
    # Variance of SR estimator under non-normality
    denom_factor = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom_factor <= 0:
        denom_factor = 1.0
    denominator = sqrt(denom_factor)
    z_score = numerator / denominator
    return float(norm.cdf(z_score))


def deflated_sharpe_ratio(
    sr: float,
    T: int,
    n_trials: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Deflated Sharpe Ratio — PSR with benchmark = E[max SR] over N trials.

    Corrects for multiple hypothesis testing: when you test N strategy
    variants, the best observed Sharpe is biased upward. DSR gives the
    probability that the observed SR exceeds the expected maximum purely
    from randomness.

    DSR > 0.95 ⇒ strategy is likely genuine (not a data-snooping artifact)
    DSR < 0.80 ⇒ strategy is likely overfit

    Args:
        sr: Estimated annualized Sharpe ratio
        T: Number of return observations
        n_trials: Number of independent trials tested
        skew: Skewness of returns
        kurt: Excess kurtosis (3 = normal)

    Returns:
        Deflated Sharpe Ratio (probability 0-1)
    """
    em = expected_max_sharpe(n_trials, T, skew, kurt)
    return probabilistic_sharpe_ratio(sr, T, sr_benchmark=em, skew=skew, kurt=kurt)


class BacktestEngine:
    """Event-driven backtesting engine.

    Takes OHLCV data and a signal series (1=LONG, -1=SHORT, 0=FLAT) and
    computes realistic P&L accounting for commissions on entry and exit.

    Attributes:
        initial_capital: Starting capital in quote currency
        commission: Fee rate applied on entry AND exit (default 0.0004 = 0.04%)
        slippage: Slippage as fraction of price (default 0.0001 = 0.01%)
    """

    def __init__(
        self,
        initial_capital: float = 10000.0,
        commission: float = 0.0004,
        slippage: float = 0.0001,
    ):
        self.initial_capital = initial_capital
        self.commission = commission
        self.slippage = slippage

    # ------------------------------------------------------------------
    # Core backtest
    # ------------------------------------------------------------------

    def run(
        self,
        df: pd.DataFrame,
        signals: pd.Series,
        leverage: int = 1,
        n_trials: int = 1,
    ) -> dict:
        """Run a backtest over OHLCV data with a signal series.

        Args:
            df: OHLCV DataFrame (must have 'open', 'high', 'low', 'close' columns,
                and be sorted by timestamp index)
            signals: Series aligned with df.index, values: 1 (LONG), -1 (SHORT), 0 (FLAT).
                NaN values are treated as 0 (FLAT).
            leverage: Leverage multiplier (default 1, no leverage)
            n_trials: Number of independent parameter combinations tested.
                Used for Deflated Sharpe Ratio computation (default 1 = no deflation).

        Returns:
            dict with keys:
                equity_curve: pd.Series of equity at each timestamp
                total_return_pct: Total return as percentage
                sharpe_ratio: Annualized Sharpe ratio
                deflated_sharpe_ratio: Deflated Sharpe Ratio (PSR vs E[max SR])
                max_drawdown_pct: Maximum drawdown as percentage
                win_rate: Fraction of winning trades
                profit_factor: Gross profit / gross loss
                total_trades: Number of completed trades
                trade_log: pd.DataFrame of individual trades
                metrics: dict with all computed metrics
        """
        # Validate inputs
        required_cols = {"open", "high", "low", "close"}
        if not required_cols.issubset(df.columns):
            missing = required_cols - set(df.columns)
            raise ValueError(f"DataFrame missing required columns: {missing}")

        if len(df) < 2:
            return self._empty_result()

        # Align signals
        sig = signals.reindex(df.index).fillna(0).astype(int)
        # Clamp to {-1, 0, 1}
        sig = sig.clip(-1, 1)

        # Simulate trading
        equity, trade_log = self._simulate(df, sig, leverage)

        # Compute metrics
        metrics = self._compute_metrics(equity, trade_log, n_trials)

        return {
            "equity_curve": equity,
            "total_return_pct": metrics["total_return_pct"],
            "sharpe_ratio": metrics["sharpe_ratio"],
            "deflated_sharpe_ratio": metrics["deflated_sharpe_ratio"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "total_trades": metrics["total_trades"],
            "trade_log": trade_log,
            "metrics": metrics,
        }

    def _simulate(
        self,
        df: pd.DataFrame,
        signals: pd.Series,
        leverage: int,
    ) -> Tuple[pd.Series, pd.DataFrame]:
        """Core simulation loop - vectorized where possible."""
        n = len(df)
        equity = pd.Series(self.initial_capital, index=df.index, dtype=float)
        position = 0  # 1=long, -1=short, 0=flat
        entry_price = 0.0
        entry_idx = 0
        trades = []

        for i in range(n):
            current_signal = int(signals.iloc[i])
            close_price = float(df["close"].iloc[i])

            # Check for exit conditions
            if position != 0 and current_signal != position:
                # Close position
                exit_price = close_price * (1.0 - self.slippage * position)
                if position == 1:
                    pnl_pct = (exit_price - entry_price) / entry_price * leverage
                else:  # position == -1
                    pnl_pct = (entry_price - exit_price) / entry_price * leverage

                # Commission on entry AND exit
                pnl_pct -= self.commission * 2

                prev_equity = equity.iloc[i - 1]
                equity.iloc[i] = prev_equity * (1.0 + pnl_pct)

                trades.append({
                    "entry_time": str(df.index[entry_idx]),
                    "exit_time": str(df.index[i]),
                    "direction": "LONG" if position == 1 else "SHORT",
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct * 100,
                    "equity_after": equity.iloc[i],
                })

                position = 0
                entry_price = 0.0
            else:
                equity.iloc[i] = equity.iloc[i - 1]

            # Check for entry conditions
            if position == 0 and current_signal != 0:
                position = current_signal
                entry_price = close_price * (1.0 + self.slippage * position)
                entry_idx = i

        # Close any open position at last bar
        if position != 0:
            exit_price = float(df["close"].iloc[-1]) * (1.0 - self.slippage * position)
            if position == 1:
                pnl_pct = (exit_price - entry_price) / entry_price * leverage
            else:
                pnl_pct = (entry_price - exit_price) / entry_price * leverage
            pnl_pct -= self.commission * 2

            equity.iloc[-1] = equity.iloc[-2] * (1.0 + pnl_pct)

            trades.append({
                "entry_time": str(df.index[entry_idx]),
                "exit_time": str(df.index[-1]),
                "direction": "LONG" if position == 1 else "SHORT",
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct * 100,
                "equity_after": equity.iloc[-1],
            })

        trade_log = pd.DataFrame(trades) if trades else pd.DataFrame(
            columns=["entry_time", "exit_time", "direction", "entry_price", "exit_price", "pnl_pct", "equity_after"]
        )

        return equity, trade_log

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, equity: pd.Series, trade_log: pd.DataFrame,
                         n_trials: int = 1) -> dict:
        """Compute all backtest metrics from equity curve and trade log."""
        if len(equity) < 2:
            return self._empty_metrics()

        # Returns
        daily_returns = equity.pct_change().dropna()
        # If equity didn't change at all, daily_returns would still exist but might be all 0
        if len(daily_returns) == 0:
            return self._empty_metrics()

        total_return = (equity.iloc[-1] / self.initial_capital - 1.0) * 100

        # Sharpe ratio (annualized, assuming 365 days)
        ret_mean = daily_returns.mean()
        ret_std = daily_returns.std()
        if ret_std > 1e-12:
            sharpe = (ret_mean / ret_std) * np.sqrt(365)
        else:
            sharpe = 0.0

        # Skewness and kurtosis for PSR/DSR
        ret_vals = daily_returns.values
        ret_skew = float(pd.Series(ret_vals).skew()) if len(ret_vals) > 2 else 0.0
        ret_kurt = float(pd.Series(ret_vals).kurtosis()) if len(ret_vals) > 3 else 3.0
        # Fix NaN from degenerate distributions
        if np.isnan(ret_skew): ret_skew = 0.0
        if np.isnan(ret_kurt): ret_kurt = 3.0

        # Deflated Sharpe Ratio
        if n_trials > 1 and sharpe > 0 and len(ret_vals) > 5:
            dsr = deflated_sharpe_ratio(
                sharpe, len(ret_vals), n_trials,
                skew=ret_skew, kurt=ret_kurt + 3.0,  # scipy uses excess kurtosis
            )
        else:
            dsr = 1.0 if sharpe > 0 else 0.0  # no deflation if single trial

        # Max drawdown
        peak = equity.expanding().max()
        drawdown = (equity - peak) / peak * 100
        max_dd = abs(drawdown.min())

        # Trade metrics
        total_trades = len(trade_log)
        if total_trades > 0:
            pnl_array = trade_log["pnl_pct"].values
            winning_trades = (pnl_array > 0).sum()
            win_rate = winning_trades / total_trades * 100

            gross_profit = pnl_array[pnl_array > 0].sum() if (pnl_array > 0).any() else 0
            gross_loss = abs(pnl_array[pnl_array < 0].sum()) if (pnl_array < 0).any() else 0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

            avg_win = pnl_array[pnl_array > 0].mean() if (pnl_array > 0).any() else 0
            avg_loss = abs(pnl_array[pnl_array < 0].mean()) if (pnl_array < 0).any() else 0
            best_trade = pnl_array.max()
            worst_trade = pnl_array.min()
        else:
            win_rate = 0.0
            profit_factor = 0.0
            avg_win = 0.0
            avg_loss = 0.0
            best_trade = 0.0
            worst_trade = 0.0

        return {
            "total_return_pct": round(total_return, 2),
            "sharpe_ratio": round(sharpe, 4),
            "deflated_sharpe_ratio": round(dsr, 4),
            "max_drawdown_pct": round(max_dd, 2),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 4),
            "total_trades": total_trades,
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "best_trade_pct": round(best_trade, 4),
            "worst_trade_pct": round(worst_trade, 4),
            "final_equity": round(equity.iloc[-1], 2),
            "return_skewness": round(ret_skew, 4),
            "return_kurtosis": round(ret_kurt, 4),
            "n_trials": n_trials,
        }

    def _empty_metrics(self) -> dict:
        return {
            "total_return_pct": 0.0,
            "sharpe_ratio": 0.0,
            "deflated_sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_trades": 0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
            "final_equity": self.initial_capital,
            "return_skewness": 0.0,
            "return_kurtosis": 3.0,
            "n_trials": 1,
        }

    def _empty_result(self) -> dict:
        return {
            "equity_curve": pd.Series(dtype=float),
            "total_return_pct": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_trades": 0,
            "trade_log": pd.DataFrame(),
            "metrics": self._empty_metrics(),
        }

    # ------------------------------------------------------------------
    # Walk-forward
    # ------------------------------------------------------------------

    def walk_forward(
        self,
        df: pd.DataFrame,
        signal_func,
        train_days: int = 60,
        test_days: int = 30,
        **signal_kwargs,
    ) -> List[dict]:
        """Walk-forward backtesting with rolling train/test windows.

        Args:
            df: OHLCV DataFrame with datetime index
            signal_func: Callable(train_df, **kwargs) -> pd.Series of signals
            train_days: Number of days for training window
            test_days: Number of days for test window
            **signal_kwargs: Passed to signal_func

        Returns:
            list of per-window result dicts from run()
        """
        results = []
        start = df.index.min()
        end = df.index.max()
        total_days = (end - start).days

        if total_days < train_days + test_days:
            # Single window
            signals = signal_func(df, **signal_kwargs)
            result = self.run(df, signals)
            results.append(result)
            return results

        window_start = start
        while window_start + pd.Timedelta(days=train_days + test_days) <= end:
            train_end = window_start + pd.Timedelta(days=train_days)
            test_end = train_end + pd.Timedelta(days=test_days)

            train_df = df.loc[window_start:train_end].copy()
            test_df = df.loc[train_end:test_end].copy()

            if len(train_df) < 10 or len(test_df) < 2:
                window_start = test_end
                continue

            # Generate signals using training data for parameter fitting
            # and test data for actual backtest
            signals = signal_func(train_df, test_df, **signal_kwargs)
            result = self.run(test_df, signals)
            result["train_window"] = (str(window_start), str(train_end))
            result["test_window"] = (str(train_end), str(test_end))
            results.append(result)

            window_start = test_end

        return results

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def generate_report(self, results: dict, title: str = "Backtest Report") -> str:
        """Generate a formatted text report from backtest results.

        Args:
            results: dict returned by run()
            title: Report title

        Returns:
            Formatted multi-line string report
        """
        m = results.get("metrics", {})
        report = [
            "=" * 60,
            f"  {title}",
            "=" * 60,
            "",
            f"  Initial Capital:    ${self.initial_capital:,.2f}",
            f"  Final Equity:       ${m.get('final_equity', 0):,.2f}",
            f"  Total Return:       {m.get('total_return_pct', 0):+.2f}%",
            f"  Sharpe Ratio:       {m.get('sharpe_ratio', 0):.4f}",
            f"  Deflated Sharpe:    {m.get('deflated_sharpe_ratio', 0):.4f}  (N={m.get('n_trials', 1)})",
            f"  Max Drawdown:       {m.get('max_drawdown_pct', 0):.2f}%",
            "",
            f"  Total Trades:       {m.get('total_trades', 0)}",
            f"  Win Rate:           {m.get('win_rate', 0):.2f}%",
            f"  Profit Factor:      {m.get('profit_factor', 0):.4f}",
            f"  Avg Win:            {m.get('avg_win_pct', 0):+.2f}%",
            f"  Avg Loss:           {m.get('avg_loss_pct', 0):+.2f}%",
            f"  Best Trade:         {m.get('best_trade_pct', 0):+.2f}%",
            f"  Worst Trade:        {m.get('worst_trade_pct', 0):+.2f}%",
            "",
            f"  Commission Rate:    {self.commission*100:.2f}% per side",
            f"  Slippage:           {self.slippage*100:.2f}%",
            "",
        ]

        # Trade log preview
        trade_log = results.get("trade_log")
        if trade_log is not None and not trade_log.empty:
            report.append("-" * 60)
            report.append("  Recent Trades:")
            report.append("-" * 60)
            recent = trade_log.tail(10)
            for _, t in recent.iterrows():
                report.append(
                    f"  {t['direction']:5s} | Entry: {t['entry_price']:,.2f} | "
                    f"Exit: {t['exit_price']:,.2f} | PnL: {t['pnl_pct']:+.2f}%"
                )

        report.append("=" * 60)
        return "\n".join(report)

    def print_report(self, results: dict, title: str = "Backtest Report"):
        """Print a formatted report to stdout."""
        print(self.generate_report(results, title))

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def plot_equity_curve(
        self,
        results: dict,
        save_path: Optional[str] = None,
        title: str = "Equity Curve",
    ):
        """Plot the equity curve. Saves to PNG if save_path is given.

        Falls back to ASCII art if matplotlib is unavailable.
        """
        equity = results.get("equity_curve")
        if equity is None or len(equity) < 2:
            print("No equity curve data to plot.")
            return

        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            fig, axes = plt.subplots(2, 1, figsize=(12, 8), gridspec_kw={"height_ratios": [3, 1]})

            # Equity curve
            ax1 = axes[0]
            ax1.plot(equity.index, equity.values, color="#1f77b4", linewidth=1.2, label="Equity")
            ax1.axhline(y=self.initial_capital, color="gray", linestyle="--", linewidth=0.8, label="Initial Capital")
            ax1.set_title(title, fontsize=14, fontweight="bold")
            ax1.set_ylabel("Equity ($)")
            ax1.legend(loc="upper left")
            ax1.grid(True, alpha=0.3)

            # Drawdown
            ax2 = axes[1]
            peak = equity.expanding().max()
            drawdown = (equity - peak) / peak * 100
            ax2.fill_between(drawdown.index, drawdown.values, 0, color="#d62728", alpha=0.3)
            ax2.plot(drawdown.index, drawdown.values, color="#d62728", linewidth=0.8)
            ax2.set_ylabel("Drawdown (%)")
            ax2.set_xlabel("Date")
            ax2.grid(True, alpha=0.3)

            # Format x-axis
            for ax in axes:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                for label in ax.get_xticklabels():
                    label.set_rotation(30)

            plt.tight_layout()

            if save_path:
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                plt.savefig(save_path, dpi=150, bbox_inches="tight")
                print(f"Equity curve saved to: {save_path}")
            else:
                plt.show()
            plt.close()
        except ImportError:
            self._plot_ascii(equity, title)

    def _plot_ascii(self, equity: pd.Series, title: str):
        """Fallback ASCII plot of equity curve."""
        if len(equity) < 2:
            return
        values = equity.values
        min_val, max_val = values.min(), values.max()
        if max_val == min_val:
            print(f"\n  {title}: flat at ${min_val:,.2f}\n")
            return

        height = 20
        width = 60
        step = max(1, len(values) // width)
        sampled = values[::step]
        x_scaled = np.linspace(0, width - 1, len(sampled), dtype=int)

        canvas = [[" "] * width for _ in range(height)]
        for x, v in zip(x_scaled, sampled):
            y = int((v - min_val) / (max_val - min_val) * (height - 1))
            canvas[height - 1 - y][x] = "*"

        print(f"\n  {title} (ASCII)")
        print(f"  ${min_val:,.2f} - ${max_val:,.2f}")
        for row in canvas:
            print("  |" + "".join(row) + "|")
        print(f"  ${min_val:,.2f}" + " " * (width - 8) + f"${max_val:,.2f}\n")
