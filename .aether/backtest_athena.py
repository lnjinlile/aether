#!/usr/bin/env python3
"""Athena backtest: evaluate all strategies in strategies.yaml against last 7 days of DB data."""

import json, math, sys, os
from datetime import datetime, timezone, timedelta
import sqlite3
import pandas as pd
import numpy as np

# ── Load strategies ──────────────────────────────────────────────
import yaml
with open("config/strategies.yaml") as f:
    cfg = yaml.safe_load(f)

strategies = cfg["strategies"]

# ── Load klines from DB ──────────────────────────────────────────
db = sqlite3.connect("data/market.db")
dfs = {}
for sym in ["BTC/USDT", "ETH/USDT"]:
    for tf in ["15m", "1h"]:
        df = pd.read_sql_query(
            "SELECT open_time, open, high, low, close, volume FROM klines "
            "WHERE symbol=? AND timeframe=? ORDER BY open_time",
            db, params=(sym, tf),
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        df.sort_index(inplace=True)
        dfs[(sym, tf)] = df
db.close()

# ── 7-day window ─────────────────────────────────────────────────
now_utc = datetime.now(timezone.utc)
cutoff = (now_utc - timedelta(days=7)).replace(tzinfo=None)
print(f"Athena Backtest — {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
print(f"Window: {cutoff.strftime('%Y-%m-%d %H:%M')} → {now_utc.strftime('%Y-%m-%d %H:%M')}")
print()

# ── Helpers ──────────────────────────────────────────────────────
def compute_rsi(close, period):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[avg_loss == 0] = 100.0
    rsi[avg_gain == 0] = 0.0
    return rsi

def compute_atr(df, period):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def sharpe(returns):
    """Annualized Sharpe from per-bar returns (assume 365*24 bars/yr for 1h)."""
    if len(returns) < 2:
        return 0.0
    mean = np.mean(returns)
    std = np.std(returns, ddof=1)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(365 * 24)  # hourly assumption, adjust for 15m

def max_drawdown(equity_curve):
    if len(equity_curve) < 2:
        return 0.0
    peak = np.maximum.accumulate(equity_curve)
    dd = (peak - equity_curve) / peak
    return float(np.max(dd) * 100)

# ── Backtest each strategy ───────────────────────────────────────
results = []

for s in strategies:
    name = s["name"]
    params = s["params"]
    sym = params["symbols"][0]
    tf = params["timeframes"][0]
    df = dfs.get((sym, tf))
    enabled = s.get("enabled", False)

    if df is None or len(df) < 100:
        print(f"⚠️  {name}: insufficient data ({len(df) if df is not None else 0} bars)")
        results.append({"name": name, "error": "insufficient data", "enabled": enabled})
        continue

    # Filter to 7-day window (plus lookback)
    warmup_cutoff = cutoff - timedelta(days=3)
    df = df[df.index >= warmup_cutoff]  # extra 3d warmup
    if len(df) < 50:
        print(f"⚠️  {name}: insufficient data in window ({len(df)} bars)")
        results.append({"name": name, "error": "insufficient data in window", "enabled": enabled})
        continue

    cls = s["class"]
    is_tf = "trend_follow" in cls
    is_rsi = "rsi" in cls
    is_ma = "ma_cross" in cls

    trades = []
    position = None  # {"side": "LONG"/"SHORT", "entry_price", "entry_idx", "sl", "tp"}
    bars_since_last = 999  # start ready
    equity = [1.0]
    trade_pnls_pct = []

    # ── TrendFollow ──
    if is_tf:
        ema_p = params["ema_period"]
        sl_p = params["stop_loss_pct"]
        tp_p = params["take_profit_pct"]
        cd = params["cooldown_bars"]

        ema = df["close"].ewm(span=ema_p, adjust=False).mean()
        slope = ema.diff(5)

        for i in range(120, len(df)):
            price = float(df["close"].iloc[i])
            uptrend = float(slope.iloc[i]) > 0
            bars_since_last += 1

            # Close position?
            if position:
                entry = position["entry_price"]
                side = position["side"]
                # Trend reversal
                if (side == "LONG" and not uptrend) or (side == "SHORT" and uptrend):
                    pnl = (price / entry - 1) if side == "LONG" else (1 - price / entry)
                    pnl *= 3  # leverage
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "trend_rev", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))  # 10% capital per trade
                    position = None
                    bars_since_last = 0
                elif side == "LONG" and price <= entry * (1 - sl_p):
                    pnl = -sl_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "SL", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None
                    bars_since_last = 0
                elif side == "LONG" and price >= entry * (1 + tp_p):
                    pnl = tp_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "TP", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None
                    bars_since_last = 0
                elif side == "SHORT" and price >= entry * (1 + sl_p):
                    pnl = -sl_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "SL", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None
                    bars_since_last = 0
                elif side == "SHORT" and price <= entry * (1 - tp_p):
                    pnl = tp_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "TP", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None
                    bars_since_last = 0

            # Open position?
            if not position and bars_since_last > cd:
                if uptrend:
                    position = {"side": "LONG", "entry_price": price, "entry_idx": i, "sl": price*(1-sl_p), "tp": price*(1+tp_p)}
                    bars_since_last = 0
                else:
                    position = {"side": "SHORT", "entry_price": price, "entry_idx": i, "sl": price*(1+sl_p), "tp": price*(1-tp_p)}
                    bars_since_last = 0

    # ── RSI Mean Reversion ──
    elif is_rsi:
        rsi_p = params["rsi_period"]
        oversold = params["oversold"]
        overbought = params["overbought"]
        exit_rsi = params["exit_rsi"]
        sl_p = params["stop_loss_pct"]
        tp_p = params["take_profit_pct"]
        cd = params["cooldown_bars"]

        rsi = compute_rsi(df["close"], rsi_p)
        cross_below_os = (rsi < oversold) & (rsi.shift(1) >= oversold)
        cross_above_ob = (rsi > overbought) & (rsi.shift(1) <= overbought)
        cross_above_exit = (rsi > exit_rsi) & (rsi.shift(1) <= exit_rsi)
        cross_below_exit = (rsi < exit_rsi) & (rsi.shift(1) >= exit_rsi)

        for i in range(50, len(df)):
            price = float(df["close"].iloc[i])
            bars_since_last += 1

            # Close position
            if position:
                entry = position["entry_price"]
                side = position["side"]
                closed = False
                if side == "LONG" and cross_above_exit.iloc[i]:
                    pnl = (price / entry - 1) * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "RSI_exit", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; closed = True; bars_since_last = 0
                elif side == "SHORT" and cross_below_exit.iloc[i]:
                    pnl = (1 - price / entry) * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "RSI_exit", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; closed = True; bars_since_last = 0
                elif side == "LONG" and price <= entry * (1 - sl_p):
                    pnl = -sl_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "SL", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; closed = True; bars_since_last = 0
                elif side == "LONG" and price >= entry * (1 + tp_p):
                    pnl = tp_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "TP", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; closed = True; bars_since_last = 0
                elif side == "SHORT" and price >= entry * (1 + sl_p):
                    pnl = -sl_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "SL", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; closed = True; bars_since_last = 0
                elif side == "SHORT" and price <= entry * (1 - tp_p):
                    pnl = tp_p * 3
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "TP", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; closed = True; bars_since_last = 0

            # Open position
            if not position and bars_since_last > cd:
                if cross_below_os.iloc[i]:
                    position = {"side": "LONG", "entry_price": price, "entry_idx": i}
                    bars_since_last = 0
                elif cross_above_ob.iloc[i]:
                    position = {"side": "SHORT", "entry_price": price, "entry_idx": i}
                    bars_since_last = 0

    # ── MA Cross ──
    elif is_ma:
        fp = params["fast_period"]
        sp = params["slow_period"]
        ap = params["atr_period"]
        sl_m = params["atr_sl_mult"]
        tp_m = params["atr_tp_mult"]
        cd = params["cooldown_bars"]

        fast_ema = df["close"].ewm(span=fp, adjust=False).mean()
        slow_ema = df["close"].ewm(span=sp, adjust=False).mean()
        atr = compute_atr(df, ap)
        cross_above = (fast_ema > slow_ema) & (fast_ema.shift(1) <= slow_ema.shift(1))
        cross_below = (fast_ema < slow_ema) & (fast_ema.shift(1) >= slow_ema.shift(1))

        for i in range(max(sp, ap)*2, len(df)):
            price = float(df["close"].iloc[i])
            atr_val = float(atr.iloc[i])
            bars_since_last += 1

            # Close position
            if position:
                side = position["side"]
                entry = position["entry_price"]
                if (side == "LONG" and cross_below.iloc[i]) or (side == "SHORT" and cross_above.iloc[i]):
                    pnl = (price / entry - 1) * 5 if side == "LONG" else (1 - price / entry) * 5
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "reverse_cross", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; bars_since_last = 0
                elif side == "LONG" and price <= entry - atr_val * sl_m:
                    pnl = (-atr_val * sl_m / entry) * 5
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "ATR_SL", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; bars_since_last = 0
                elif side == "LONG" and price >= entry + atr_val * tp_m:
                    pnl = (atr_val * tp_m / entry) * 5
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "ATR_TP", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; bars_since_last = 0
                elif side == "SHORT" and price >= entry + atr_val * sl_m:
                    pnl = (-atr_val * sl_m / entry) * 5
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "ATR_SL", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; bars_since_last = 0
                elif side == "SHORT" and price <= entry - atr_val * tp_m:
                    pnl = (atr_val * tp_m / entry) * 5
                    trades.append({"entry": position["entry_idx"], "exit": i, "pnl_pct": pnl*100, "reason": "ATR_TP", "side": side})
                    trade_pnls_pct.append(pnl)
                    equity.append(equity[-1] * (1 + pnl * 0.1))
                    position = None; bars_since_last = 0

            # Open position
            if not position and bars_since_last > cd:
                if cross_above.iloc[i]:
                    position = {"side": "LONG", "entry_price": price, "entry_idx": i}
                    bars_since_last = 0
                elif cross_below.iloc[i]:
                    position = {"side": "SHORT", "entry_price": price, "entry_idx": i}
                    bars_since_last = 0

    # ── Compute metrics ──
    n_trades = len(trades)
    if n_trades == 0:
        net_pct = 0.0
        winrate = 0.0
        sh = 0.0
        dd = 0.0
        avg_win = 0.0
        avg_loss = 0.0
    else:
        pnls = [t["pnl_pct"] for t in trades]
        # Net: compound return
        compound = 1.0
        for p in trade_pnls_pct:
            compound *= (1 + p * 0.1)  # same as equity calc
        net_pct = (compound - 1) * 100
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        winrate = len(wins) / n_trades * 100 if n_trades > 0 else 0
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0

        # Sharpe from trade returns (annualized assuming 1 trade/day on avg, rough)
        if len(trade_pnls_pct) >= 2:
            tr = np.array(trade_pnls_pct) / 100
            sh = (np.mean(tr) / np.std(tr, ddof=1)) * math.sqrt(365) if np.std(tr, ddof=1) > 0 else 0
        else:
            sh = 0.0

        # Max drawdown from equity curve
        eq = np.array(equity)
        peak = np.maximum.accumulate(eq)
        dd = float(np.max((peak - eq) / peak) * 100)

    result = {
        "name": name,
        "symbol": sym,
        "timeframe": tf,
        "enabled": enabled,
        "net_pct": round(net_pct, 2),
        "sharpe": round(sh, 2),
        "max_dd_pct": round(dd, 2),
        "winrate": round(winrate, 1),
        "trades": n_trades,
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "params": {k: v for k, v in params.items()},
    }
    results.append(result)

    status = "🟢" if net_pct > 0 and sh > 0 else ("🟡" if net_pct >= 0 else "🔴")
    print(f"{status} {name} ({sym} {tf}) | net={net_pct:+.2f}% sharpe={sh:+.2f} DD={dd:.1f}% WR={winrate:.0f}% trades={n_trades}")

# ── Write athena.json ────────────────────────────────────────────
output = {
    "run_time": now_utc.isoformat(),
    "window_days": 7,
    "results": results,
}

os.makedirs(".aether", exist_ok=True)
with open(".aether/athena.json", "w") as f:
    json.dump(output, f, indent=2, default=str)

print(f"\n✅ athena.json written — {len(results)} strategies evaluated")
