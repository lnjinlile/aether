#!/usr/bin/env python3
"""
Prometheus — Regime-Filtered Strategy Optimization
Adds ADX-based trending/ranging detection to MA_Cross and RSI_MR strategies.
Backtests on full 90d data range with Deflated Sharpe Ratio.
"""
import sys, os, json, yaml
from datetime import datetime, timezone
import numpy as np
import pandas as pd

sys.path.insert(0, '/home/rinnen/binance_quant')

from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine, deflated_sharpe_ratio

# ── Signal generators (inlined from athena_backtest) ──

def adx_values(high, low, close, period=14):
    """Compute ADX values."""
    n = len(close)
    tr = np.maximum(high - low, np.maximum(
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1))
    ))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).ewm(span=period, adjust=False).mean().values

    up_move = high - np.roll(high, 1)
    down_move = np.roll(low, 1) - low
    up_move[0] = 0
    down_move[0] = 0

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_di = 100 * pd.Series(plus_dm).ewm(span=period, adjust=False).mean().values / atr
    minus_di = 100 * pd.Series(minus_dm).ewm(span=period, adjust=False).mean().values / atr

    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)
    adx = pd.Series(dx).ewm(span=period, adjust=False).mean().values
    return adx, plus_di, minus_di


def ma_cross_signals_filtered(df, fast_period, slow_period, atr_period,
                               atr_sl_mult, atr_tp_mult, cooldown_bars,
                               adx_threshold=20):
    """MA Crossover with ADX trending filter."""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(close)

    fast_ema = pd.Series(close).ewm(span=fast_period, adjust=False).mean().values
    slow_ema = pd.Series(close).ewm(span=slow_period, adjust=False).mean().values

    # ATR
    high_low = high - low
    high_close = np.abs(high - np.roll(close, 1))
    low_close = np.abs(low - np.roll(close, 1))
    high_close[0] = low_close[0] = 0
    tr = np.maximum(np.maximum(high_low, high_close), low_close)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

    # ADX filter
    adx, plus_di, minus_di = adx_values(high, low, close, period=14)

    # Volume filter: avoid trading on low volume
    volume = df['volume'].values
    vol_ma = pd.Series(volume).rolling(20).mean().values

    cross_above = (fast_ema > slow_ema) & (np.roll(fast_ema, 1) <= np.roll(slow_ema, 1))
    cross_above[:1] = False
    cross_below = (fast_ema < slow_ema) & (np.roll(fast_ema, 1) >= np.roll(slow_ema, 1))
    cross_below[:1] = False

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    atr_entry = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = max(slow_period, atr_period) * 2

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        # EXIT
        if pos == 1:
            exit_trigger = False
            if cross_below[i]:
                exit_trigger = True
            elif atr_entry > 0 and price <= entry_price - atr_entry * atr_sl_mult:
                exit_trigger = True
            elif atr_entry > 0 and price >= entry_price + atr_entry * atr_tp_mult:
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        elif pos == -1:
            exit_trigger = False
            if cross_above[i]:
                exit_trigger = True
            elif atr_entry > 0 and price >= entry_price + atr_entry * atr_sl_mult:
                exit_trigger = True
            elif atr_entry > 0 and price <= entry_price - atr_entry * atr_tp_mult:
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        if pos != 0:
            signals[i] = pos

        # ENTRY — with ADX and volume filters
        if pos == 0 and bars_since_trade > cooldown_bars:
            is_trending = adx[i] > adx_threshold
            normal_volume = volume[i] > vol_ma[i] * 0.6  # don't trade on <60% avg vol
            if is_trending and normal_volume:
                if cross_above[i]:
                    pos = 1; entry_price = price; atr_entry = atr[i]
                    signals[i] = 1; bars_since_trade = 0
                elif cross_below[i]:
                    pos = -1; entry_price = price; atr_entry = atr[i]
                    signals[i] = -1; bars_since_trade = 0

    return pd.Series(signals, index=df.index)


def rsi_mr_signals_filtered(df, rsi_period, oversold, overbought, exit_rsi,
                             sl_pct, tp_pct, cooldown_bars,
                             adx_threshold=15):
    """RSI Mean Reversion with ADX filter (trade ranges, avoid trends)."""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(close)

    # RSI
    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/rsi_period, adjust=False).mean().values
    avg_loss = loss.ewm(alpha=1/rsi_period, adjust=False).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.inf), where=avg_loss != 0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[avg_loss == 0] = 100.0; rsi[avg_gain == 0] = 0.0

    # ADX filter
    adx, _, _ = adx_values(high, low, close, period=14)

    cross_below_os = (rsi < oversold) & (np.roll(rsi, 1) >= oversold)
    cross_below_os[:1] = False
    cross_above_ob = (rsi > overbought) & (np.roll(rsi, 1) <= overbought)
    cross_above_ob[:1] = False
    cross_above_ex = (rsi > exit_rsi) & (np.roll(rsi, 1) <= exit_rsi)
    cross_above_ex[:1] = False
    cross_below_ex = (rsi < exit_rsi) & (np.roll(rsi, 1) >= exit_rsi)
    cross_below_ex[:1] = False

    signals = np.zeros(n, dtype=int)
    pos = 0; entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = rsi_period * 3

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        is_ranging = adx[i] < adx_threshold

        if pos == 1:
            exit_trigger = False
            if cross_above_ex[i]: exit_trigger = True
            elif price <= entry_price * (1 - sl_pct): exit_trigger = True
            elif price >= entry_price * (1 + tp_pct): exit_trigger = True
            if exit_trigger: signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        elif pos == -1:
            exit_trigger = False
            if cross_below_ex[i]: exit_trigger = True
            elif price >= entry_price * (1 + sl_pct): exit_trigger = True
            elif price <= entry_price * (1 - tp_pct): exit_trigger = True
            if exit_trigger: signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        if pos != 0: signals[i] = pos

        if pos == 0 and bars_since_trade > cooldown_bars:
            if is_ranging:
                if cross_below_os[i]:
                    pos = 1; entry_price = price; signals[i] = 1; bars_since_trade = 0
                elif cross_above_ob[i]:
                    pos = -1; entry_price = price; signals[i] = -1; bars_since_trade = 0

    return pd.Series(signals, index=df.index)


def trendfollow_signals_filtered(df, ema_period, sl_pct, tp_pct, cooldown_bars,
                                  adx_threshold=25):
    """TrendFollow with ADX confirmation."""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(close)
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]

    adx, plus_di, minus_di = adx_values(high, low, close, period=14)

    signals = np.zeros(n, dtype=int)
    pos = 0; entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = max(ema_period * 2, 100)

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        uptrend = ema_slope[i] > 0
        strong_trend = adx[i] > adx_threshold

        if pos == 1:
            exit_trigger = False
            if not uptrend: exit_trigger = True
            elif price <= entry_price * (1 - sl_pct): exit_trigger = True
            elif price >= entry_price * (1 + tp_pct): exit_trigger = True
            if exit_trigger: signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        elif pos == -1:
            exit_trigger = False
            if uptrend: exit_trigger = True
            elif price >= entry_price * (1 + sl_pct): exit_trigger = True
            elif price <= entry_price * (1 - tp_pct): exit_trigger = True
            if exit_trigger: signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        if pos != 0: signals[i] = pos

        if pos == 0 and bars_since_trade > cooldown_bars and strong_trend:
            if uptrend:
                pos = 1; entry_price = price; signals[i] = 1; bars_since_trade = 0
            else:
                pos = -1; entry_price = price; signals[i] = -1; bars_since_trade = 0

    return pd.Series(signals, index=df.index)


# ══════════════════════════════════════════════════════════════════════

cfg = get_config()
storage = MarketStorage(cfg.db_path)
engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

print("🔥 Prometheus — Regime-Filtered Strategy Optimization")
print("=" * 70)
t0 = datetime.now(timezone.utc)
print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}")
print()

# Load data
data = {}
for sym in ['BTC/USDT', 'ETH/USDT']:
    for tf in ['15m', '1h']:
        df = storage.load_klines(sym, tf)
        if df.empty:
            continue
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('open_time', inplace=True)
        df.sort_index(inplace=True)
        data[(sym, tf)] = df
        days = (df.index[-1] - df.index[0]).days
        print(f"  📊 {sym:10s} {tf:4s}: {len(df):5d} bars, {days}d "
              f"[{df.index[0].strftime('%m/%d')} → {df.index[-1].strftime('%m/%d')}]")

# ══════════════════════════════════════════════════════════════════════
# 1. MA_Cross BTC 1h — UNFILTERED vs FILTERED comparison
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("1. MA_CROSS BTC 1h — UNFILTERED vs ADX-FILTERED vs ADX+VOL-FILTERED")
print("=" * 80)

df_btc_1h = data[('BTC/USDT', '1h')]
print(f"  Data: {len(df_btc_1h)} bars, {(df_btc_1h.index[-1]-df_btc_1h.index[0]).days}d, "
      f"last=${df_btc_1h.iloc[-1]['close']:,.0f}")

# Current config from strategies.yaml: fast=12, slow=26, atr=14, sl=2, tp=3, cd=5
current_params = {'fast': 12, 'slow': 26, 'atr': 14, 'sl': 2.0, 'tp': 3.0, 'cd': 5}

# Test: unfiltered baseline
from athena_backtest import ma_cross_signals as ma_cross_unfiltered

print(f"\n  ── UNFILTERED BASELINE ──")

mc_sweep = []
n_combos_mc = sum(1 for fp,sp in [(7,25),(5,20),(10,30),(12,26),(8,21),(5,13)]
                   for slm,tpm in [(2.0,3.0),(1.5,3.0),(2.0,4.0),(2.5,4.0),(2.0,5.0)])

for fp, sp in [(7, 25), (5, 20), (10, 30), (12, 26), (8, 21), (5, 13)]:
    for slm, tpm in [(2.0, 3.0), (1.5, 3.0), (2.0, 4.0), (2.5, 4.0), (2.0, 5.0)]:
        sig = ma_cross_unfiltered(df_btc_1h, fp, sp, 14, slm, tpm, 5)
        res = engine.run(df_btc_1h, sig, n_trials=n_combos_mc)
        m = res['metrics']
        mc_sweep.append({'fp':fp,'sp':sp,'slm':slm,'tpm':tpm,'filtered':False,
            'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
            'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
            'wr':m['win_rate'],'pf':m['profit_factor'],'trades':m['total_trades']})

# Test: ADX-filtered
print(f"  ── ADX-FILTERED (ADX>20) ──")

for fp, sp in [(7, 25), (5, 20), (10, 30), (12, 26), (8, 21), (5, 13)]:
    for slm, tpm in [(2.0, 3.0), (1.5, 3.0), (2.0, 4.0), (2.5, 4.0), (2.0, 5.0)]:
        sig = ma_cross_signals_filtered(df_btc_1h, fp, sp, 14, slm, tpm, 5, adx_threshold=20)
        res = engine.run(df_btc_1h, sig, n_trials=n_combos_mc)
        m = res['metrics']
        mc_sweep.append({'fp':fp,'sp':sp,'slm':slm,'tpm':tpm,'filtered':True,
            'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
            'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
            'wr':m['win_rate'],'pf':m['profit_factor'],'trades':m['total_trades']})

mc_sweep.sort(key=lambda x: (x['sharpe'] if x['trades']>=5 else -999, x['net']), reverse=True)

# Show top unfiltered
top_unfiltered = [r for r in mc_sweep if not r['filtered']][:5]
top_filtered = [r for r in mc_sweep if r['filtered']][:5]

print(f"\n  Top 5 UNFILTERED:")
print(f"  {'Fast':>4s} {'Slow':>4s} {'SLm':>5s} {'TPm':>5s} {'Net%':>8s} {'Shp':>7s} {'DSR':>7s} {'DD%':>6s} {'WR%':>5s} {'#T':>4s}")
for r in top_unfiltered:
    print(f"  {r['fp']:4d} {r['sp']:4d} {r['slm']:4.1f}x {r['tpm']:4.1f}x "
          f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dsr']:7.4f} {r['dd']:5.1f}% "
          f"{r['wr']:4.0f}% {r['trades']:4d}")

print(f"\n  Top 5 ADX-FILTERED:")
print(f"  {'Fast':>4s} {'Slow':>4s} {'SLm':>5s} {'TPm':>5s} {'Net%':>8s} {'Shp':>7s} {'DSR':>7s} {'DD%':>6s} {'WR%':>5s} {'#T':>4s}")
for r in top_filtered:
    print(f"  {r['fp']:4d} {r['sp']:4d} {r['slm']:4.1f}x {r['tpm']:4.1f}x "
          f"{r['net']:+8.2f}% {r['sharpe']:+7.2f} {r['dsr']:7.4f} {r['dd']:5.1f}% "
          f"{r['wr']:4.0f}% {r['trades']:4d}")

best_unfiltered = top_unfiltered[0]
best_filtered = top_filtered[0]

# ══════════════════════════════════════════════════════════════════════
# 2. MA_Cross ETH 1h
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("2. MA_CROSS ETH 1h — UNFILTERED vs ADX-FILTERED")
print("=" * 80)

df_eth_1h = data[('ETH/USDT', '1h')]
print(f"  Data: {len(df_eth_1h)} bars, {(df_eth_1h.index[-1]-df_eth_1h.index[0]).days}d, "
      f"last=${df_eth_1h.iloc[-1]['close']:,.0f}")

mc_eth_sweep = []
for fp, sp in [(5, 13), (7, 25), (5, 20), (10, 30), (12, 26), (8, 21)]:
    for slm, tpm in [(2.0, 3.0), (1.5, 3.0), (2.0, 4.0), (2.5, 4.0), (2.0, 5.0)]:
        # Unfiltered
        sig = ma_cross_unfiltered(df_eth_1h, fp, sp, 14, slm, tpm, 5)
        res = engine.run(df_eth_1h, sig, n_trials=n_combos_mc)
        m = res['metrics']
        mc_eth_sweep.append({'fp':fp,'sp':sp,'slm':slm,'tpm':tpm,'filtered':False,
            'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
            'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
            'wr':m['win_rate'],'pf':m['profit_factor'],'trades':m['total_trades']})
        # Filtered
        sig = ma_cross_signals_filtered(df_eth_1h, fp, sp, 14, slm, tpm, 5, adx_threshold=20)
        res = engine.run(df_eth_1h, sig, n_trials=n_combos_mc)
        m = res['metrics']
        mc_eth_sweep.append({'fp':fp,'sp':sp,'slm':slm,'tpm':tpm,'filtered':True,
            'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
            'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
            'wr':m['win_rate'],'pf':m['profit_factor'],'trades':m['total_trades']})

mc_eth_sweep.sort(key=lambda x: (x['sharpe'] if x['trades']>=4 else -999, x['net']), reverse=True)

top_eth_unfiltered = [r for r in mc_eth_sweep if not r['filtered']][:5]
top_eth_filtered = [r for r in mc_eth_sweep if r['filtered']][:5]

print(f"\n  Top 5 UNFILTERED ETH:")
for r in top_eth_unfiltered:
    print(f"  f={r['fp']} s={r['sp']} sl={r['slm']}x tp={r['tpm']}x "
          f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} dsr={r['dsr']:.4f} "
          f"dd={r['dd']:.1f}% wr={r['wr']:.0f}% #T={r['trades']}")

print(f"\n  Top 5 ADX-FILTERED ETH:")
for r in top_eth_filtered:
    print(f"  f={r['fp']} s={r['sp']} sl={r['slm']}x tp={r['tpm']}x "
          f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} dsr={r['dsr']:.4f} "
          f"dd={r['dd']:.1f}% wr={r['wr']:.0f}% #T={r['trades']}")

# ══════════════════════════════════════════════════════════════════════
# 3. RSI_MR with ADX range filter (BTC 1h)
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("3. RSI_MR BTC 1h — UNFILTERED vs RANGING-FILTERED (ADX<15)")
print("=" * 80)

from athena_backtest import rsi_mr_signals as rsi_mr_unfiltered

rsi_sweep = []
n_rsi = 2 * 3 * 4  # 24
for rsi_p in [7, 14]:
    for os_l, ob_l in [(30, 70), (25, 75), (35, 65)]:
        for sl, tp in [(0.02, 0.04), (0.02, 0.06), (0.03, 0.06), (0.03, 0.08)]:
            # Unfiltered
            sig = rsi_mr_unfiltered(df_btc_1h, rsi_p, os_l, ob_l, 50, sl, tp, 5)
            res = engine.run(df_btc_1h, sig, n_trials=n_rsi * 2)
            m = res['metrics']
            rsi_sweep.append({'rsi_p':rsi_p,'os':os_l,'ob':ob_l,'sl':sl,'tp':tp,'filtered':False,
                'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
                'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
                'wr':m['win_rate'],'trades':m['total_trades']})
            # Filtered (only trade ranges)
            sig = rsi_mr_signals_filtered(df_btc_1h, rsi_p, os_l, ob_l, 50, sl, tp, 5, adx_threshold=15)
            res = engine.run(df_btc_1h, sig, n_trials=n_rsi * 2)
            m = res['metrics']
            rsi_sweep.append({'rsi_p':rsi_p,'os':os_l,'ob':ob_l,'sl':sl,'tp':tp,'filtered':True,
                'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
                'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
                'wr':m['win_rate'],'trades':m['total_trades']})

rsi_sweep.sort(key=lambda x: (x['sharpe'] if x['trades']>=3 else -999, x['net']), reverse=True)

top_rsi_unfiltered = [r for r in rsi_sweep if not r['filtered']][:5]
top_rsi_filtered = [r for r in rsi_sweep if r['filtered']][:5]

print(f"\n  Top 5 UNFILTERED RSI_MR:")
for r in top_rsi_unfiltered:
    print(f"  RSI{r['rsi_p']} OS{r['os']} OB{r['ob']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% "
          f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} dsr={r['dsr']:.4f} "
          f"wr={r['wr']:.0f}% #T={r['trades']}")

print(f"\n  Top 5 RANGING-FILTERED RSI_MR:")
for r in top_rsi_filtered:
    print(f"  RSI{r['rsi_p']} OS{r['os']} OB{r['ob']} SL={r['sl']*100:.1f}% TP={r['tp']*100:.1f}% "
          f"net={r['net']:+.2f}% shp={r['sharpe']:+.2f} dsr={r['dsr']:.4f} "
          f"wr={r['wr']:.0f}% #T={r['trades']}")

# ══════════════════════════════════════════════════════════════════════
# 4. TrendFollow with ADX filter
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("4. TRENDFOLLOW BTC 1h — ADX-FILTERED quick sweep")
print("=" * 80)

tf_adx_sweep = []
n_tf = sum(1 for ema in [20,30,50,75,100] for sl in [0.01,0.015,0.02,0.03] for tp in [0.02,0.03,0.04,0.05] if tp > sl)
for ema in [20, 30, 50, 75, 100]:
    for sl in [0.01, 0.015, 0.02, 0.03]:
        for tp in [0.02, 0.03, 0.04, 0.05]:
            if tp <= sl: continue
            sig = trendfollow_signals_filtered(df_btc_1h, ema, sl, tp, 8, adx_threshold=25)
            res = engine.run(df_btc_1h, sig, n_trials=n_tf)
            m = res['metrics']
            tf_adx_sweep.append({'ema':ema,'sl':sl,'tp':tp,
                'net':m['total_return_pct'],'sharpe':m['sharpe_ratio'],
                'dsr':m['deflated_sharpe_ratio'],'dd':m['max_drawdown_pct'],
                'wr':m['win_rate'],'trades':m['total_trades']})

tf_adx_sweep.sort(key=lambda x: (x['sharpe'] if x['trades']>=3 else -999, x['net']), reverse=True)
print(f"\n  Top 10 ADX-FILTERED TrendFollow:")
for r in tf_adx_sweep[:10]:
    print(f"  EMA{r['ema']:4d} SL={r['sl']*100:4.1f}% TP={r['tp']*100:4.1f}% "
          f"net={r['net']:+7.2f}% shp={r['sharpe']:+6.2f} dsr={r['dsr']:.4f} "
          f"dd={r['dd']:5.1f}% wr={r['wr']:4.0f}% #T={r['trades']}")


# ══════════════════════════════════════════════════════════════════════
# SUMMARY & RECOMMENDATIONS
# ══════════════════════════════════════════════════════════════════════

print("\n" + "=" * 80)
print("PROMETHEUS RECOMMENDATIONS")
print("=" * 80)

actions = []

# Compare MA_Cross filtered vs unfiltered
print(f"\n📊 MA_Cross_BTC (1h) — Regime Filter Impact:")
print(f"   UNFILTERED best: f={best_unfiltered['fp']} s={best_unfiltered['sp']} "
      f"sl={best_unfiltered['slm']}x tp={best_unfiltered['tpm']}x "
      f"net={best_unfiltered['net']:+.2f}% shp={best_unfiltered['sharpe']:+.2f} "
      f"dsr={best_unfiltered['dsr']:.4f} #T={best_unfiltered['trades']}")
print(f"   FILTERED best:   f={best_filtered['fp']} s={best_filtered['sp']} "
      f"sl={best_filtered['slm']}x tp={best_filtered['tpm']}x "
      f"net={best_filtered['net']:+.2f}% shp={best_filtered['sharpe']:+.2f} "
      f"dsr={best_filtered['dsr']:.4f} #T={best_filtered['trades']}")

# Decide which version wins
if best_filtered['sharpe'] > best_unfiltered['sharpe'] and best_filtered['dsr'] > 0.5:
    print(f"   ✅ ADX-FILTERED version is superior (sharpe={best_filtered['sharpe']:+.2f} vs {best_unfiltered['sharpe']:+.2f})")
    actions.append({
        'strategy': 'MA_Cross_BTC',
        'action': 'ADD_ADX_FILTER',
        'params': f"fast={best_filtered['fp']} slow={best_filtered['sp']} sl={best_filtered['slm']}x tp={best_filtered['tpm']}x adx_threshold=20",
        'improvement': f"sharpe {best_unfiltered['sharpe']:+.2f}→{best_filtered['sharpe']:+.2f}, dsr {best_unfiltered['dsr']:.4f}→{best_filtered['dsr']:.4f}"
    })
elif best_unfiltered['sharpe'] > best_filtered['sharpe']:
    print(f"   ℹ️  ADX filter reduces trades too much — stick with unfiltered")
    # But recommend better unfiltered params if different from current
    current_best = best_unfiltered
    if (current_best['fp'] != current_params['fast'] or current_best['sp'] != current_params['slow'] or
        current_best['slm'] != current_params['sl'] or current_best['tpm'] != current_params['tp']):
        print(f"   ✅ PARAM UPDATE: f={current_best['fp']} s={current_best['sp']} sl={current_best['slm']}x tp={current_best['tpm']}x")
        actions.append({
            'strategy': 'MA_Cross_BTC',
            'action': 'UPDATE_PARAMS',
            'params': f"fast={current_best['fp']} slow={current_best['sp']} sl={current_best['slm']}x tp={current_best['tpm']}x",
            'improvement': f"net={current_best['net']:+.2f}% shp={current_best['sharpe']:+.2f}"
        })

# ETH recommendations
if top_eth_filtered and top_eth_unfiltered:
    eth_best_f = top_eth_filtered[0]
    eth_best_uf = top_eth_unfiltered[0]
    print(f"\n📊 MA_Cross_ETH (1h) — Regime Filter Impact:")
    print(f"   UNFILTERED best: f={eth_best_uf['fp']} s={eth_best_uf['sp']} "
          f"net={eth_best_uf['net']:+.2f}% shp={eth_best_uf['sharpe']:+.2f} dsr={eth_best_uf['dsr']:.4f}")
    print(f"   FILTERED best:   f={eth_best_f['fp']} s={eth_best_f['sp']} "
          f"net={eth_best_f['net']:+.2f}% shp={eth_best_f['sharpe']:+.2f} dsr={eth_best_f['dsr']:.4f}")

    if eth_best_f['sharpe'] > eth_best_uf['sharpe'] and eth_best_f['dsr'] > 0.5:
        print(f"   ✅ ADX-FILTERED wins for ETH")
        actions.append({
            'strategy': 'MA_Cross_ETH',
            'action': 'ADD_ADX_FILTER',
            'params': f"fast={eth_best_f['fp']} slow={eth_best_f['sp']} sl={eth_best_f['slm']}x tp={eth_best_f['tpm']}x adx_threshold=20",
            'improvement': f"sharpe {eth_best_uf['sharpe']:+.2f}→{eth_best_f['sharpe']:+.2f}"
        })

# RSI_MR recommendations
if top_rsi_filtered and top_rsi_unfiltered:
    rsi_best_f = top_rsi_filtered[0]
    rsi_best_uf = top_rsi_unfiltered[0]
    print(f"\n📊 RSI_MR (BTC 1h) — Regime Filter Impact:")
    print(f"   UNFILTERED best: RSI{rsi_best_uf['rsi_p']} OS{rsi_best_uf['os']} OB{rsi_best_uf['ob']} "
          f"net={rsi_best_uf['net']:+.2f}% shp={rsi_best_uf['sharpe']:+.2f} dsr={rsi_best_uf['dsr']:.4f} #T={rsi_best_uf['trades']}")
    print(f"   FILTERED best:   RSI{rsi_best_f['rsi_p']} OS{rsi_best_f['os']} OB{rsi_best_f['ob']} "
          f"net={rsi_best_f['net']:+.2f}% shp={rsi_best_f['sharpe']:+.2f} dsr={rsi_best_f['dsr']:.4f} #T={rsi_best_f['trades']}")

    if rsi_best_f['sharpe'] > 0.5 and rsi_best_f['trades'] >= 3 and rsi_best_uf['sharpe'] <= rsi_best_f['sharpe']:
        print(f"   ✅ RANGING-FILTER RSI_MR viable — recommend ENABLE")
        actions.append({
            'strategy': 'RSI_MR',
            'action': 'ENABLE_WITH_RANGE_FILTER',
            'params': f"rsi_p={rsi_best_f['rsi_p']} os={rsi_best_f['os']} ob={rsi_best_f['ob']} sl={rsi_best_f['sl']*100:.1f}% tp={rsi_best_f['tp']*100:.1f}%",
            'improvement': f"net={rsi_best_f['net']:+.2f}% shp={rsi_best_f['sharpe']:+.2f} dsr={rsi_best_f['dsr']:.4f}"
        })

# TrendFollow ADX
if tf_adx_sweep:
    best_tf_adx = tf_adx_sweep[0]
    print(f"\n📊 TrendFollow BTC 1h — ADX-FILTERED:")
    print(f"   Best: EMA={best_tf_adx['ema']} SL={best_tf_adx['sl']*100:.1f}% TP={best_tf_adx['tp']*100:.1f}% "
          f"net={best_tf_adx['net']:+.2f}% shp={best_tf_adx['sharpe']:+.2f} dsr={best_tf_adx['dsr']:.4f} #T={best_tf_adx['trades']}")
    if best_tf_adx['sharpe'] > 0.8 and best_tf_adx['trades'] >= 4:
        print(f"   ✅ Viable with ADX filter — consider re-enabling")

# ══════════════════════════════════════════════════════════════════════
# SAVE RESULTS
# ══════════════════════════════════════════════════════════════════════

os.makedirs('.aether', exist_ok=True)

prom_data = {
    'run_time': t0.isoformat(),
    'run_type': 'regime_filtered_optimization',
    'version': '2.0.0',
    'data': {
        'btc_1h_bars': len(df_btc_1h),
        'btc_1h_days': (df_btc_1h.index[-1] - df_btc_1h.index[0]).days,
        'eth_1h_bars': len(df_eth_1h) if df_eth_1h is not None else 0,
    },
    'findings': {
        'ma_cross_btc': {
            'unfiltered_best': {'fp': best_unfiltered['fp'], 'sp': best_unfiltered['sp'],
                'net': best_unfiltered['net'], 'sharpe': best_unfiltered['sharpe'],
                'dsr': best_unfiltered['dsr'], 'trades': best_unfiltered['trades']},
            'filtered_best': {'fp': best_filtered['fp'], 'sp': best_filtered['sp'],
                'net': best_filtered['net'], 'sharpe': best_filtered['sharpe'],
                'dsr': best_filtered['dsr'], 'trades': best_filtered['trades']},
        },
    },
    'actions': actions,
    'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
}

with open('.aether/prometheus.json', 'w') as f:
    json.dump(prom_data, f, indent=2, default=str)

print(f"\n💾 prometheus.json updated with {len(actions)} actions")

# ══════════════════════════════════════════════════════════════════════
# APPLY WINNING CONFIG UPDATES to strategies.yaml
# ══════════════════════════════════════════════════════════════════════

updates_applied = []
if actions:
    with open('config/strategies.yaml') as f:
        config = yaml.safe_load(f)

    for action in actions:
        strat_name = action['strategy']
        act_type = action['action']

        for s in config['strategies']:
            if s['name'] == strat_name:
                if act_type == 'UPDATE_PARAMS' or act_type == 'ADD_ADX_FILTER':
                    # Parse params from action string
                    params_str = action['params']
                    # Extract fp/sp/sl/tp
                    import re
                    if 'fast=' in params_str:
                        fp_match = re.search(r'fast=(\d+)', params_str)
                        sp_match = re.search(r'slow=(\d+)', params_str)
                        sl_match = re.search(r'sl=([\d.]+)x', params_str)
                        tp_match = re.search(r'tp=([\d.]+)x', params_str)
                        if fp_match: s['params']['fast_period'] = int(fp_match.group(1))
                        if sp_match: s['params']['slow_period'] = int(sp_match.group(1))
                        if sl_match: s['params']['atr_sl_mult'] = float(sl_match.group(1))
                        if tp_match: s['params']['atr_tp_mult'] = float(tp_match.group(1))

                    # Add ADX threshold if filtered
                    if 'ADD_ADX_FILTER' in act_type:
                        s['params']['regime_filter'] = 'ADX'
                        s['params']['adx_threshold'] = 20
                        s['params']['adx_period'] = 14

                elif act_type == 'ENABLE_WITH_RANGE_FILTER':
                    s['enabled'] = True
                    # Parse RSI params
                    params_str = action['params']
                    rp = re.search(r'rsi_p=(\d+)', params_str)
                    os_m = re.search(r'os=(\d+)', params_str)
                    ob_m = re.search(r'ob=(\d+)', params_str)
                    sl_m = re.search(r'sl=([\d.]+)%', params_str)
                    tp_m = re.search(r'tp=([\d.]+)%', params_str)
                    if rp: s['params']['rsi_period'] = int(rp.group(1))
                    if os_m: s['params']['oversold'] = int(os_m.group(1))
                    if ob_m: s['params']['overbought'] = int(ob_m.group(1))
                    if sl_m: s['params']['stop_loss_pct'] = float(sl_m.group(1)) / 100
                    if tp_m: s['params']['take_profit_pct'] = float(tp_m.group(1)) / 100
                    s['params']['regime_filter'] = 'ADX_RANGE'
                    s['params']['adx_threshold'] = 15
                    s['params']['adx_period'] = 14

                updates_applied.append(f"{strat_name}: {act_type}")
                print(f"  ✅ Applied: {strat_name} — {act_type}")

    if updates_applied:
        with open('config/strategies.yaml', 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        print(f"\n  📝 strategies.yaml updated with {len(updates_applied)} changes")

elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
print(f"\n⏱️ {elapsed:.0f}s | 🔥 Prometheus regime-filtered optimization complete")
