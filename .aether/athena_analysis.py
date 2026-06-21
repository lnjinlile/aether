#!/usr/bin/env python3
"""Athena: Aether Strategy Brain — backtest, evaluate, report."""

import json, os, sys, time
from datetime import datetime, timezone
import numpy as np
import pandas as pd

# Project root (script lives in .aether/; project is one level up)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from config.settings import get_config

# ── helpers ──────────────────────────────────────────────────────────

def run_strategy_backtest(name, strategy_cls, params, symbols, timeframes, storage, days=7):
    """Run backtest on a strategy using DB data. Returns per-symbol results."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000

    results = {}
    for sym in symbols:
        for tf in timeframes:
            df = storage.load_klines(sym, tf, start=start_ms, end=end_ms)
            if df.empty or len(df) < 30:
                results[f"{sym}/{tf}"] = {"error": "insufficient data", "n_bars": len(df)}
                continue

            df = df.set_index(pd.to_datetime(df['open_time'], unit='ms'))
            df = df[['open', 'high', 'low', 'close', 'volume']].copy()

            # Instantiate strategy with fresh state
            try:
                strat_params = {k: v for k, v in params.items()
                                if k not in ('symbols', 'timeframes')}
                strat = strategy_cls(name=name, symbols=[sym], timeframes=[tf], **strat_params)
            except Exception as e:
                results[f"{sym}/{tf}"] = {"error": f"init failed: {e}"}
                continue

            # Walk through bars, feed data, generate signals
            signals_list = []
            warmup = max(strat.params.get('ema_period', 100),
                         strat.params.get('slow_period', 25),
                         strat.params.get('rsi_period', 14)) + 10
            warmup = min(warmup, len(df) // 2)

            for i in range(len(df)):
                window = df.iloc[:i+1]
                strat.feed_data(sym, tf, window)
                sig = strat.generate_signal(sym)
                sig_type = sig.type.value

                if sig_type in ('LONG', 'SHORT'):
                    signals_list.append(1 if sig_type == 'LONG' else -1)
                elif sig_type in ('CLOSE_LONG', 'CLOSE_SHORT'):
                    signals_list.append(0)  # close = go flat
                else:
                    signals_list.append(0)

            signals = pd.Series(signals_list, index=df.index)

            # Run backtest
            engine = BacktestEngine(initial_capital=10000.0)
            bt = engine.run(df, signals)
            metrics = bt['metrics']
            metrics['n_bars'] = len(df)
            metrics['n_trades'] = len(bt.get('trade_log', []))
            results[f"{sym}/{tf}"] = metrics

    return results


def format_pct(v):
    if v is None:
        return "N/A"
    return f"{v:+.2f}%"

def format_sharpe(v):
    if v is None:
        return "N/A"
    return f"{v:.2f}"


# ── main ─────────────────────────────────────────────────────────────

def main():
    storage = MarketStorage()

    # ── 1. Check trades_log for real trading activity ──
    trade_history = storage.get_trade_history(limit=100)
    open_trades = storage.get_open_trades()

    # ── 2. Backtest all strategies ──
    from strategy.examples.trend_follow import TrendFollow
    from strategy.examples.ma_cross import MACrossoverStrategy
    from strategy.examples.rsi_mean_reversion import RSIMeanReversionStrategy

    strategies_config = {
        "TrendFollow": {
            "cls": TrendFollow,
            "params": {"ema_period": 100, "stop_loss_pct": 0.02, "take_profit_pct": 0.04, "cooldown_bars": 5},
            "symbols": ["BTC/USDT", "ETH/USDT"],
            "timeframes": ["15m"],
            "enabled": True,
        },
        "MA_Cross": {
            "cls": MACrossoverStrategy,
            "params": {"fast_period": 7, "slow_period": 25, "atr_period": 14, "atr_sl_mult": 2.0, "atr_tp_mult": 3.0, "cooldown_bars": 5},
            "symbols": ["BTC/USDT"],
            "timeframes": ["1h"],
            "enabled": False,
        },
        "RSI_MR": {
            "cls": RSIMeanReversionStrategy,
            "params": {"rsi_period": 14, "oversold": 30, "overbought": 70, "exit_rsi": 50, "stop_loss_pct": 0.03, "take_profit_pct": 0.06, "cooldown_bars": 5},
            "symbols": ["BTC/USDT"],
            "timeframes": ["1h"],
            "enabled": False,
        },
    }

    all_results = {}
    for name, cfg in strategies_config.items():
        bt = run_strategy_backtest(
            name, cfg["cls"], cfg["params"],
            cfg["symbols"], cfg["timeframes"], storage, days=7
        )
        all_results[name] = {
            "enabled": cfg["enabled"],
            "backtest": bt,
        }

    # ── 3. Make assessments ──
    assessments = {}
    warnings = []

    for name, result in all_results.items():
        bt = result["backtest"]
        if not bt:
            assessments[name] = {"status": "no_data", "recommendation": "insufficient data"}
            continue

        # Aggregate across symbols/timeframes
        returns = []
        sharpes = []
        win_rates = []
        trades = []
        for key, m in bt.items():
            if 'error' in m:
                continue
            returns.append(m.get('total_return_pct', 0))
            sharpes.append(m.get('sharpe_ratio', 0))
            win_rates.append(m.get('win_rate', 0))
            trades.append(m.get('n_trades', 0))

        if not returns:
            assessments[name] = {"status": "error", "recommendation": "all backtests failed"}
            continue

        avg_return = np.mean(returns)
        avg_sharpe = np.mean(sharpes)
        avg_win_rate = np.mean(win_rates)
        total_trades = sum(trades)

        status = "ok"
        if avg_win_rate < 30:
            status = "poor_win_rate"
            warnings.append(f"⚠️ {name}: 胜率 {avg_win_rate:.1f}% < 30% — 建议暂停")
        if avg_sharpe < 0:
            status = "negative_sharpe"
            warnings.append(f"⚠️ {name}: 夏普 {avg_sharpe:.2f} < 0 — 建议暂停")
        if avg_return < -5:
            status = "heavy_loss"
            warnings.append(f"🔴 {name}: 7日回测亏损 {avg_return:.1f}% — 强烈建议暂停")

        assessments[name] = {
            "status": status,
            "avg_return_pct": round(avg_return, 2),
            "avg_sharpe": round(avg_sharpe, 2),
            "avg_win_rate_pct": round(avg_win_rate, 1),
            "total_trades_7d": total_trades,
            "details": bt,
        }

    # ── 4. DB stats ──
    db_stats = storage.get_db_stats()

    # ── 5. Current price check ──
    btc_df = storage.load_klines("BTC/USDT", "15m", end=int(time.time()*1000))
    btc_price = float(btc_df['close'].iloc[-1]) if not btc_df.empty else None
    eth_df = storage.load_klines("ETH/USDT", "15m", end=int(time.time()*1000))
    eth_price = float(eth_df['close'].iloc[-1]) if not eth_df.empty else None

    # ── 6. Trade log summary ──
    closed_trades = [t for t in trade_history if t.get('status') == 'CLOSED']
    total_pnl = sum(t.get('pnl', 0) for t in closed_trades)

    # ── 7. Write athena.json ──
    athena = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market": {
            "btc_price": btc_price,
            "eth_price": eth_price,
        },
        "strategies": assessments,
        "trade_summary": {
            "open_positions": len(open_trades),
            "closed_trades_7d": len(closed_trades),
            "total_pnl": round(total_pnl, 4),
        },
        "warnings": warnings,
        "db_stats": db_stats,
    }

    os.makedirs(os.path.join(ROOT, ".aether"), exist_ok=True)
    with open(os.path.join(ROOT, ".aether", "athena.json"), "w") as f:
        json.dump(athena, f, indent=2, default=str)

    # ── 8. Build bulletin entry ──
    now_utc = datetime.now(timezone.utc).strftime("%m-%d %H:%M")
    lines = [f"\n---\n### {now_utc} — 🧠 Athena: 策略评估"]

    # Market snapshot
    if btc_price and eth_price:
        lines.append(f"\n**市场**: BTC={btc_price:,.1f} | ETH={eth_price:,.2f}")

    # Strategy assessments
    lines.append(f"\n**策略回测 (近7日)**:")
    for name, a in assessments.items():
        enabled_tag = "🟢" if all_results[name]["enabled"] else "⚫"
        if a["status"] in ("ok",):
            icon = "✅"
        elif a["status"] in ("poor_win_rate", "negative_sharpe"):
            icon = "⚠️"
        else:
            icon = "🔴"

        s = f"  {enabled_tag} {icon} **{name}**: "
        if "avg_return_pct" in a:
            s += f"收益 {a['avg_return_pct']:+.2f}% | 夏普 {a['avg_sharpe']:.2f} | 胜率 {a['avg_win_rate_pct']:.1f}% | {a['total_trades_7d']}笔"
        else:
            s += a.get("recommendation", a.get("status", "N/A"))
        lines.append(s)

    # Warnings
    if warnings:
        lines.append(f"\n**⚠️ 警告**:")
        for w in warnings:
            lines.append(f"  {w}")

    # Trade summary
    lines.append(f"\n**交易记录**: {len(open_trades)} 持仓 | {len(closed_trades)} 已平 | 总PnL: {total_pnl:+.4f} USDT")

    # Status line
    lines.append(f"\n**状态**: 🟢 正常")

    # Append to bulletin
    bulletin_path = os.path.join(ROOT, ".aether", "bulletin.md")
    with open(bulletin_path, "a") as f:
        f.write("\n".join(lines))

    # ── 9. Print summary for cron output ──
    print("\n".join(lines))


if __name__ == "__main__":
    main()
