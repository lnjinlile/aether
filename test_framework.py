"""Test script: run backtests on both strategies with synthetic data."""

import sys
import os

# Add project root to path
sys.path.insert(0, "/home/rinnen/binance_quant")

import numpy as np
import pandas as pd
from pathlib import Path

from backtest.engine import BacktestEngine
from strategy.base import SignalType
from strategy.examples.ma_cross import MACrossoverStrategy
from strategy.examples.rsi_mean_reversion import RSIMeanReversionStrategy


def generate_synthetic_data(days: int = 90, volatility: float = 0.015) -> pd.DataFrame:
    """Generate synthetic OHLCV data with trends and mean reversion.

    Combines a slow trend component with mean-reverting noise for
    more realistic price action that triggers both MA crosses and RSI extremes.
    Price range stays within ~15% of the starting price.
    """
    np.random.seed(42)
    n = days * 24 * 4  # 15-min candles, ~96 per day
    dt = 1.0 / (365 * 24 * 4)

    dates = pd.date_range(end=pd.Timestamp.now(), periods=n, freq="15min")

    # Generate price with trend + mean-reverting component
    t = np.arange(n)
    # Trend: gentle sinusoidal oscillation
    trend = 0.05 * np.sin(2 * np.pi * t / (15 * 24 * 4))  # 15-day cycle
    trend += 0.03 * np.sin(2 * np.pi * t / (45 * 24 * 4))  # 45-day cycle

    # Mean-reverting random component (Ornstein-Uhlenbeck)
    mr_component = np.zeros(n)
    theta = 0.02  # mean reversion speed
    for i in range(1, n):
        mr_component[i] = (1 - theta) * mr_component[i-1] + np.random.normal(0, volatility * np.sqrt(dt))

    # Scale and combine
    mr_component = mr_component / np.std(mr_component) * 0.03  # 3% std dev
    log_returns = np.diff(np.concatenate([[0], trend + mr_component]))
    price = 50000.0 * np.exp(np.cumsum(log_returns))

    # Generate OHLC
    o = price.copy()
    intraday_noise = np.random.normal(0, volatility * 0.2, n)
    c = o * np.exp(intraday_noise)
    h = np.maximum(o, c) * (1 + np.abs(np.random.normal(0, volatility * 0.1, n)))
    l = np.minimum(o, c) * (1 - np.abs(np.random.normal(0, volatility * 0.1, n)))
    v = np.random.lognormal(10, 1, n)

    df = pd.DataFrame({
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
    }, index=dates)

    return df


def strategy_to_signals(strategy, df: pd.DataFrame, symbol: str, timeframe: str) -> pd.Series:
    """Run a strategy over a DataFrame and produce a signal series."""
    strategy.feed_data(symbol, timeframe, df)

    signals = []
    positions = []  # track for closing

    for i in range(len(df)):
        # Feed a growing window to simulate real-time
        window = df.iloc[:i + 1].copy()
        strategy._data[(symbol, timeframe)] = window
        strategy._preprocess(symbol, timeframe, window)

        signal = strategy.generate_signal(symbol)
        sig_type = signal.type

        current_position = 0
        if positions:
            current_position = 1 if positions[-1] == "LONG" else (
                -1 if positions[-1] == "SHORT" else 0
            )

        if sig_type == SignalType.LONG:
            signals.append(1)
            positions.append("LONG")
        elif sig_type == SignalType.SHORT:
            signals.append(-1)
            positions.append("SHORT")
        elif sig_type in (SignalType.CLOSE_LONG, SignalType.CLOSE_SHORT):
            signals.append(0)
            positions.append("FLAT")
            # Reset strategy position tracking
            strategy._positions.pop(symbol, None)
        else:
            signals.append(current_position)
            positions.append(positions[-1] if positions else "FLAT")

    return pd.Series(signals, index=df.index)


def main():
    print("=" * 60)
    print("  STRATEGY & BACKTEST FRAMEWORK - TEST SUITE")
    print("=" * 60)

    # Generate synthetic data
    print("\n[1/4] Generating synthetic OHLCV data (180 days, 15-min candles)...")
    df = generate_synthetic_data(days=180, volatility=0.02)
    print(f"      Generated {len(df)} candles, date range: {df.index[0]} to {df.index[-1]}")
    print(f"      Price range: ${df['close'].min():,.2f} - ${df['close'].max():,.2f}")

    # Create backtest engine
    engine = BacktestEngine(
        initial_capital=10000.0,
        commission=0.0004,
        slippage=0.0001,
    )

    results_dir = Path("/home/rinnen/binance_quant/backtest/results")
    results_dir.mkdir(parents=True, exist_ok=True)

    # -- Test 1: MA Cross Strategy --
    print("\n[2/4] Testing MA Crossover Strategy...")
    ma_strat = MACrossoverStrategy(
        name="MA_Cross",
        symbols=["BTC/USDT"],
        timeframes=["15m"],
        fast_period=7,
        slow_period=25,
        atr_period=14,
        atr_sl_mult=2.0,
        atr_tp_mult=3.0,
        cooldown_bars=20,
    )

    ma_signals = strategy_to_signals(ma_strat, df, "BTC/USDT", "15m")
    long_signals = (ma_signals == 1).sum()
    short_signals = (ma_signals == -1).sum()
    print(f"      Signals: {long_signals} LONG, {short_signals} SHORT, "
          f"{(ma_signals == 0).sum()} FLAT")

    ma_results = engine.run(df, ma_signals, leverage=1)
    print()
    engine.print_report(ma_results, title="MA Crossover Strategy")

    # Save equity curve plot
    ma_plot_path = results_dir / "ma_cross_equity.png"
    engine.plot_equity_curve(ma_results, save_path=str(ma_plot_path),
                             title="MA Crossover Strategy - Equity Curve")

    # Save results summary
    ma_summary = {
        "strategy": "MA_Cross",
        **ma_results["metrics"],
    }
    pd.Series(ma_summary).to_csv(results_dir / "ma_cross_summary.csv", header=False)
    if not ma_results["trade_log"].empty:
        ma_results["trade_log"].to_csv(results_dir / "ma_cross_trades.csv", index=False)
    ma_results["equity_curve"].to_csv(results_dir / "ma_cross_equity.csv", header=True)

    # -- Test 2: RSI Mean Reversion Strategy --
    print("\n[3/4] Testing RSI Mean Reversion Strategy...")
    rsi_strat = RSIMeanReversionStrategy(
        name="RSI_MR",
        symbols=["BTC/USDT"],
        timeframes=["15m"],
        rsi_period=14,
        oversold=30.0,
        overbought=70.0,
        exit_rsi=50.0,
        stop_loss_pct=0.03,
        take_profit_pct=0.06,
    )

    rsi_signals = strategy_to_signals(rsi_strat, df, "BTC/USDT", "15m")
    long_signals = (rsi_signals == 1).sum()
    short_signals = (rsi_signals == -1).sum()
    print(f"      Signals: {long_signals} LONG, {short_signals} SHORT, "
          f"{(rsi_signals == 0).sum()} FLAT")

    rsi_results = engine.run(df, rsi_signals, leverage=1)
    print()
    engine.print_report(rsi_results, title="RSI Mean Reversion Strategy")

    # Save equity curve plot
    rsi_plot_path = results_dir / "rsi_mr_equity.png"
    engine.plot_equity_curve(rsi_results, save_path=str(rsi_plot_path),
                             title="RSI Mean Reversion Strategy - Equity Curve")

    # Save results
    rsi_summary = {
        "strategy": "RSI_MeanReversion",
        **rsi_results["metrics"],
    }
    pd.Series(rsi_summary).to_csv(results_dir / "rsi_mr_summary.csv", header=False)
    if not rsi_results["trade_log"].empty:
        rsi_results["trade_log"].to_csv(results_dir / "rsi_mr_trades.csv", index=False)
    rsi_results["equity_curve"].to_csv(results_dir / "rsi_mr_equity.csv", header=True)

    # -- Test 3: Strategy Manager --
    print("\n[4/4] Testing Strategy Manager...")
    from strategy.manager import StrategyManager

    mgr = StrategyManager()
    mgr.register(MACrossoverStrategy(name="MA_Cross"))
    mgr.register(RSIMeanReversionStrategy(name="RSI_MR"))

    mgr.on_kline_update("BTC/USDT", "15m", df.iloc[-20:].copy())
    pending = mgr.get_pending_signals()
    print(f"      Registered strategies: {mgr.get_active_strategies()}")
    print(f"      Pending signals: {len(pending)}")
    for sig in pending:
        print(f"        {sig}")

    # Comparison summary
    print("\n" + "=" * 60)
    print("  STRATEGY COMPARISON")
    print("=" * 60)
    print(f"\n  {'Metric':<25s} {'MA Cross':>15s} {'RSI MR':>15s}")
    print(f"  {'-'*25} {'-'*15} {'-'*15}")
    metrics_keys = [
        ("total_return_pct", "Total Return (%)"),
        ("sharpe_ratio", "Sharpe Ratio"),
        ("max_drawdown_pct", "Max Drawdown (%)"),
        ("win_rate", "Win Rate (%)"),
        ("profit_factor", "Profit Factor"),
        ("total_trades", "Total Trades"),
    ]
    for key, label in metrics_keys:
        ma_val = ma_results["metrics"][key]
        rsi_val = rsi_results["metrics"][key]
        if key == "total_trades":
            print(f"  {label:<25s} {ma_val:>15d} {rsi_val:>15d}")
        elif key == "profit_factor":
            print(f"  {label:<25s} {ma_val:>15.4f} {rsi_val:>15.4f}")
        else:
            print(f"  {label:<25s} {ma_val:>15.2f} {rsi_val:>15.2f}")

    print(f"\n  Results saved to: {results_dir}")
    print(f"  Files created:")
    for f in sorted(results_dir.glob("*")):
        print(f"    {f.name}")

    print("\n✅ All tests completed successfully!")


if __name__ == "__main__":
    main()
