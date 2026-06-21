#!/usr/bin/env python3
"""Train an ML Alpha model and run a quick backtest.

Usage:
    cd /home/rinnen/binance_quant
    source venv/bin/activate
    python ml_alpha/train.py

Pulls 90+ days of BTC/USDT 1h data from BinanceDataCollector,
engineers features, trains a LightGBM model, prints metrics,
saves the model, and runs a simple walk-forward backtest on the
last 20% of data.
"""

import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from data.collector import BinanceDataCollector
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from ml_alpha.strategy import MLAlphaStrategy
from strategy.base import SignalType


def main():
    print("=" * 60)
    print("  ML Alpha Strategy — Training & Backtest")
    print("=" * 60)

    # ── 1. Fetch data ───────────────────────────────────────────
    print("\n[1/5] Fetching BTC/USDT 1h data (90 days)...")
    collector = BinanceDataCollector()
    df = collector.fetch_historical(
        symbol="BTC/USDT",
        timeframe="1h",
        days=90,
    )
    if df.empty:
        print("ERROR: No data fetched. Check API connectivity.")
        sys.exit(1)

    # Standardize columns
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    df.sort_index(inplace=True)
    print(f"      Fetched {len(df)} candles: {df.index[0]} → {df.index[-1]}")

    # ── 2. Build features ───────────────────────────────────────
    print("\n[2/5] Engineering features...")
    engineer = FeatureEngineer()
    X, y = engineer.build_features(df)
    print(f"      Feature matrix: {X.shape[0]} rows × {X.shape[1]} cols")
    print(f"      Class balance: UP={y.sum()} ({y.mean():.1%}), "
          f"DOWN={(~y.astype(bool)).sum()} ({(1 - y.mean()):.1%})")
    print(f"      Features: {list(X.columns)}")

    # ── 3. Train/val/test split (70/15/15) ────────────────────────
    n = len(X)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)
    X_train, y_train = X.iloc[:train_end], y.iloc[:train_end]
    X_val, y_val = X.iloc[train_end:val_end], y.iloc[train_end:val_end]
    X_test, y_test = X.iloc[val_end:], y.iloc[val_end:]
    print(f"\n[3/5] Split: {len(X_train)} train / {len(X_val)} val / {len(X_test)} test")

    # ── 4. Train model with early stopping ────────────────────────
    print("\n[4/5] Training LightGBM model (with regularization)...")
    model = AlphaModel()
    train_acc = model.train(X_train, y_train, X_val, y_val)
    test_acc = model.model.score(X_test, y_test)
    print(f"      Train accuracy: {train_acc:.4f}")
    print(f"      Val accuracy:   {model.model.score(X_val, y_val):.4f}")
    print(f"      Test accuracy:  {test_acc:.4f}")
    gap = train_acc - test_acc
    if gap > 0.15:
        print(f"      ⚠️  Overfitting gap: {gap:.1%} (train-test)")
    else:
        print(f"      Generalization gap: {gap:.1%} (acceptable)")

    print("\n      Top 10 Feature Importance:")
    for feat, score in model.get_feature_importance():
        print(f"        {feat:20s}  {score:.4f}")

    # ── 5. Sample predictions ──────────────────────────────────
    print("\n      Sample predictions (last 5 test bars):")
    probs = model.predict(X_test.iloc[-5:])
    signals = model.predict_signal(X_test.iloc[-5:])
    signal_labels = {1: "LONG", -1: "SHORT", 0: "HOLD"}
    for i, (p, s) in enumerate(zip(probs, signals)):
        print(f"        Row {i}: prob_up={p:.4f} → {signal_labels[s]}")

    # ── 6. Save model ──────────────────────────────────────────
    model_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "model.pkl"
    )
    model.save(model_path)
    print(f"\n      Model saved to: {model_path}")

    # ── 7. Quick backtest on test data ──────────────────────────
    print(f"\n[5/5] Backtest on last 20% of data ({len(X_test)} bars)...")

    # Feed data through the strategy for each bar
    strategy = MLAlphaStrategy(
        model_path=model_path,
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        confidence_threshold=0.55,
        name="MLAlpha_Backtest",
    )

    test_df = df.iloc[val_end:].copy()
    trades = []
    in_position = False
    position_side = None
    entry_price = 0.0

    # We need a minimum lookback for feature building — use last 200 bars
    # of training data for warmup
    warmup_df = df.iloc[max(0, val_end - 200):val_end].copy()

    for i in range(len(test_df)):
        # Build rolling window: warmup + test up to current bar
        if i == 0:
            window = warmup_df.copy()
        current_bar = test_df.iloc[[i]] if hasattr(test_df.iloc[[i]], 'index') else pd.DataFrame([test_df.iloc[i]]).T
        # Build up the window
        window = pd.concat([warmup_df, test_df.iloc[:i+1]])

        # Feed data to strategy
        strategy.feed_data("BTC/USDT", "1h", window)

        # Check for SL/TP on existing position
        current_price = float(test_df["close"].iloc[i])
        if in_position:
            sl = strategy.params.get("stop_loss_pct", 0.02)
            tp = strategy.params.get("take_profit_pct", 0.04)
            if position_side == "LONG":
                if current_price <= entry_price * (1 - sl):
                    trades[-1]["exit_price"] = current_price
                    trades[-1]["pnl_pct"] = -sl
                    trades[-1]["exit_reason"] = "SL"
                    in_position = False
                    continue
                elif current_price >= entry_price * (1 + tp):
                    trades[-1]["exit_price"] = current_price
                    trades[-1]["pnl_pct"] = tp
                    trades[-1]["exit_reason"] = "TP"
                    in_position = False
                    continue
            elif position_side == "SHORT":
                if current_price >= entry_price * (1 + sl):
                    trades[-1]["exit_price"] = current_price
                    trades[-1]["pnl_pct"] = -sl
                    trades[-1]["exit_reason"] = "SL"
                    in_position = False
                    continue
                elif current_price <= entry_price * (1 - tp):
                    trades[-1]["exit_price"] = current_price
                    trades[-1]["pnl_pct"] = tp
                    trades[-1]["exit_reason"] = "TP"
                    in_position = False
                    continue

        # Generate signal
        signal = strategy.generate_signal("BTC/USDT")

        if not in_position and signal.type in (SignalType.LONG, SignalType.SHORT):
            in_position = True
            position_side = "LONG" if signal.type == SignalType.LONG else "SHORT"
            entry_price = current_price
            trades.append({
                "entry_time": test_df.index[i],
                "side": position_side,
                "entry_price": entry_price,
                "exit_price": None,
                "pnl_pct": 0.0,
                "exit_reason": None,
            })
            strategy.on_order_filled("BTC/USDT", "buy" if position_side == "LONG" else "sell",
                                     entry_price, 0.001)

    # Force-close any open trade at last bar
    if in_position and trades:
        trades[-1]["exit_price"] = float(test_df["close"].iloc[-1])
        if position_side == "LONG":
            trades[-1]["pnl_pct"] = (trades[-1]["exit_price"] - entry_price) / entry_price
        else:
            trades[-1]["pnl_pct"] = (entry_price - trades[-1]["exit_price"]) / entry_price
        trades[-1]["exit_reason"] = "EOD"

    print(f"\n      Backtest Results:")
    print(f"        Total trades: {len(trades)}")
    if trades:
        wins = sum(1 for t in trades if t["pnl_pct"] > 0)
        win_rate = wins / len(trades) * 100
        net_return = sum(t["pnl_pct"] for t in trades) * 100
        print(f"        Win rate: {win_rate:.1f}% ({wins}/{len(trades)})")
        print(f"        Net return: {net_return:.2f}%")
        print(f"        Trade breakdown:")
        for t in trades:
            print(f"          {t['entry_time']}  {t['side']:5s}  "
                  f"entry={t['entry_price']:.2f}  exit={t['exit_price']:.2f}  "
                  f"pnl={t['pnl_pct']:.4%}  [{t['exit_reason']}]")
    else:
        print("        No trades generated.")

    print("\n" + "=" * 60)
    print("  Training & backtest complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
