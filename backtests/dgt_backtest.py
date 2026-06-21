"""Backtest Dynamic Grid Trading Strategy on historical Binance data.

Self-contained backtest that:
1. Fetches real historical klines
2. Simulates grid trading logic bar-by-bar
3. Reports PnL, Sharpe, drawdown, win rate, etc.
"""

import sys
import os
import json
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

# Path setup — point to project root
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from config.settings import get_config
from data.collector import BinanceDataCollector


class GridBacktest:
    """Simulate Dynamic Grid Trading on historical candle data."""

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "15m",
        grid_range_pct: float = 3.0,
        num_levels: int = 5,
        qty_per_level: float = 0.001,
        min_spread_pct: float = 0.2,
        leverage: int = 3,
        rebalance_hours: int = 4,
        stop_loss_pct: float = 5.0,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.grid_range_pct = grid_range_pct
        self.num_levels = num_levels
        self.qty_per_level = qty_per_level
        self.min_spread_pct = min_spread_pct
        self.leverage = leverage
        self.rebalance_bars = int(rebalance_hours * 60 / self._tf_minutes())
        self.stop_loss_pct = stop_loss_pct

        # State
        self.levels = []  # list of dict: {buy, sell, qty, filled, entry_idx}
        self.center = 0.0
        self.bars_since_rebalance = 0
        self.realized_pnls = []
        self.equity_curve = []
        self.trades = []  # completed buy-sell pairs

    def _tf_minutes(self) -> int:
        m = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        return m.get(self.timeframe, 15)

    def _build_grid(self, center_price: float):
        """Construct grid levels around center price."""
        half_range = self.grid_range_pct / 2.0
        step = half_range / self.num_levels  # spacing in pct

        self.center = center_price
        self.levels = []
        for i in range(self.num_levels):
            buy_offset = -(half_range - i * step)
            buy_px = center_price * (1 + buy_offset / 100)
            # Sell at buy + min_spread + half a step above
            sell_px = buy_px * (1 + (self.min_spread_pct + step * 0.8) / 100)
            self.levels.append({
                "buy": round(buy_px, 1),
                "sell": round(sell_px, 1),
                "qty": self.qty_per_level,
                "filled": False,
                "entry_idx": -1,
                "entry_price": 0.0,
                "stop_loss": buy_px * (1 - self.stop_loss_pct / 100),
            })
        self.bars_since_rebalance = 0

    def run(self, df: pd.DataFrame, initial_capital: float = 100.0) -> dict:
        """Run grid backtest over historical data.

        Args:
            df: OHLCV DataFrame with columns: open_time, open, high, low, close, volume
            initial_capital: Starting capital in USDT

        Returns:
            dict with performance metrics
        """
        if len(df) < 100:
            return {"error": f"Not enough data: {len(df)} bars"}

        # Initialize grid at first close
        self._build_grid(float(df["close"].iloc[0]))
        opened_positions = []  # track open buy-side positions {entry_price, qty, stop_loss, sell_target}
        self.realized_pnls = []
        self.trades = []
        self.equity_curve = []
        cumulative_pnl = 0.0

        for i in range(len(df)):
            bar = df.iloc[i]
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            open_ = float(bar["open"])

            # Track bars since rebalance
            self.bars_since_rebalance += 1

            # --- Check for fills on existing grid levels ---
            # We simulate with bar OHLC — if price crossed a level, it fills
            for lvl in self.levels:
                if lvl["filled"]:
                    # Already bought — check if sell target or stop hit this bar
                    pos = lvl
                    sell_target = pos["sell"]
                    stop_loss = pos["stop_loss"]

                    # Check if sell target hit
                    if high >= sell_target:
                        pnl = (sell_target - pos["entry_price"]) * pos["qty"]
                        self.realized_pnls.append(pnl)
                        self.trades.append({
                            "type": "win" if pnl > 0 else "loss",
                            "entry": pos["entry_price"],
                            "exit": sell_target,
                            "qty": pos["qty"],
                            "pnl": round(pnl, 4),
                            "pnl_pct": round((sell_target / pos["entry_price"] - 1) * 100, 3),
                            "exit_reason": "tp",
                        })
                        cumulative_pnl += pnl
                        pos["filled"] = False
                        pos["entry_idx"] = -1
                        pos["entry_price"] = 0.0

                    # Check if stop loss hit
                    elif low <= stop_loss:
                        pnl = (stop_loss - pos["entry_price"]) * pos["qty"]
                        self.realized_pnls.append(pnl)
                        self.trades.append({
                            "type": "loss",
                            "entry": pos["entry_price"],
                            "exit": stop_loss,
                            "qty": pos["qty"],
                            "pnl": round(pnl, 4),
                            "pnl_pct": round((stop_loss / pos["entry_price"] - 1) * 100, 3),
                            "exit_reason": "sl",
                        })
                        cumulative_pnl += pnl
                        pos["filled"] = False
                        pos["entry_idx"] = -1
                        pos["entry_price"] = 0.0

                else:
                    # Not filled yet — check if price dipped to buy level
                    if low <= lvl["buy"]:
                        lvl["filled"] = True
                        lvl["entry_idx"] = i
                        lvl["entry_price"] = lvl["buy"]
                        lvl["stop_loss"] = lvl["buy"] * (1 - self.stop_loss_pct / 100)

            # --- Rebalance grid periodically ---
            if self.bars_since_rebalance >= self.rebalance_bars:
                # Close all unfilled positions at market before rebalancing
                for lvl in self.levels:
                    if lvl["filled"]:
                        pnl = (close - lvl["entry_price"]) * lvl["qty"]
                        self.realized_pnls.append(pnl)
                        self.trades.append({
                            "type": "win" if pnl > 0 else "loss",
                            "entry": lvl["entry_price"],
                            "exit": close,
                            "qty": lvl["qty"],
                            "pnl": round(pnl, 4),
                            "pnl_pct": round((close / lvl["entry_price"] - 1) * 100, 3),
                            "exit_reason": "rebalance",
                        })
                        cumulative_pnl += pnl
                self._build_grid(close)

            # Track equity
            # Calculate unrealized PnL on open positions
            unrealized = 0.0
            for lvl in self.levels:
                if lvl["filled"]:
                    unrealized += (close - lvl["entry_price"]) * lvl["qty"]

            self.equity_curve.append({
                "idx": i,
                "time": bar["open_time"] if isinstance(bar["open_time"], (int, float)) else str(bar["open_time"]),
                "close": close,
                "realized": cumulative_pnl,
                "unrealized": unrealized,
                "equity": initial_capital + cumulative_pnl + unrealized,
            })

        # Close all remaining positions at last close
        last_close = float(df["close"].iloc[-1])
        for lvl in self.levels:
            if lvl["filled"]:
                pnl = (last_close - lvl["entry_price"]) * lvl["qty"]
                self.realized_pnls.append(pnl)
                self.trades.append({
                    "type": "win" if pnl > 0 else "loss",
                    "entry": lvl["entry_price"],
                    "exit": last_close,
                    "qty": lvl["qty"],
                    "pnl": round(pnl, 4),
                    "pnl_pct": round((last_close / lvl["entry_price"] - 1) * 100, 3),
                    "exit_reason": "eod",
                })
                cumulative_pnl += pnl

        return self._compute_metrics(df, initial_capital, cumulative_pnl)

    def _compute_metrics(self, df: pd.DataFrame, initial_capital: float, final_pnl: float) -> dict:
        """Compute performance metrics."""
        equity = [e["equity"] for e in self.equity_curve]
        returns = pd.Series(equity).pct_change().dropna()

        winning_trades = [t for t in self.trades if t["pnl"] > 0]
        losing_trades = [t for t in self.trades if t["pnl"] <= 0]

        # Sharpe ratio (annualized for 15m bars)
        if len(returns) > 1 and returns.std() > 0:
            # ~35040 bars per year for 15m
            periods_per_year = 365 * 24 * 60 / self._tf_minutes()
            sharpe = (returns.mean() / returns.std()) * np.sqrt(periods_per_year)
        else:
            sharpe = 0.0

        # Max drawdown
        equity_s = pd.Series(equity)
        rolling_max = equity_s.cummax()
        drawdown = (equity_s - rolling_max) / rolling_max * 100
        max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0

        # Win rate
        total_trades = len(self.trades)
        win_rate = len(winning_trades) / total_trades * 100 if total_trades > 0 else 0

        # Average win/loss
        avg_win = np.mean([t["pnl"] for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t["pnl"] for t in losing_trades]) if losing_trades else 0
        profit_factor = abs(sum(t["pnl"] for t in winning_trades) / sum(t["pnl"] for t in losing_trades)) if losing_trades and sum(t["pnl"] for t in losing_trades) != 0 else float("inf")

        # Grid efficiency
        total_pnl = sum(t["pnl"] for t in self.trades)
        total_volume = sum(t["qty"] * t["entry"] for t in self.trades)
        pnl_per_volume = total_pnl / total_volume * 100 if total_volume > 0 else 0

        # Exit reason breakdown
        exit_reasons = {}
        for t in self.trades:
            reason = t["exit_reason"]
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period": f"{len(df)} bars ({len(df) * self._tf_minutes() / 60 / 24:.1f} days)",
            "start_date": str(df["open_time"].iloc[0]) if "open_time" in df else "?",
            "end_date": str(df["open_time"].iloc[-1]) if "open_time" in df else "?",
            "params": {
                "grid_range_pct": self.grid_range_pct,
                "num_levels": self.num_levels,
                "qty_per_level": self.qty_per_level,
                "min_spread_pct": self.min_spread_pct,
                "leverage": self.leverage,
                "rebalance_hours": self.rebalance_bars * self._tf_minutes() / 60,
            },
            "performance": {
                "initial_capital": initial_capital,
                "final_equity": round(equity[-1], 2) if equity else initial_capital,
                "total_pnl": round(final_pnl, 4),
                "total_pnl_pct": round(final_pnl / initial_capital * 100, 2),
                "total_trades": total_trades,
                "winning_trades": len(winning_trades),
                "losing_trades": len(losing_trades),
                "win_rate": round(win_rate, 1),
                "avg_win": round(avg_win, 4),
                "avg_loss": round(avg_loss, 4),
                "profit_factor": round(profit_factor, 2),
                "sharpe_ratio": round(sharpe, 2),
                "max_drawdown_pct": round(max_dd, 2),
                "pnl_per_volume_pct": round(pnl_per_volume, 2),
                "exit_reasons": exit_reasons,
            },
            "grid_stats": {
                "avg_active_levels": 0,  # computed below
                "total_grid_rebuilds": 0,
            },
        }

    def print_report(self, metrics: dict):
        """Pretty-print backtest results."""
        p = metrics["performance"]
        print(f"\n{'='*60}")
        print(f"  Dynamic Grid Trading — Backtest Report")
        print(f"{'='*60}")
        print(f"  Symbol:      {metrics['symbol']}")
        print(f"  Timeframe:   {metrics['timeframe']}")
        print(f"  Period:      {metrics['period']}")
        print(f"  Range:       {metrics['start_date']} → {metrics['end_date']}")
        print(f"{'='*60}")
        print(f"  PARAMETERS")
        print(f"  Grid Range:  ±{metrics['params']['grid_range_pct']/2:.1f}%")
        print(f"  Levels:      {metrics['params']['num_levels']} buy + {metrics['params']['num_levels']} sell")
        print(f"  Qty/Level:   {metrics['params']['qty_per_level']:.4f} BTC")
        print(f"  Min Spread:  {metrics['params']['min_spread_pct']:.1f}%")
        print(f"  Leverage:    {metrics['params']['leverage']}x")
        print(f"  Rebalance:   every {metrics['params']['rebalance_hours']:.0f}h")
        print(f"{'='*60}")
        print(f"  PERFORMANCE")
        print(f"  Initial:     {p['initial_capital']:.2f} USDT")
        print(f"  Final:       {p['final_equity']:.2f} USDT")
        print(f"  PnL:         {p['total_pnl']:+.4f} USDT ({p['total_pnl_pct']:+.2f}%)")
        print(f"  Trades:      {p['total_trades']} total")
        print(f"  Win Rate:    {p['win_rate']:.1f}% ({p['winning_trades']}W / {p['losing_trades']}L)")
        print(f"  Avg Win:     {p['avg_win']:+.4f} USDT")
        print(f"  Avg Loss:    {p['avg_loss']:+.4f} USDT")
        print(f"  Profit Factor: {p['profit_factor']:.2f}")
        print(f"  Sharpe:      {p['sharpe_ratio']:.2f}")
        print(f"  Max DD:      {p['max_drawdown_pct']:.1f}%")
        print(f"  Exit Reasons: {p['exit_reasons']}")
        print(f"{'='*60}")

        # Verdict
        if p["total_pnl"] > 0 and p["sharpe_ratio"] > 0.5 and p["win_rate"] > 45:
            verdict = "✅ PROFITABLE — Deploy recommended"
        elif p["total_pnl"] > 0:
            verdict = "⚠️ MARGINAL — Needs parameter tuning"
        else:
            verdict = "❌ NOT PROFITABLE — Skip or rework"
        print(f"  VERDICT: {verdict}")
        print(f"{'='*60}\n")


def main():
    """Fetch live data and run backtest."""
    print("Loading config...")
    cfg = get_config()

    print("Creating collector...")
    collector = BinanceDataCollector(cfg.api_key, cfg.api_secret, cfg.testnet)

    # Fetch 7 days of 15m candles (~672 bars)
    print("Fetching BTC/USDT 15m data (7 days)...")
    df = collector.fetch_historical("BTC/USDT", "15m", days=7)
    print(f"Fetched {len(df)} candles")

    if len(df) < 100:
        print(f"ERROR: Only {len(df)} candles — need at least 100")
        return

    # Also test ETH
    print("Fetching ETH/USDT 15m data (7 days)...")
    df_eth = collector.fetch_historical("ETH/USDT", "15m", days=7)
    print(f"Fetched {len(df_eth)} ETH candles")

    results = {}

    # --- BTC backtest ---
    print("\n" + "="*60)
    print("  Running BTC/USDT Dynamic Grid Backtest...")
    print("="*60)

    bt = GridBacktest(
        symbol="BTC/USDT",
        timeframe="15m",
        grid_range_pct=3.0,
        num_levels=5,
        qty_per_level=0.001,
        min_spread_pct=0.2,
        leverage=3,
        rebalance_hours=4,
        stop_loss_pct=5.0,
    )
    btc_metrics = bt.run(df, initial_capital=100.0)
    bt.print_report(btc_metrics)
    results["BTC/USDT"] = btc_metrics

    # --- ETH backtest ---
    if len(df_eth) >= 100:
        print("\n" + "="*60)
        print("  Running ETH/USDT Dynamic Grid Backtest...")
        print("="*60)

        bt_eth = GridBacktest(
            symbol="ETH/USDT",
            timeframe="15m",
            grid_range_pct=4.0,  # ETH is more volatile
            num_levels=5,
            qty_per_level=0.01,  # ~$17 per level
            min_spread_pct=0.3,
            leverage=3,
            rebalance_hours=4,
            stop_loss_pct=6.0,
        )
        eth_metrics = bt_eth.run(df_eth, initial_capital=100.0)
        bt_eth.print_report(eth_metrics)
        results["ETH/USDT"] = eth_metrics

    # Save results
    os.makedirs(".aether", exist_ok=True)
    with open(".aether/dgt_backtest.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("Results saved to .aether/dgt_backtest.json")

    return results


if __name__ == "__main__":
    main()
