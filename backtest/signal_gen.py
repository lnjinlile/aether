"""
Shared signal generators for backtesting — vectorized implementations
mirroring live strategy logic. Used by engine.py (5-min heartbeat) and
athena_backtest.py (7-day evaluation).
"""
import pandas as pd
import numpy as np


def trendfollow_signals(df: pd.DataFrame, ema_period: int,
                         sl_pct: float, tp_pct: float,
                         cooldown_bars: int) -> pd.Series:
    """Vectorized TrendFollow — signals: 1=LONG, -1=SHORT, 0=FLAT."""
    close = df['close'].values
    n = len(close)
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = max(ema_period * 2, 100)

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        slope = ema_slope[i]
        uptrend = slope > 0

        if pos == 1:
            exit_trigger = False
            if not uptrend:
                exit_trigger = True
            elif price <= entry_price * (1 - sl_pct):
                exit_trigger = True
            elif price >= entry_price * (1 + tp_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = False
            if uptrend:
                exit_trigger = True
            elif price >= entry_price * (1 + sl_pct):
                exit_trigger = True
            elif price <= entry_price * (1 - tp_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos

        if pos == 0 and bars_since_trade > cooldown_bars:
            if uptrend:
                pos = 1
                entry_price = price
                signals[i] = 1
                bars_since_trade = 0
            else:
                pos = -1
                entry_price = price
                signals[i] = -1
                bars_since_trade = 0

    return pd.Series(signals, index=df.index)


def rsi_mr_signals(df: pd.DataFrame, rsi_period: int,
                   oversold: float, overbought: float, exit_rsi: float,
                   sl_pct: float, tp_pct: float,
                   cooldown_bars: int) -> pd.Series:
    """Vectorized RSI Mean Reversion — signals: 1=LONG, -1=SHORT, 0=FLAT."""
    close = df['close'].values
    n = len(close)

    delta = pd.Series(close).diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/rsi_period, adjust=False).mean().values
    avg_loss = loss.ewm(alpha=1/rsi_period, adjust=False).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.inf), where=avg_loss != 0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[avg_loss == 0] = 100.0
    rsi[avg_gain == 0] = 0.0

    cross_below_oversold = (rsi < oversold) & (np.roll(rsi, 1) >= oversold)
    cross_below_oversold[:1] = False
    cross_above_overbought = (rsi > overbought) & (np.roll(rsi, 1) <= overbought)
    cross_above_overbought[:1] = False
    cross_above_exit = (rsi > exit_rsi) & (np.roll(rsi, 1) <= exit_rsi)
    cross_above_exit[:1] = False
    cross_below_exit = (rsi < exit_rsi) & (np.roll(rsi, 1) >= exit_rsi)
    cross_below_exit[:1] = False

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = rsi_period * 3

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        if pos == 1:
            exit_trigger = False
            if cross_above_exit[i]:
                exit_trigger = True
            elif price <= entry_price * (1 - sl_pct):
                exit_trigger = True
            elif price >= entry_price * (1 + tp_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = False
            if cross_below_exit[i]:
                exit_trigger = True
            elif price >= entry_price * (1 + sl_pct):
                exit_trigger = True
            elif price <= entry_price * (1 - tp_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos

        if pos == 0 and bars_since_trade > cooldown_bars:
            if cross_below_oversold[i]:
                pos = 1
                entry_price = price
                signals[i] = 1
                bars_since_trade = 0
            elif cross_above_overbought[i]:
                pos = -1
                entry_price = price
                signals[i] = -1
                bars_since_trade = 0

    return pd.Series(signals, index=df.index)


def dynamic_grid_signals(df: pd.DataFrame, grid_range_pct: float, num_levels: int,
                         qty_per_level: float, rebalance_interval_bars: int,
                         min_spread_pct: float, leverage: int = 3) -> pd.Series:
    """Vectorized DynamicGrid — signals: 1=LONG (grid buy fill), 0=EXIT (grid sell fill)."""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(close)
    signals = np.zeros(n, dtype=int)

    half_range = grid_range_pct / 2.0
    step = half_range / num_levels

    levels = []
    centre = 0.0
    bars_since_rebalance = 0
    min_bars = 50

    for i in range(min_bars, n):
        price = close[i]
        bar_high = high[i]
        bar_low = low[i]
        bars_since_rebalance += 1

        if len(levels) == 0 or bars_since_rebalance > rebalance_interval_bars:
            centre = price
            levels = []
            for j in range(num_levels):
                buy_px = centre * (1.0 - half_range / 100.0 + j * step / 100.0)
                sell_px = buy_px * (1.0 + min_spread_pct / 100.0 + step / 100.0)
                if sell_px <= buy_px * (1.0 + min_spread_pct / 100.0):
                    sell_px = buy_px * (1.0 + min_spread_pct / 100.0 + step / 100.0)
                levels.append({
                    'buy': round(buy_px, 1), 'sell': round(sell_px, 1),
                    'buy_filled': False, 'sell_filled': False,
                })
            bars_since_rebalance = 0

        exited = False
        for lv in levels:
            if lv['buy_filled'] and not lv['sell_filled']:
                if bar_high >= lv['sell'] or (
                    i > 0 and close[i-1] < lv['sell'] and price >= lv['sell']
                ):
                    lv['sell_filled'] = True
                    lv['buy_filled'] = False
                    signals[i] = 0
                    exited = True
                    break
        if exited:
            continue

        for lv in levels:
            if not lv['buy_filled']:
                if bar_low <= lv['buy'] or (
                    i > 0 and close[i-1] > lv['buy'] and price <= lv['buy']
                ):
                    lv['buy_filled'] = True
                    signals[i] = 1
                    break

    return pd.Series(signals, index=df.index)


def ma_cross_signals(df: pd.DataFrame, fast_period: int, slow_period: int,
                     atr_period: int, atr_sl_mult: float, atr_tp_mult: float,
                     cooldown_bars: int) -> pd.Series:
    """Vectorized MA Crossover — signals: 1=LONG, -1=SHORT, 0=FLAT."""
    close = df['close'].values
    high = df['high'].values
    low = df['low'].values
    n = len(close)

    fast_ema = pd.Series(close).ewm(span=fast_period, adjust=False).mean().values
    slow_ema = pd.Series(close).ewm(span=slow_period, adjust=False).mean().values

    high_low = high - low
    high_close = np.abs(high - np.roll(close, 1))
    low_close = np.abs(low - np.roll(close, 1))
    high_close[0] = 0
    low_close[0] = 0
    tr = np.maximum(np.maximum(high_low, high_close), low_close)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

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

        if pos == 1:
            exit_trigger = False
            if cross_below[i]:
                exit_trigger = True
            elif atr_entry > 0 and price <= entry_price - atr_entry * atr_sl_mult:
                exit_trigger = True
            elif atr_entry > 0 and price >= entry_price + atr_entry * atr_tp_mult:
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = False
            if cross_above[i]:
                exit_trigger = True
            elif atr_entry > 0 and price >= entry_price + atr_entry * atr_sl_mult:
                exit_trigger = True
            elif atr_entry > 0 and price <= entry_price - atr_entry * atr_tp_mult:
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos

        if pos == 0 and bars_since_trade > cooldown_bars:
            if cross_above[i]:
                pos = 1
                entry_price = price
                atr_entry = atr[i]
                signals[i] = 1
                bars_since_trade = 0
            elif cross_below[i]:
                pos = -1
                entry_price = price
                atr_entry = atr[i]
                signals[i] = -1
                bars_since_trade = 0

    return pd.Series(signals, index=df.index)
