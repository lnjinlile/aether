"""
Shared signal generators for backtesting — vectorized implementations
mirroring live strategy logic. Used by engine.py (5-min heartbeat) and
athena_backtest.py (7-day evaluation).
"""
import pandas as pd
import numpy as np


# ── Shared Computation Utilities ──

def _compute_rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothed RSI — vectorized via pandas ewm for speed.

    Replaces Python for-loop with C-level EMA. Numerically equivalent
    to the Wilder recursion after warmup (correlation >0.9995).
    ~14-28× faster for 10K+ bar series.
    """
    n = len(close)
    delta = np.diff(close, prepend=close[0])
    gain = np.maximum(delta, 0.0)
    loss = np.maximum(-delta, 0.0)
    alpha = 1.0 / period
    avg_gain = pd.Series(gain).ewm(alpha=alpha, adjust=False).mean().values
    avg_loss = pd.Series(loss).ewm(alpha=alpha, adjust=False).mean().values
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.divide(avg_gain, avg_loss)
        rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[:period - 1] = np.nan
    # Handle edge cases: zero loss → RSI=100; both zero → NaN
    rsi = np.where(avg_loss == 0, 100.0, rsi)
    rsi = np.where((avg_gain == 0) & (avg_loss == 0), np.nan, rsi)
    return rsi


def _compute_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 period: int) -> np.ndarray:
    """Wilder's smoothed ATR — vectorized via pandas ewm for speed.

    Replaces Python for-loop with C-level EMA. ~14-28× faster.
    """
    n = len(close)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close),
                               np.abs(low - prev_close)))
    atr = pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().values.copy()
    atr[:period - 1] = np.nan
    return atr


# ── Signal Generators ──


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

    rsi = _compute_rsi(close, rsi_period)

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


def bband_rsi_signals(df: pd.DataFrame,
                      bb_period: int = 20, bb_std: float = 2.5,
                      rsi_period: int = 14, rsi_oversold: float = 30, rsi_overbought: float = 70,
                      stop_loss_pct: float = 0.02, take_profit_pct: float = 0.05,
                      cooldown_bars: int = 3) -> pd.Series:
    """BBand + RSI mean reversion — signals: 1=LONG, -1=SHORT, 0=FLAT."""
    close = df['close'].values.astype(float)
    n = len(close)

    min_bars = max(bb_period, rsi_period) * 2 + 10
    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # Bollinger Bands
    sma = pd.Series(close).rolling(bb_period).mean().values
    std = pd.Series(close).rolling(bb_period).std(ddof=0).values
    upper = sma + bb_std * std
    lower = sma - bb_std * std

    # RSI
    rsi = _compute_rsi(close, rsi_period)

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    start_bar = max(bb_period + 5, rsi_period + 5, 50)

    for i in range(start_bar, n):
        bars_since_trade += 1
        price = close[i]
        b_upper = upper[i]
        b_lower = lower[i]
        rsi_val = rsi[i]

        has_pos = (pos != 0)

        # Exit: take profit / stop loss only
        if has_pos:
            pnl = price / entry_price - 1.0
            if pos == 1:
                if pnl >= take_profit_pct:
                    signals[i] = 0; pos = 0; bars_since_trade = 0; continue
                elif pnl <= -stop_loss_pct:
                    signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            else:  # pos == -1
                if pnl <= -take_profit_pct:
                    signals[i] = 0; pos = 0; bars_since_trade = 0; continue
                elif pnl >= stop_loss_pct:
                    signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            signals[i] = pos
            continue

        # Entry: need cooldown to pass
        if bars_since_trade <= cooldown_bars:
            continue

        # LONG: touch lower band + RSI oversold
        if not np.isnan(b_lower) and price <= b_lower and not np.isnan(rsi_val) and rsi_val < rsi_oversold:
            pos = 1; entry_price = price; signals[i] = 1; bars_since_trade = 0
            continue

        # SHORT: touch upper band + RSI overbought
        if not np.isnan(b_upper) and price >= b_upper and not np.isnan(rsi_val) and rsi_val > rsi_overbought:
            pos = -1; entry_price = price; signals[i] = -1; bars_since_trade = 0
            continue

    return pd.Series(signals, index=df.index)


def adx_trend_signals(df: pd.DataFrame,
                      adx_period: int = 14,
                      adx_threshold: float = 25.0,
                      adx_exit: float = 20.0,
                      ema_period: int = 50,
                      atr_period: int = 14,
                      atr_sl_mult: float = 2.0,
                      atr_tp_mult: float = 4.0,
                      cooldown_bars: int = 3) -> pd.Series:
    """Vectorized ADX+EMA Trend — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Entry: ADX > adx_threshold + price vs EMA direction + DI confirmation.
    Exit:  ADX < adx_exit, direction reversal, or trailing ATR stop.
    """
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)

    min_bars = max(adx_period * 2, ema_period, atr_period) * 2
    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # --- ATR ---
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr2[0] = tr3[0] = 0.0
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

    # --- ADX ---
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr_smooth = pd.Series(tr).ewm(span=adx_period, adjust=False).mean().values
    plus_di = 100.0 * pd.Series(plus_dm).ewm(span=adx_period, adjust=False).mean().values
    minus_di = 100.0 * pd.Series(minus_dm).ewm(span=adx_period, adjust=False).mean().values

    denom = np.where(atr_smooth > 0, atr_smooth, np.nan)
    plus_di = np.where(~np.isnan(denom), plus_di / denom, 0.0)
    minus_di = np.where(~np.isnan(denom), minus_di / denom, 0.0)

    di_sum = plus_di + minus_di
    dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    adx = pd.Series(dx).ewm(span=adx_period, adjust=False).mean().values

    # --- EMA ---
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values

    # --- Signal generation ---
    signals = np.zeros(n, dtype=int)
    pos = 0
    trailing_stop = 0.0
    bars_since_trade = cooldown_bars + 1
    start_bar = max(adx_period * 2, ema_period, atr_period) * 2

    for i in range(start_bar, n):
        bars_since_trade += 1
        price = close[i]
        _adx = adx[i]
        _ema = ema[i]
        _atr = atr[i]
        _plus_di = plus_di[i]
        _minus_di = minus_di[i]

        if np.isnan(_adx) or np.isnan(_ema) or np.isnan(_atr):
            continue

        # --- Exit logic ---
        if pos == 1:
            # ADX exhaustion
            if _adx < adx_exit:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            # Direction reversal
            if price < _ema and _adx > adx_threshold:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            # Trailing stop
            new_trail = max(trailing_stop, price - _atr * atr_sl_mult)
            trailing_stop = new_trail
            if price < trailing_stop:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        elif pos == -1:
            if _adx < adx_exit:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            if price > _ema and _adx > adx_threshold:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            new_trail = min(trailing_stop, price + _atr * atr_sl_mult)
            trailing_stop = new_trail
            if price > trailing_stop:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        if pos != 0:
            signals[i] = pos
            continue

        # --- Entry logic ---
        if bars_since_trade <= cooldown_bars:
            continue

        if _adx < adx_threshold:
            continue

        # LONG: price above EMA + +DI > -DI
        if price > _ema and _plus_di > _minus_di:
            pos = 1; trailing_stop = price - _atr * atr_sl_mult
            signals[i] = 1; bars_since_trade = 0; continue

        # SHORT: price below EMA + -DI > +DI
        if price < _ema and _minus_di > _plus_di:
            pos = -1; trailing_stop = price + _atr * atr_sl_mult
            signals[i] = -1; bars_since_trade = 0; continue

    return pd.Series(signals, index=df.index)


def momentum_signals(df: pd.DataFrame,
                     fast_ema: int = 12, slow_ema: int = 26,
                     signal_period: int = 9, atr_period: int = 14,
                     atr_sl_mult: float = 2.0, atr_tp_mult: float = 3.5) -> pd.Series:
    """Vectorized Momentum (MACD always-in-market) — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Mirrors strategy/examples/momentum.py:
    - MACD direction determines bias: MACD_hist > 0 → LONG, < 0 → SHORT
    - Flip in MACD sign → exit current position
    - ATR-based SL/TP while in position
    - Always in market after first entry
    """
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)
    min_bars = max(slow_ema, atr_period) * 2 + signal_period

    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # MACD
    ema_fast = pd.Series(close).ewm(span=fast_ema, adjust=False).mean().values
    ema_slow = pd.Series(close).ewm(span=slow_ema, adjust=False).mean().values
    macd = ema_fast - ema_slow
    macd_signal = pd.Series(macd).ewm(span=signal_period, adjust=False).mean().values
    macd_hist = macd - macd_signal
    macd_direction = macd_hist > 0  # True=LONG bias, False=SHORT bias

    # ATR
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr2[0] = tr3[0] = 0.0
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    start_bar = min_bars

    for i in range(start_bar, n):
        price = close[i]
        _atr = atr[i]
        if np.isnan(_atr) or _atr <= 0:
            continue

        macd_long = macd_direction[i]
        macd_prev_long = macd_direction[i - 1] if i > 0 else macd_long
        flipped = macd_long != macd_prev_long

        # --- First entry: always enter ---
        if pos == 0 and i == start_bar:
            pos = 1 if macd_long else -1
            entry_price = price
            signals[i] = pos
            continue

        # --- Exit on MACD flip ---
        if pos != 0 and flipped:
            signals[i] = 0  # close
            pos = 1 if macd_long else -1
            entry_price = price
            continue

        # --- ATR SL/TP while in position, then reverse ---
        if pos == 1:
            if price <= entry_price - _atr * atr_sl_mult:
                signals[i] = 0
            elif price >= entry_price + _atr * atr_tp_mult:
                signals[i] = 0
            else:
                signals[i] = pos
                continue
            pos = -1 if not macd_long else 1
            entry_price = price
            continue
        elif pos == -1:
            if price >= entry_price + _atr * atr_sl_mult:
                signals[i] = 0
            elif price <= entry_price - _atr * atr_tp_mult:
                signals[i] = 0
            else:
                signals[i] = pos
                continue
            pos = 1 if macd_long else -1
            entry_price = price
            continue

    return pd.Series(signals, index=df.index)


def vol_breakout_signals(
    df: pd.DataFrame,
    atr_period: int = 20,
    atr_mult: float = 2.0,
    ema_period: int = 50,
    atr_sl_mult: float = 1.5,
    atr_tp_mult: float = 3.0,
    cooldown_bars: int = 5,
    volume_filter: bool = True,
    vol_ma_period: int = 20,
) -> pd.Series:
    """Vectorized VolBreakout — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Mirrors strategy/examples/vol_breakout.py logic:
    - Price breaks above EMA + N*ATR → LONG
    - Price breaks below EMA - N*ATR → SHORT
    - Price crosses back through EMA → exit
    - ATR trailing stop while in position
    """
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    volume = df.get("volume", pd.Series(1.0, index=df.index)).values.astype(float)
    n = len(close)
    min_bars = max(ema_period, atr_period) * 2 + 10
    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # ATR
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr2[0] = tr3[0] = 0.0
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

    # EMA
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values

    # Upper/Lower channel
    upper = ema + atr * atr_mult
    lower = ema - atr * atr_mult

    # Break signals (using prev bar's channel)
    prev_close = np.roll(close, 1)
    prev_upper = np.roll(upper, 1)
    prev_lower = np.roll(lower, 1)
    break_up = (close > prev_upper) & (prev_close <= prev_upper)
    break_down = (close < prev_lower) & (prev_close >= prev_lower)

    # Cross EMA signals
    prev_ema = np.roll(ema, 1)
    cross_below_ema = (close < ema) & (np.roll(close, 1) >= prev_ema)
    cross_above_ema = (close > ema) & (np.roll(close, 1) <= prev_ema)

    # Volume ratio
    if volume_filter:
        vol_ma = pd.Series(volume).rolling(vol_ma_period).mean().values
        vol_ratio = np.where(vol_ma > 0, volume / vol_ma, 1.0)
    else:
        vol_ratio = np.ones(n)

    signals = np.zeros(n, dtype=int)
    pos = 0
    trailing_stop = 0.0
    bars_since_trade = cooldown_bars + 1

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        _atr = atr[i]
        if np.isnan(_atr) or _atr <= 0:
            continue

        # --- Exit logic ---
        if pos == 1:
            if cross_below_ema[i]:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            new_trail = max(trailing_stop, price - _atr * atr_sl_mult)
            trailing_stop = new_trail
            if price <= trailing_stop:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        elif pos == -1:
            if cross_above_ema[i]:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue
            new_trail = min(trailing_stop, price + _atr * atr_sl_mult)
            trailing_stop = new_trail
            if price >= trailing_stop:
                signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        if pos != 0:
            signals[i] = pos
            continue

        # --- Entry logic ---
        if bars_since_trade <= cooldown_bars:
            continue
        if volume_filter and vol_ratio[i] < 1.0:
            continue

        if break_up[i]:
            pos = 1
            trailing_stop = price - _atr * atr_sl_mult
            signals[i] = 1
            bars_since_trade = 0
        elif break_down[i]:
            pos = -1
            trailing_stop = price + _atr * atr_sl_mult
            signals[i] = -1
            bars_since_trade = 0

    return pd.Series(signals, index=df.index)


def trend_pullback_signals(df: pd.DataFrame,
                            ema_period: int = 100, atr_period: int = 14,
                            atr_sl_mult: float = 1.5, atr_tp_mult: float = 3.0,
                            cooldown_bars: int = 5) -> pd.Series:
    """Vectorized TrendPullback — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Mirrors strategy/examples/trend_pullback.py:
    - EMA100 slope determines trend: slope > 0 → uptrend (LONG), < 0 → downtrend (SHORT)
    - Entry: always in trend direction after cooldown
    - Exit: trend reversal (slope sign flip) or ATR SL/TP (capped at 5% of price)
    """
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)
    min_bars = ema_period * 2 + atr_period + 10

    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # EMA + slope
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]

    # ATR
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr2[0] = tr3[0] = 0.0
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    start_bar = min_bars

    for i in range(start_bar, n):
        bars_since_trade += 1
        price = close[i]
        slope = ema_slope[i]
        _atr = atr[i]
        if np.isnan(_atr) or _atr <= 0:
            continue
        uptrend = slope > 0

        # --- Exit logic ---
        if pos == 1:
            exit_trigger = False
            if not uptrend:
                exit_trigger = True
            else:
                atr_capped = min(_atr, price * 0.05)
                if atr_capped > 0:
                    if price <= entry_price - atr_capped * atr_sl_mult:
                        exit_trigger = True
                    elif price >= entry_price + atr_capped * atr_tp_mult:
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
            else:
                atr_capped = min(_atr, price * 0.05)
                if atr_capped > 0:
                    if price >= entry_price + atr_capped * atr_sl_mult:
                        exit_trigger = True
                    elif price <= entry_price - atr_capped * atr_tp_mult:
                        exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        # Hold
        if pos != 0:
            signals[i] = pos
            continue

        # --- Entry: trend direction, after cooldown ---
        if bars_since_trade <= cooldown_bars:
            continue

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


def supertrend_signals(
    df: pd.DataFrame,
    atr_period: int = 10,
    atr_mult: float = 3.0,
    cooldown_bars: int = 3,
) -> pd.Series:
    """Vectorized Supertrend — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Mirrors strategy/examples/supertrend.py logic:
    - Compute ATR and Supertrend bands (upper/lower)
    - Trend flip UP (close crosses above previous lower band) → LONG
    - Trend flip DOWN (close crosses below previous upper band) → SHORT
    - Trend reversal → exit current position
    - Cooldown bars after each trade exit
    """
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    n = len(close)
    min_bars = atr_period * 2 + 10
    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # ATR (via ewm)
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr2[0] = tr3[0] = 0.0
    tr = np.maximum(np.maximum(tr1, tr2), tr3)
    atr = pd.Series(tr).ewm(span=atr_period, adjust=False).mean().values

    # Basic bands
    hl2 = (high + low) / 2
    basic_upper = hl2 + atr_mult * atr
    basic_lower = hl2 - atr_mult * atr

    # Final bands and trend (loop required for Supertrend propagation)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    trend = np.zeros(n, dtype=int)

    # Find first valid bar
    first_valid = atr_period
    for i in range(atr_period, n):
        if not np.isnan(atr[i]):
            first_valid = i
            break
    else:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    final_upper[first_valid] = basic_upper[first_valid]
    final_lower[first_valid] = basic_lower[first_valid]

    for i in range(first_valid + 1, n):
        # Final Upper Band
        if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
        # Final Lower Band
        if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
        # Trend
        prev_lower = final_lower[i-1]
        prev_upper = final_upper[i-1]
        if np.isnan(prev_lower):
            trend[i] = 0
        elif close[i] > prev_lower:
            trend[i] = 1  # Uptrend
        elif close[i] < prev_upper:
            trend[i] = -1  # Downtrend
        else:
            trend[i] = trend[i-1]

    # Signal generation with position tracking and cooldown
    signals = np.zeros(n, dtype=int)
    pos = 0
    bars_since_trade = cooldown_bars + 1

    for i in range(first_valid + 1, n):
        bars_since_trade += 1

        # Exit on trend reversal
        if pos == 1 and trend[i] == -1:
            signals[i] = 0; pos = 0; bars_since_trade = 0; continue
        if pos == -1 and trend[i] == 1:
            signals[i] = 0; pos = 0; bars_since_trade = 0; continue

        if pos != 0:
            signals[i] = pos
            continue

        # Entry with cooldown
        if bars_since_trade <= cooldown_bars:
            continue

        # Bull flip: -1 → 1
        if trend[i] == 1 and trend[i-1] == -1:
            pos = 1; signals[i] = 1; bars_since_trade = 0; continue
        # Bear flip: 1 → -1
        if trend[i] == -1 and trend[i-1] == 1:
            pos = -1; signals[i] = -1; bars_since_trade = 0; continue

    return pd.Series(signals, index=df.index)


def macd_crossover_signals(df: pd.DataFrame,
                           fast_period: int = 12,
                           slow_period: int = 26,
                           signal_period: int = 9,
                           stop_loss_pct: float = 0.02,
                           take_profit_pct: float = 0.04,
                           cooldown_bars: int = 5) -> pd.Series:
    """Vectorized MACD Crossover — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Mirrors strategy/examples/macd.py:
    - Bullish crossover (MACD crosses above signal) → LONG
    - Bearish crossover (MACD crosses below signal) → SHORT
    - Cross back to opposite direction → CLOSE
    - Fixed SL/TP with cooldown between trades
    """
    close = df['close'].values.astype(float)
    n = len(close)
    min_bars = max(slow_period, signal_period) + 2

    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # MACD computation
    ema_fast = pd.Series(close).ewm(span=fast_period, adjust=False).mean().values
    ema_slow = pd.Series(close).ewm(span=slow_period, adjust=False).mean().values
    macd_line = ema_fast - ema_slow
    signal_line = pd.Series(macd_line).ewm(span=signal_period, adjust=False).mean().values

    # Crossover detection
    macd_above = macd_line > signal_line
    macd_above_prev = np.roll(macd_above, 1)
    macd_above_prev[0] = macd_above_prev[1]

    cross_above = macd_above & ~macd_above_prev
    cross_below = ~macd_above & macd_above_prev

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        if pos == 1:
            if cross_below[i]:
                pos = 0; bars_since_trade = 0; continue
            if price <= entry_price * (1 - stop_loss_pct):
                pos = 0; bars_since_trade = 0; continue
            if price >= entry_price * (1 + take_profit_pct):
                pos = 0; bars_since_trade = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if cross_above[i]:
                pos = 0; bars_since_trade = 0; continue
            if price >= entry_price * (1 + stop_loss_pct):
                pos = 0; bars_since_trade = 0; continue
            if price <= entry_price * (1 - take_profit_pct):
                pos = 0; bars_since_trade = 0; continue
            signals[i] = -1; continue

        if pos == 0 and bars_since_trade > cooldown_bars:
            if cross_above[i]:
                pos = 1; entry_price = price; signals[i] = 1; bars_since_trade = 0; continue
            if cross_below[i]:
                pos = -1; entry_price = price; signals[i] = -1; bars_since_trade = 0; continue

    return pd.Series(signals, index=df.index)


def donchian_mr_signals(df: pd.DataFrame,
                        donchian_period: int = 20,
                        rsi_period: int = 14,
                        oversold: float = 20.0,
                        overbought: float = 80.0,
                        exit_level: float = 50.0,
                        stop_loss_pct: float = 0.02,
                        take_profit_pct: float = 0.04,
                        cooldown_bars: int = 5) -> pd.Series:
    """
    Donchian Channel Mean Reversion — fade the breakout.

    Donchian bands are computed on shifted close so current bar can break through.
    LONG: close drops below N-period min close + RSI oversold
    SHORT: close rises above N-period max close + RSI overbought
    Exit: cross back through mid-line, RSI normalizes, or SL/TP hit.

    Optimized params (from 162-run sweep, ETH 1h 365d):
        DP=10, OS=20, OB=80, CD=5 → +429% SR=0.552 DD=16.2% WR=72%
    """
    close = df['close'].values.astype(float)
    n = len(close)

    if n < max(donchian_period, rsi_period) + 5:
        return pd.Series(0, index=df.index, dtype=int)

    signals = np.zeros(n, dtype=int) if isinstance(df.index, pd.DatetimeIndex) else np.zeros(n, dtype=int)

    # Donchian Channel — shifted so current bar excluded from window
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]

    dc_upper = np.full(n, np.nan)
    dc_lower = np.full(n, np.nan)
    dc_mid = np.full(n, np.nan)

    for i in range(donchian_period, n):
        window = prev_close[i - donchian_period + 1:i + 1]
        dc_upper[i] = np.max(window)
        dc_lower[i] = np.min(window)
        dc_mid[i] = (dc_upper[i] + dc_lower[i]) / 2.0

    # RSI (via shared utility)
    rsi = _compute_rsi(close, rsi_period)

    min_bars = max(donchian_period, rsi_period) + 5
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        if np.isnan(dc_upper[i]) or np.isnan(rsi[i]):
            continue

        # Position management
        if pos == 1:
            cross_mid = (price > dc_mid[i]) and (close[i - 1] <= dc_mid[i - 1])
            rsi_exit = (rsi[i] > exit_level) and (rsi[i - 1] <= exit_level)
            sl_hit = price <= entry_price * (1 - stop_loss_pct)
            tp_hit = price >= entry_price * (1 + take_profit_pct)
            if cross_mid or rsi_exit or sl_hit or tp_hit:
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = 1
            continue

        elif pos == -1:
            cross_mid = (price < dc_mid[i]) and (close[i - 1] >= dc_mid[i - 1])
            rsi_exit = (rsi[i] < exit_level) and (rsi[i - 1] >= exit_level)
            sl_hit = price >= entry_price * (1 + stop_loss_pct)
            tp_hit = price <= entry_price * (1 - take_profit_pct)
            if cross_mid or rsi_exit or sl_hit or tp_hit:
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = -1
            continue

        # Entry
        if pos == 0 and bars_since_trade > cooldown_bars:
            if price < dc_lower[i] and rsi[i] < oversold:
                pos = 1
                entry_price = price
                signals[i] = 1
                bars_since_trade = 0
                continue
            if price > dc_upper[i] and rsi[i] > overbought:
                pos = -1
                entry_price = price
                signals[i] = -1
                bars_since_trade = 0
                continue

    return pd.Series(signals, index=df.index)


def stoch_rsi_signals(df: pd.DataFrame,
                      rsi_period: int = 14,
                      stoch_period: int = 14,
                      smooth_k: int = 3,
                      smooth_d: int = 3,
                      oversold: float = 0.20,
                      overbought: float = 0.80,
                      stop_loss_pct: float = 0.02,
                      take_profit_pct: float = 0.04,
                      cooldown_bars: int = 5) -> pd.Series:
    """Vectorized StochRSI Mean Reversion — signals: 1=LONG, -1=SHORT, 0=FLAT.

    StochRSI = (RSI - min(RSI, stoch_period)) / (max(RSI, stoch_period) - min(RSI, stoch_period))
    %K = SMA(StochRSI, smooth_k)

    Entry:
      %K crosses below oversold → LONG
      %K crosses above overbought → SHORT
    Exit:
      %K crosses 0.5 midline from below → CLOSE_LONG
      %K crosses 0.5 midline from above → CLOSE_SHORT
    """
    close = df["close"].values
    n = len(close)

    # ── Compute RSI ──
    rsi = _compute_rsi(close, rsi_period)

    # ── Compute StochRSI %K ──
    stoch_k_vals = np.full(n, np.nan)
    warmup = rsi_period + stoch_period
    for i in range(warmup, n):
        window = rsi[i - stoch_period + 1 : i + 1]
        rsi_min = np.min(window)
        rsi_max = np.max(window)
        denom = rsi_max - rsi_min
        if denom == 0:
            stoch_raw = 0.5
        else:
            stoch_raw = (rsi[i] - rsi_min) / denom
        stoch_k_vals[i] = np.clip(stoch_raw, 0.0, 1.0)

    # ── Signal generation ──
    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = rsi_period + stoch_period + smooth_k + smooth_d + 5

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        curr_k = stoch_k_vals[i]
        prev_k = stoch_k_vals[i - 1]
        if np.isnan(curr_k) or np.isnan(prev_k):
            continue

        # ── Exit: midline crossover + SL/TP ──
        cross_above_mid = (prev_k <= 0.5) and (curr_k > 0.5)
        cross_below_mid = (prev_k >= 0.5) and (curr_k < 0.5)

        if pos == 1:
            exit_trigger = cross_above_mid
            if not exit_trigger and price <= entry_price * (1 - stop_loss_pct):
                exit_trigger = True
            if not exit_trigger and price >= entry_price * (1 + take_profit_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = cross_below_mid
            if not exit_trigger and price >= entry_price * (1 + stop_loss_pct):
                exit_trigger = True
            if not exit_trigger and price <= entry_price * (1 - take_profit_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos

        # ── Entry: oversold/overbought crossover on %K ──
        cross_below_os = (prev_k >= oversold) and (curr_k < oversold)
        cross_above_ob = (prev_k <= overbought) and (curr_k > overbought)

        if pos == 0 and bars_since_trade > cooldown_bars:
            if cross_below_os:
                pos = 1
                entry_price = price
                signals[i] = 1
                bars_since_trade = 0
            elif cross_above_ob:
                pos = -1
                entry_price = price
                signals[i] = -1
                bars_since_trade = 0

    return pd.Series(signals, index=df.index)


def donchian_trend_signals(df: pd.DataFrame,
                            donchian_period: int = 20,
                            adx_period: int = 14,
                            adx_threshold: float = 25.0,
                            atr_period: int = 14,
                            atr_sl_mult: float = 2.0,
                            atr_tp_mult: float = 4.0,
                            cooldown_bars: int = 5) -> pd.Series:
    """Vectorized Donchian Trend Following — signals: 1=LONG, -1=SHORT, 0=FLAT.

    Entry: close breaks above N-period Donchian high (shifted) + ADX > threshold → LONG
           close breaks below N-period Donchian low (shifted) + ADX > threshold → SHORT
    Exit:  reverse breakout (break opposite side) or ATR-based SL/TP.
    """
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)

    min_bars = max(donchian_period, adx_period, atr_period) + 10
    if n < min_bars:
        return pd.Series(0, index=df.index, dtype=int)

    signals = np.zeros(n, dtype=int)

    # ── Donchian Channel (shifted: use prev bar close for channel calc) ──
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]

    dc_upper = np.full(n, np.nan)
    dc_lower = np.full(n, np.nan)

    for i in range(donchian_period, n):
        window = prev_close[i - donchian_period + 1:i + 1]
        dc_upper[i] = np.max(window)
        dc_lower[i] = np.min(window)

    # ── ATR ──
    atr = _compute_atr(high, low, close, atr_period)

    # ── ADX (Wilder's smoothing) ──
    up_move = high - np.roll(high, 1)
    down_move = np.roll(low, 1) - low
    up_move[0] = down_move[0] = 0.0

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # ADX: Wilder's DI uses Wilder's ATR (already computed)
    atr_wilder = atr

    plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1.0/adx_period, adjust=False).mean().values / atr_wilder
    minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1.0/adx_period, adjust=False).mean().values / atr_wilder

    di_sum = plus_di + minus_di
    di_sum[di_sum == 0] = 1.0
    dx = 100 * np.abs(plus_di - minus_di) / di_sum
    adx = pd.Series(dx).ewm(alpha=1.0/adx_period, adjust=False).mean().values

    # ── Signal generation ──
    pos = 0
    entry_price = 0.0
    entry_atr = 0.0
    bars_since_trade = cooldown_bars + 1

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        if np.isnan(dc_upper[i]) or np.isnan(atr[i]) or np.isnan(adx[i]):
            continue

        curr_atr = atr[i]
        if curr_atr <= 0:
            continue

        # ── Breakout detection ──
        break_upper = (price > dc_upper[i]) and (close[i - 1] <= dc_upper[i - 1])
        break_lower = (price < dc_lower[i]) and (close[i - 1] >= dc_lower[i - 1])

        # ── Position management ──
        if pos == 1:
            exit_break = (price < dc_lower[i])
            sl_hit = price <= entry_price - entry_atr * atr_sl_mult
            tp_hit = price >= entry_price + entry_atr * atr_tp_mult
            if exit_break or sl_hit or tp_hit:
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = 1
            continue

        elif pos == -1:
            exit_break = (price > dc_upper[i])
            sl_hit = price >= entry_price + entry_atr * atr_sl_mult
            tp_hit = price <= entry_price - entry_atr * atr_tp_mult
            if exit_break or sl_hit or tp_hit:
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = -1
            continue

        # ── Entry ──
        if pos == 0 and bars_since_trade > cooldown_bars:
            if adx[i] >= adx_threshold:
                if break_upper:
                    pos = 1
                    entry_price = price
                    entry_atr = curr_atr
                    signals[i] = 1
                    bars_since_trade = 0
                    continue
                elif break_lower:
                    pos = -1
                    entry_price = price
                    entry_atr = curr_atr
                    signals[i] = -1
                    bars_since_trade = 0
                    continue

    return pd.Series(signals, index=df.index)
