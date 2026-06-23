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


def _compute_spread_zscore(eth_close: np.ndarray, btc_close: np.ndarray,
                           lookback: int = 200) -> np.ndarray:
    """Compute rolling z-score of ETH/BTC ratio for spread MR filter.

    Returns z-score array. Positive = ETH overvalued vs BTC.
    The spread is mean-reverting (Hurst=0.076, half-life=3.4h on 1h ETH data).

    Usage in signal generators:
        spread_z = _compute_spread_zscore(eth_close, btc_close)
        spread_ok = (spread_z[i] <= spread_z_entry)  # for LONG entries
    """
    n = len(eth_close)
    ratio = np.where(btc_close > 0, eth_close / btc_close, 0.0)
    z = np.zeros(n)

    for i in range(lookback, n):
        window = ratio[max(0, i - lookback):i]
        mu = np.mean(window)
        sigma = np.std(window)
        if sigma > 1e-6:
            z[i] = (ratio[i] - mu) / sigma
    return z


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


def _compute_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 period: int) -> 'tuple[np.ndarray, np.ndarray, np.ndarray]':
    """Wilder's ADX — vectorized via pandas ewm for speed.

    Returns (adx, plus_di, minus_di) arrays. All three signal generators
    (adx_trend, donchian_trend, trend_composite) previously duplicated
    this ~30-line block. Consolidated in PERF-060.

    Uses Wilder's smoothing internally (TR-based ATR for DI normalization).
    """
    n = len(close)

    # ── True Range ──
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr2[0] = tr3[0] = 0.0
    tr = np.maximum(np.maximum(tr1, tr2), tr3)

    # ── Directional Movement ──
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # ── Wilder-smoothed ATR and DI ──
    atr_wilder = pd.Series(tr).ewm(span=period, adjust=False).mean().values
    plus_di_raw = pd.Series(plus_dm).ewm(span=period, adjust=False).mean().values
    minus_di_raw = pd.Series(minus_dm).ewm(span=period, adjust=False).mean().values

    # ── Normalize DI by Wilder ATR ──
    denom = np.where(atr_wilder > 0, atr_wilder, np.nan)
    plus_di = np.where(~np.isnan(denom), 100.0 * plus_di_raw / denom, 0.0)
    minus_di = np.where(~np.isnan(denom), 100.0 * minus_di_raw / denom, 0.0)

    # ── DX → ADX ──
    di_sum = plus_di + minus_di
    with np.errstate(divide='ignore', invalid='ignore'):
        dx = np.where(di_sum > 0, 100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
    adx = pd.Series(dx).ewm(span=period, adjust=False).mean().values

    return adx, plus_di, minus_di


def _check_sl_tp(price, entry_price, pos,
                 sl_pct=None, tp_pct=None,
                 atr_entry=None, atr_sl_mult=None, atr_tp_mult=None):
    """Check if stop-loss or take-profit is triggered. Returns bool.

    Supports both percentage-based (sl_pct/tp_pct) and ATR-based
    (atr_entry × atr_sl_mult / atr_tp_mult) exits.  Used by all
    signal generators to eliminate ~120 lines of duplicated SL/TP logic.
    """
    if pos == 1:  # Long
        if sl_pct is not None and price <= entry_price * (1.0 - sl_pct):
            return True
        if tp_pct is not None and price >= entry_price * (1.0 + tp_pct):
            return True
        if (atr_entry is not None and atr_sl_mult is not None
                and atr_entry > 0 and price <= entry_price - atr_entry * atr_sl_mult):
            return True
        if (atr_entry is not None and atr_tp_mult is not None
                and atr_entry > 0 and price >= entry_price + atr_entry * atr_tp_mult):
            return True
    elif pos == -1:  # Short
        if sl_pct is not None and price >= entry_price * (1.0 + sl_pct):
            return True
        if tp_pct is not None and price <= entry_price * (1.0 - tp_pct):
            return True
        if (atr_entry is not None and atr_sl_mult is not None
                and atr_entry > 0 and price >= entry_price + atr_entry * atr_sl_mult):
            return True
        if (atr_entry is not None and atr_tp_mult is not None
                and atr_entry > 0 and price <= entry_price - atr_entry * atr_tp_mult):
            return True
    return False


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
            exit_trigger = ((not uptrend) or
                            _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct))
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = (uptrend or
                            _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct))
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
            exit_trigger = (cross_above_exit[i] or
                            _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct))
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = (cross_below_exit[i] or
                            _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct))
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


def _detect_bullish_divergence(close: np.ndarray, rsi: np.ndarray,
                                i: int, lookback: int = 30,
                                rsi_threshold: float = 40.0) -> bool:
    """Detect bullish RSI divergence: price lower low + RSI higher low.

    Looks back from bar i for two swing lows:
    - Recent low: lowest close in last lookback//2 bars
    - Prior low: lowest close in the preceding lookback//2 bars
    Returns True if price LL but RSI HL (bullish divergence).
    Only triggers when RSI < rsi_threshold (confirms oversold context).
    """
    if i < lookback or np.isnan(rsi[i]):
        return False
    if rsi[i] >= rsi_threshold:
        return False

    half = lookback // 2
    # Recent window: [i-half, i]
    recent_start = max(0, i - half)
    recent_slice = slice(recent_start, i + 1)
    recent_price_low_idx = recent_start + np.nanargmin(close[recent_slice])
    recent_rsi_low = rsi[recent_price_low_idx]

    # Prior window: [i-lookback, i-half)
    prior_start = max(0, i - lookback)
    prior_end = max(0, i - half)
    if prior_end <= prior_start:
        return False
    prior_slice = slice(prior_start, prior_end)
    prior_price_low_idx = prior_start + np.nanargmin(close[prior_slice])
    prior_rsi_low = rsi[prior_price_low_idx]

    price_ll = close[recent_price_low_idx] < close[prior_price_low_idx]
    rsi_hl = recent_rsi_low > prior_rsi_low

    return bool(price_ll and rsi_hl)


def _detect_bearish_divergence(close: np.ndarray, rsi: np.ndarray,
                                i: int, lookback: int = 30,
                                rsi_threshold: float = 60.0) -> bool:
    """Detect bearish RSI divergence: price higher high + RSI lower high."""
    if i < lookback or np.isnan(rsi[i]):
        return False
    if rsi[i] <= rsi_threshold:
        return False

    half = lookback // 2
    recent_start = max(0, i - half)
    recent_slice = slice(recent_start, i + 1)
    recent_price_high_idx = recent_start + np.nanargmax(close[recent_slice])
    recent_rsi_high = rsi[recent_price_high_idx]

    prior_start = max(0, i - lookback)
    prior_end = max(0, i - half)
    if prior_end <= prior_start:
        return False
    prior_slice = slice(prior_start, prior_end)
    prior_price_high_idx = prior_start + np.nanargmax(close[prior_slice])
    prior_rsi_high = rsi[prior_price_high_idx]

    price_hh = close[recent_price_high_idx] > close[prior_price_high_idx]
    rsi_lh = recent_rsi_high < prior_rsi_high

    return bool(price_hh and rsi_lh)


def rsi_divergence_mr_signals(df: pd.DataFrame, rsi_period: int,
                               oversold: float, overbought: float, exit_rsi: float,
                               sl_pct: float, tp_pct: float,
                               cooldown_bars: int,
                               div_lookback: int = 30,
                               require_divergence: bool = True,
                               div_rsi_max: float = 40.0) -> pd.Series:
    """Vectorized RSI Mean Reversion with divergence confirmation.

    Extends rsi_mr_signals with optional RSI divergence filter:
    - LONG requires bullish divergence (price LL + RSI HL) in oversold zone
    - SHORT requires bearish divergence (price HH + RSI LH) in overbought zone
    - When require_divergence=False, falls back to plain RSI MR (divergence adds
      extra confidence weight but doesn't block entry).

    Signals: 1=LONG, -1=SHORT, 0=FLAT.
    """
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
    min_bars = max(rsi_period * 3, div_lookback + 5)

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        # Exit logic (same as rsi_mr_signals)
        if pos == 1:
            if cross_above_exit[i] or _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            if cross_below_exit[i] or _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos

        # Entry logic
        if pos == 0 and bars_since_trade > cooldown_bars:
            if cross_below_oversold[i]:
                if require_divergence:
                    if _detect_bullish_divergence(close, rsi, i, div_lookback, div_rsi_max):
                        pos = 1
                        entry_price = price
                        signals[i] = 1
                        bars_since_trade = 0
                else:
                    pos = 1
                    entry_price = price
                    signals[i] = 1
                    bars_since_trade = 0
            elif cross_above_overbought[i]:
                if require_divergence:
                    if _detect_bearish_divergence(close, rsi, i, div_lookback,
                                                  overbought if overbought > 60 else 60.0):
                        pos = -1
                        entry_price = price
                        signals[i] = -1
                        bars_since_trade = 0
                else:
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

    atr = _compute_atr(high, low, close, atr_period)

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
            exit_trigger = cross_below[i] or _check_sl_tp(
                price, entry_price, pos,
                atr_entry=atr_entry, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult)
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = cross_above[i] or _check_sl_tp(
                price, entry_price, pos,
                atr_entry=atr_entry, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult)
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
            if _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct):
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

    # --- ATR (shared utility for signal exits) ---
    atr = _compute_atr(high, low, close, atr_period)

    # ADX + DI (PERF-060: shared _compute_adx utility, was ~24 lines inline)
    adx, plus_di, minus_di = _compute_adx(high, low, close, adx_period)

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

    # ATR (shared utility)
    atr = _compute_atr(high, low, close, atr_period)

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
            if _check_sl_tp(price, entry_price, pos,
                            atr_entry=_atr, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult):
                signals[i] = 0
                pos = -1 if not macd_long else 1
                entry_price = price
                continue
            signals[i] = pos
            continue
        elif pos == -1:
            if _check_sl_tp(price, entry_price, pos,
                            atr_entry=_atr, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult):
                signals[i] = 0
                pos = 1 if not macd_long else -1
                entry_price = price
                continue
            signals[i] = pos
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

    # ATR (shared utility)
    atr = _compute_atr(high, low, close, atr_period)

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

    # ATR (shared utility)
    atr = _compute_atr(high, low, close, atr_period)

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
            if not uptrend:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
            atr_capped = min(_atr, price * 0.05)
            if _check_sl_tp(price, entry_price, pos,
                            atr_entry=atr_capped, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult):
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            if uptrend:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
            atr_capped = min(_atr, price * 0.05)
            if _check_sl_tp(price, entry_price, pos,
                            atr_entry=atr_capped, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult):
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

    # ATR (shared utility, via ewm)
    atr = _compute_atr(high, low, close, atr_period)

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
            if _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct):
                pos = 0; bars_since_trade = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if cross_above[i]:
                pos = 0; bars_since_trade = 0; continue
            if _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct):
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
                        cooldown_bars: int = 5,
                        volume_filter: float = 0.0,
                        spread_z_entry: float = 0.0) -> pd.Series:
    """
    Donchian Channel Mean Reversion — fade the breakout.

    Donchian bands are computed on shifted close so current bar can break through.
    LONG: close drops below N-period min close + RSI oversold
    SHORT: close rises above N-period max close + RSI overbought
    Exit: cross back through mid-line, RSI normalizes, or SL/TP hit.

    volume_filter: if > 0, requires volume > MA(volume,20) * volume_filter at entry.
                   Default 0.0 (disabled). BandMR uses 1.2.

    spread_z_entry: if < 0, requires ETH/BTC ratio z-score < spread_z_entry for LONG entry.
                    (ETH undervalued vs BTC). Default 0.0 (disabled).
                    Only affects ETH/USDT symbols. Adds ~200ms for DB lookup.
                    PERF-094: Spread MR filter — Hurst=0.076, half-life=3.4h.

    Optimized params (from 162-run sweep, ETH 1h 365d):
        DP=10, OS=20, OB=80, CD=5 → +429% SR=0.552 DD=16.2% WR=72%
    """
    close = df['close'].values.astype(float)
    n = len(close)

    if n < max(donchian_period, rsi_period) + 5:
        return pd.Series(0, index=df.index, dtype=int)

    signals = np.zeros(n, dtype=int) if isinstance(df.index, pd.DatetimeIndex) else np.zeros(n, dtype=int)

    # Donchian Channel — vectorized via pandas rolling (O(n) C-level vs O(n×period) loop)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    prev_series = pd.Series(prev_close, index=df.index)

    dc_upper = prev_series.rolling(donchian_period).max().values
    dc_lower = prev_series.rolling(donchian_period).min().values
    dc_mid = (dc_upper + dc_lower) / 2.0

    # RSI (via shared utility)
    rsi = _compute_rsi(close, rsi_period)

    # Volume filter — MA(volume, 20) for entry quality confirmation (BandMR)
    vol_ma = None
    vol_ok = np.ones(n, dtype=bool)  # default: all bars pass
    if volume_filter > 0 and 'volume' in df.columns:
        vol = df['volume'].values.astype(float)
        vol_ma = pd.Series(vol).rolling(20).mean().values
        vol_ok = vol > (vol_ma * volume_filter)

    # Spread z-score filter — ETH/BTC ratio mean-reversion (PERF-094)
    spread_z = np.zeros(n)
    spread_ok = np.ones(n, dtype=bool)  # default: all bars pass
    if spread_z_entry < 0:
        try:
            import sqlite3, os
            # Detect ETH symbol from DataFrame attrs or column metadata
            is_eth = False
            for attr_val in [df.attrs.get('symbol', ''), str(df.attrs)]:
                if 'ETH' in attr_val.upper():
                    is_eth = True
                    break
            if is_eth:
                # Find market.db
                candidates = [
                    os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), 'data', 'market.db'),
                    'data/market.db',
                ]
                db_path = None
                for c in candidates:
                    if os.path.exists(c):
                        db_path = c
                        break
                if db_path:
                    db = sqlite3.connect(db_path)
                    # Read both BTC and ETH 1h close prices aligned by timestamp.
                    # DB returns ALL bars; trim to match DataFrame length (covers lookback).
                    rows = db.execute("""
                        SELECT b.open_time, e.close as eth_close, b.close as btc_close
                        FROM klines b
                        JOIN klines e ON b.open_time = e.open_time
                            AND e.symbol = 'ETH/USDT' AND e.timeframe = '1h'
                        WHERE b.symbol = 'BTC/USDT' AND b.timeframe = '1h'
                        ORDER BY b.open_time
                    """).fetchall()
                    db.close()
                    if rows and len(rows) >= n:
                        eth_aligned = np.array([r[1] for r in rows], dtype=float)
                        btc_aligned = np.array([r[2] for r in rows], dtype=float)
                        full_spread_z = _compute_spread_zscore(eth_aligned, btc_aligned)
                        # Trim to match DataFrame length (last n bars)
                        spread_z_full = full_spread_z[-n:]
                        # Verify alignment via close price match on first valid bar
                        if abs(eth_aligned[-n] - close[0]) < 0.01:
                            spread_z = spread_z_full
                            spread_ok = spread_z <= spread_z_entry
        except Exception:
            pass  # fallback: spread filter disabled on error

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
            if cross_mid or rsi_exit or _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct):
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = 1
            continue

        elif pos == -1:
            cross_mid = (price < dc_mid[i]) and (close[i - 1] >= dc_mid[i - 1])
            rsi_exit = (rsi[i] < exit_level) and (rsi[i - 1] >= exit_level)
            if cross_mid or rsi_exit or _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct):
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = -1
            continue

        # Entry
        if pos == 0 and bars_since_trade > cooldown_bars and vol_ok[i]:
            if price < dc_lower[i] and rsi[i] < oversold and spread_ok[i]:
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

    # ── Compute StochRSI %K — vectorized via pandas rolling (was O(n×period) loop) ──
    rsi_series = pd.Series(rsi)
    rsi_min = rsi_series.rolling(stoch_period).min().values
    rsi_max = rsi_series.rolling(stoch_period).max().values
    denom = rsi_max - rsi_min
    stoch_raw = np.where(denom == 0, 0.5, (rsi - rsi_min) / np.where(denom == 0, 1.0, denom))
    stoch_k_vals = np.clip(stoch_raw, 0.0, 1.0)

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
            exit_trigger = cross_above_mid or _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct)
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = cross_below_mid or _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct)
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

    # ── Donchian Channel — vectorized via pandas rolling (O(n) C-level) ──
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    prev_series = pd.Series(prev_close, index=df.index)

    dc_upper = prev_series.rolling(donchian_period).max().values
    dc_lower = prev_series.rolling(donchian_period).min().values

    # ── ATR (shared utility) ──
    atr = _compute_atr(high, low, close, atr_period)

    # ADX + DI (PERF-060: shared utility, was ~18 lines inline)
    adx, plus_di, minus_di = _compute_adx(high, low, close, adx_period)

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
            if exit_break or _check_sl_tp(price, entry_price, pos,
                                          atr_entry=entry_atr, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult):
                pos = 0
                bars_since_trade = 0
                continue
            signals[i] = 1
            continue

        elif pos == -1:
            exit_break = (price > dc_upper[i])
            if exit_break or _check_sl_tp(price, entry_price, pos,
                                          atr_entry=entry_atr, atr_sl_mult=atr_sl_mult, atr_tp_mult=atr_tp_mult):
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


# ══════════════════════════════════════════════════════════════════════
# ML Strategy Signal Generators — migrated from athena_backtest.py
# ══════════════════════════════════════════════════════════════════════

def mlalpha_signals(df: pd.DataFrame, model_path: str = "ml_alpha/model.pkl",
                    confidence_threshold: float = 0.55,
                    sl_pct: float = 0.02, tp_pct: float = 0.04) -> pd.Series:
    """MLAlpha (LightGBM) signal generator — loads pre-trained model, generates signals with SL/TP."""
    from ml_alpha.features import FeatureEngineer
    from ml_alpha.trainer import AlphaModel

    engineer = FeatureEngineer()
    X_full, _ = engineer.build_features(df)
    if X_full.empty or len(X_full) < 50:
        return pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    model = AlphaModel()
    try:
        model.load(model_path)
    except Exception:
        return pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    expected_features = getattr(model.model, 'n_features_in_', None)
    if expected_features is not None and X_full.shape[1] != expected_features:
        return pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    close = df['close'].astype(float).loc[X_full.index]
    n = len(X_full)
    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0

    for i in range(n):
        row = X_full.iloc[[i]]
        price = float(close.iloc[i])
        try:
            prob = float(model.predict(row)[0])
        except Exception:
            if pos != 0:
                signals[i] = pos
            continue

        if pos == 1:
            if _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                pos = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                pos = 0; continue
            signals[i] = -1; continue

        if pos == 0:
            if prob > confidence_threshold:
                pos = 1; entry_price = price; signals[i] = 1
            elif prob < (1 - confidence_threshold):
                pos = -1; entry_price = price; signals[i] = -1

    full_signals = np.zeros(len(df), dtype=int)
    full_signals[-n:] = signals
    return pd.Series(full_signals, index=df.index)


def mlensemble_signals(df: pd.DataFrame, prediction_horizon: int = 5,
                       confidence_threshold: float = 0.60,
                       min_train_samples: int = 200,
                       sl_pct: float = 0.02, tp_pct: float = 0.03) -> pd.Series:
    """MLEnsemble (LightGBM+XGBoost+RF) — train on first 70%, generate signals on last 30%."""
    try:
        from lightgbm import LGBMClassifier
        from xgboost import XGBClassifier
        from sklearn.ensemble import RandomForestClassifier
    except ImportError:
        return pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    volume = df.get('volume', pd.Series(1.0, index=df.index)).values.astype(float)
    n = len(df)

    if n < min_train_samples + 20:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    feats = pd.DataFrame(index=df.index)
    feats['log_return_1'] = np.log(close / np.roll(close, 1))
    feats['log_return_3'] = np.log(close / np.roll(close, 3))
    feats['log_return_5'] = np.log(close / np.roll(close, 5))
    feats['volatility_10'] = feats['log_return_1'].rolling(10).std()
    feats['volatility_20'] = feats['log_return_1'].rolling(20).std()
    feats['hilo_pct'] = (high - low) / close * 100
    feats['volume_ratio_5'] = volume / pd.Series(volume).rolling(5).mean().values
    feats['price_ma5'] = pd.Series(close).rolling(5).mean()
    feats['price_ma20'] = pd.Series(close).rolling(20).mean()
    feats['ma5_div_ma20'] = feats['price_ma5'] / feats['price_ma20'] - 1.0
    feats['price_div_ma50'] = close / pd.Series(close).rolling(50).mean().values - 1.0

    # RSI_14 (PERF-060: shared _compute_rsi, was ~7 lines inline)
    feats['rsi_14'] = _compute_rsi(close, 14)

    feats = feats.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)

    future_close = np.roll(close, -prediction_horizon)
    future_close[-prediction_horizon:] = np.nan
    pct_change = (future_close - close) / close
    labels = np.full(n, 1, dtype=int)
    labels[pct_change > 0.003] = 2
    labels[pct_change < -0.003] = 0
    labels[-prediction_horizon:] = 1

    train_end = int(n * 0.70)
    if train_end < min_train_samples:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    X_all = feats.values.astype(float)
    y_all = labels
    X_train = X_all[:train_end]
    y_train = y_all[:train_end]

    try:
        lgb = LGBMClassifier(n_estimators=100, max_depth=5, learning_rate=0.05, verbosity=-1,
                             random_state=42, force_col_wise=True, predict_disable_shape_check=True)
        xgb = XGBClassifier(n_estimators=100, max_depth=5, learning_rate=0.05, verbosity=0,
                            random_state=42, eval_metric='mlogloss')
        rf = RandomForestClassifier(n_estimators=100, max_depth=8, random_state=42, n_jobs=-1)
        lgb.fit(X_train, y_train)
        xgb.fit(X_train, y_train)
        rf.fit(X_train, y_train)
    except Exception:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0

    for i in range(train_end, n):
        row = X_all[i:i+1]
        price = close[i]
        try:
            prob_lgb = lgb.predict_proba(row)
            prob_xgb = xgb.predict_proba(row)
            prob_rf = rf.predict_proba(row)
            avg_prob = (prob_lgb + prob_xgb + prob_rf) / 3.0
            pred_class = int(np.argmax(avg_prob, axis=1)[0])
            confidence = float(np.max(avg_prob))
        except Exception:
            if pos != 0:
                signals[i] = pos
            continue

        direction = pred_class - 1

        if pos == 1:
            if direction == -1 or _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                pos = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if direction == 1 or _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                pos = 0; continue
            signals[i] = -1; continue

        if pos == 0 and confidence >= confidence_threshold:
            if direction == 1:
                pos = 1; entry_price = price; signals[i] = 1
            elif direction == -1:
                pos = -1; entry_price = price; signals[i] = -1

    return pd.Series(signals, index=df.index)


def regimeswitch_signals(df: pd.DataFrame,
                         trend_ema_period: int = 50, trend_sl_pct: float = 0.02, trend_tp_pct: float = 0.05,
                         mr_rsi_period: int = 14, mr_oversold: int = 30, mr_overbought: int = 70,
                         mr_sl_pct: float = 0.02, mr_tp_pct: float = 0.04,
                         vol_window: int = 20, regime_lookback: int = 100,
                         cooldown_bars: int = 5, high_vol_capital_pct: float = 0.25) -> pd.Series:
    """RegimeSwitch — heuristic regime detection + sub-strategy dispatch."""
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)

    if n < max(regime_lookback, trend_ema_period, mr_rsi_period) + 10:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    returns = np.diff(np.log(close))
    returns = np.insert(returns, 0, 0.0)
    vol = pd.Series(returns).rolling(vol_window).std().fillna(0).values

    ema = pd.Series(close).ewm(span=trend_ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]

    # RSI (PERF-060: shared _compute_rsi, was ~9 lines inline)
    rsi = _compute_rsi(close, mr_rsi_period)

    ma20 = pd.Series(close).rolling(20).mean().values
    ma50 = pd.Series(close).rolling(50).mean().values

    tr = np.maximum(high - low, np.maximum(
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).ewm(span=14, adjust=False).mean().values

    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    min_bars = max(regime_lookback, trend_ema_period, mr_rsi_period)

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]

        median_vol = np.nanmedian(vol[max(0, i-regime_lookback):i+1])
        current_vol = vol[i]
        trend_strength = abs(ma20[i] - ma50[i]) / price if not np.isnan(ma20[i]) and not np.isnan(ma50[i]) else 0
        slope_dir = ema_slope[i]

        if current_vol > median_vol * 1.5:
            regime = 'HIGH_VOL'
        elif trend_strength > 0.02 or abs(slope_dir / price) > 0.005:
            regime = 'TRENDING'
        elif current_vol < median_vol * 0.5:
            regime = 'LOW_VOL'
        else:
            regime = 'RANGING'

        if regime == 'HIGH_VOL':
            if pos != 0:
                signals[i] = 0; pos = 0
                bars_since_trade = 0
            continue

        if pos == 1:
            if regime == 'RANGING' and atr[i] > 0 and price >= entry_price + atr[i] * 2:
                signals[i] = 0; pos = 0
                bars_since_trade = 0
                continue
            sl_pct = trend_sl_pct if regime == 'TRENDING' else mr_sl_pct
            tp_pct = trend_tp_pct if regime == 'TRENDING' else mr_tp_pct
            if _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                signals[i] = 0; pos = 0
                bars_since_trade = 0
                continue

        elif pos == -1:
            sl_pct = trend_sl_pct if regime == 'TRENDING' else mr_sl_pct
            tp_pct = trend_tp_pct if regime == 'TRENDING' else mr_tp_pct
            if _check_sl_tp(price, entry_price, pos, sl_pct, tp_pct):
                signals[i] = 0; pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos
            continue

        if pos == 0 and bars_since_trade > cooldown_bars:
            if regime == 'TRENDING':
                if ema_slope[i] > 0:
                    pos = 1; entry_price = price; signals[i] = 1
                    bars_since_trade = 0
                elif ema_slope[i] < 0:
                    pos = -1; entry_price = price; signals[i] = -1
                    bars_since_trade = 0
            elif regime in ('RANGING', 'LOW_VOL'):
                if rsi[i] < mr_oversold:
                    pos = 1; entry_price = price; signals[i] = 1
                    bars_since_trade = 0
                elif rsi[i] > mr_overbought:
                    pos = -1; entry_price = price; signals[i] = -1
                    bars_since_trade = 0

    return pd.Series(signals, index=df.index)


# ══════════════════════════════════════════════════════════════════════
# Shared Signal Dispatch Registry — single source of truth for
# strategy_type → signal generator mapping.
# Used by both engine.py (5-min heartbeat) and athena_backtest.py (eval).
# ══════════════════════════════════════════════════════════════════════

def _dispatch_mlalpha_lazy(df, p):
    """Lazy-import wrapper — avoids loading ml_alpha on module import."""
    return mlalpha_signals(
        df, p.get("model_path", "ml_alpha/model.pkl"),
        p.get("confidence_threshold", 0.55),
        sl_pct=p.get("atr_sl_mult", 2.0) * 0.01,
        tp_pct=p.get("atr_tp_mult", 3.0) * 0.01)


def _dispatch_mlensemble_lazy(df, p):
    return mlensemble_signals(
        df, p.get("prediction_horizon", 5),
        p.get("confidence_threshold", 0.60),
        p.get("min_train_samples", 200),
        sl_pct=p.get("atr_sl_mult", 2.0) * 0.01,
        tp_pct=p.get("atr_tp_mult", 3.0) * 0.01)


def _dispatch_regimeswitch_lazy(df, p):
    return regimeswitch_signals(
        df,
        trend_ema_period=p.get("trend_ema_period", 50),
        trend_sl_pct=p.get("trend_sl_pct", 0.02),
        trend_tp_pct=p.get("trend_tp_pct", 0.05),
        mr_rsi_period=p.get("mr_rsi_period", 14),
        mr_oversold=p.get("mr_oversold", 30),
        mr_overbought=p.get("mr_overbought", 70),
        mr_sl_pct=p.get("mr_sl_pct", 0.02),
        mr_tp_pct=p.get("mr_tp_pct", 0.04),
        vol_window=p.get("vol_window", 20),
        regime_lookback=p.get("regime_lookback", 100),
        cooldown_bars=p.get("cooldown_bars", 5),
        high_vol_capital_pct=p.get("high_vol_capital_pct", 0.25))


def keltner_mr_signals(df: pd.DataFrame,
                       kc_period: int = 20,
                       atr_mult: float = 2.0,
                       atr_period: int = 14,
                       rsi_period: int = 14,
                       oversold: float = 25.0,
                       overbought: float = 75.0,
                       exit_level: float = 50.0,
                       stop_loss_pct: float = 0.02,
                       take_profit_pct: float = 0.04,
                       cooldown_bars: int = 5) -> np.ndarray:
    """Vectorized Keltner Channel Mean Reversion.

    Mirrors strategy/examples/keltner_mr.py KeltnerMRStrategy.generate_signal:
    - LONG:  price breaks below KC lower band + RSI < oversold → fade, expect reversion up
    - SHORT: price breaks above KC upper band + RSI > overbought → fade, expect reversion down
    - Exit:  close crosses back across KC midline, or RSI crosses exit_level, or SL/TP hit
    """
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)

    min_bars = max(kc_period, rsi_period, atr_period) * 3 + 10
    if n < min_bars:
        return np.zeros(n, dtype=int)

    # Keltner Channel: EMA middle ± atr_mult * ATR
    ema_mid = pd.Series(close).ewm(span=kc_period, adjust=False).mean().values
    atr = _compute_atr(high, low, close, atr_period)
    kc_upper = ema_mid + atr_mult * atr
    kc_lower = ema_mid - atr_mult * atr

    # RSI
    rsi = _compute_rsi(close, rsi_period)

    # Entry triggers: current bar closes outside, previous close was inside
    prev_c = np.roll(close, 1)
    prev_upper = np.roll(kc_upper, 1)
    prev_lower = np.roll(kc_lower, 1)
    break_lower = (close < kc_lower) & (prev_c >= prev_lower)
    break_upper = (close > kc_upper) & (prev_c <= prev_upper)
    break_lower[:2] = False
    break_upper[:2] = False

    # Exit triggers: cross back across midline or RSI cross
    prev_mid = np.roll(ema_mid, 1)
    cross_above_mid = (close > ema_mid) & (prev_c <= prev_mid)
    cross_below_mid = (close < ema_mid) & (prev_c >= prev_mid)
    cross_above_mid[:2] = False
    cross_below_mid[:2] = False

    prev_rsi = np.roll(rsi, 1)
    rsi_above_exit = (rsi > exit_level) & (prev_rsi <= exit_level)
    rsi_below_exit = (rsi < exit_level) & (prev_rsi >= exit_level)
    rsi_above_exit[:2] = False
    rsi_below_exit[:2] = False

    signals = np.zeros(n, dtype=int)
    pos = 0  # 1=long, -1=short
    entry_price = 0.0
    bars_since_trade = cooldown_bars + 1
    start_bar = max(kc_period, rsi_period, atr_period) * 3

    for i in range(start_bar, n):
        bars_since_trade += 1
        price = close[i]

        if np.isnan(ema_mid[i]) or np.isnan(atr[i]) or np.isnan(rsi[i]):
            continue

        # ---- Position Management ----
        if pos == 1:
            exit_trigger = (cross_above_mid[i] or rsi_above_exit[i] or
                            _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct))
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue
        elif pos == -1:
            exit_trigger = (cross_below_mid[i] or rsi_below_exit[i] or
                            _check_sl_tp(price, entry_price, pos, stop_loss_pct, take_profit_pct))
            if exit_trigger:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos
            continue

        # ---- Entry ----
        if bars_since_trade <= cooldown_bars:
            continue

        _rsi = rsi[i]
        if break_lower[i] and not np.isnan(_rsi) and _rsi < oversold:
            pos = 1
            entry_price = price
            signals[i] = 1
            bars_since_trade = 0
            continue
        if break_upper[i] and not np.isnan(_rsi) and _rsi > overbought:
            pos = -1
            entry_price = price
            signals[i] = -1
            bars_since_trade = 0
            continue

    return pd.Series(signals, index=df.index)


SIGNAL_DISPATCH = {
    "TrendFollow":              lambda d, p: trendfollow_signals(d, p["ema_period"], p["stop_loss_pct"], p["take_profit_pct"], p["cooldown_bars"]),
    "RSIMeanReversionStrategy": lambda d, p: rsi_mr_signals(d, p["rsi_period"], p["oversold"], p["overbought"], p["exit_rsi"], p["stop_loss_pct"], p["take_profit_pct"], p["cooldown_bars"]),
    "MACrossoverStrategy":      lambda d, p: ma_cross_signals(d, p["fast_period"], p["slow_period"], p["atr_period"], p["atr_sl_mult"], p["atr_tp_mult"], p["cooldown_bars"]),
    "DynamicGridStrategy":      lambda d, p: dynamic_grid_signals(d, p["grid_range_pct"], p["num_levels"], p["qty_per_level"], p["rebalance_interval_bars"], p["min_spread_pct"], p.get("leverage", 3)),
    "MLAlphaStrategy":          _dispatch_mlalpha_lazy,
    "MLEnsembleStrategy":       _dispatch_mlensemble_lazy,
    "RegimeSwitchStrategy":     _dispatch_regimeswitch_lazy,
    "BBandMeanReversion":       lambda d, p: bband_rsi_signals(d, p.get("bb_period", 20), p.get("bb_std", 2.5), p.get("rsi_period", 14), p.get("rsi_oversold", 30), p.get("rsi_overbought", 70), p.get("stop_loss_pct", 0.02), p.get("take_profit_pct", 0.05), p.get("cooldown_bars", 3)),
    "DonchianMRStrategy":       lambda d, p: donchian_mr_signals(d, p.get("donchian_period", 20), p.get("rsi_period", 14), p.get("oversold", 20), p.get("overbought", 80), p.get("exit_level", 50), p.get("stop_loss_pct", 0.02), p.get("take_profit_pct", 0.04), p.get("cooldown_bars", 5)),
    "SupertrendStrategy":       lambda d, p: supertrend_signals(d, p.get("atr_period", 10), p.get("atr_mult", 3.0), p.get("cooldown_bars", 3)),
    "VolBreakoutStrategy":      lambda d, p: vol_breakout_signals(d, p.get("atr_period", 20), p.get("atr_mult", 2.0), p.get("ema_period", 50), p.get("atr_sl_mult", 1.5), p.get("atr_tp_mult", 3.0), p.get("cooldown_bars", 5), p.get("volume_filter", True), p.get("vol_ma_period", 20)),
    "MACDCrossoverStrategy":    lambda d, p: macd_crossover_signals(d, p.get("fast_period", 12), p.get("slow_period", 26), p.get("signal_period", 9), p.get("stop_loss_pct", 0.02), p.get("take_profit_pct", 0.04), p.get("cooldown_bars", 5)),
    "ADXTrendStrategy":         lambda d, p: adx_trend_signals(d, p.get("adx_period", 14), p.get("adx_threshold", 25), p.get("adx_exit", 20), p.get("ema_period", 50), p.get("atr_period", 14), p.get("atr_sl_mult", 2.0), p.get("atr_tp_mult", 4.0), p.get("cooldown_bars", 3)),
    "MomentumStrategy":         lambda d, p: momentum_signals(d, p.get("fast_ema", 12), p.get("slow_ema", 26), p.get("signal_period", 9), p.get("atr_period", 14), p.get("atr_sl_mult", 2.0), p.get("atr_tp_mult", 3.5)),
    "TrendPullback":            lambda d, p: trend_pullback_signals(d, p.get("ema_period", 100), p.get("atr_period", 14), p.get("atr_sl_mult", 1.5), p.get("atr_tp_mult", 3.0), p.get("cooldown_bars", 5)),
    "StochRSIMeanReversionStrategy": lambda d, p: stoch_rsi_signals(d, p.get("rsi_period", 14), p.get("stoch_period", 14), p.get("smooth_k", 3), p.get("smooth_d", 3), p.get("oversold", 0.20), p.get("overbought", 0.80), p.get("stop_loss_pct", 0.02), p.get("take_profit_pct", 0.04), p.get("cooldown_bars", 5)),
    "DonchianTrendStrategy":    lambda d, p: donchian_trend_signals(d, p.get("donchian_period", 20), p.get("adx_period", 14), p.get("adx_threshold", 25), p.get("atr_period", 14), p.get("atr_sl_mult", 2.0), p.get("atr_tp_mult", 4.0), p.get("cooldown_bars", 5)),
    "KeltnerMRStrategy":        lambda d, p: keltner_mr_signals(d, p.get("kc_period", 20), p.get("atr_mult", 2.0), p.get("atr_period", 14), p.get("rsi_period", 14), p.get("oversold", 25.0), p.get("overbought", 75.0), p.get("exit_level", 50.0), p.get("stop_loss_pct", 0.02), p.get("take_profit_pct", 0.04), p.get("cooldown_bars", 5)),
    "BandMRStrategy":           lambda d, p: donchian_mr_signals(d, p.get("donchian_period", 20), p.get("rsi_period", 14), p.get("oversold", 30.0), p.get("overbought", 75.0), p.get("exit_level", 50.0), p.get("stop_loss_pct", 0.01), p.get("take_profit_pct", 0.025), p.get("cooldown_bars", 8), p.get("volume_filter", 1.2)),
}


def dispatch_signals(df: pd.DataFrame, strategy_type: str, params: dict) -> np.ndarray:
    """Resolve strategy_type → signal generator → signal array.

    Central dispatch for both engine.py (5-min heartbeat) and athena_backtest.py
    (periodic evaluation). Single source of truth for strategy→signal mapping.

    Returns:
        np.ndarray of signals (1=LONG, -1=SHORT, 0=FLAT)
    Raises:
        KeyError if strategy_type is not registered in SIGNAL_DISPATCH.
    """
    dispatcher = SIGNAL_DISPATCH.get(strategy_type)
    if dispatcher is None:
        raise KeyError(f"Unknown strategy type: {strategy_type}")
    return dispatcher(df, params)


def trend_composite_signals(df: pd.DataFrame,
                             adx_period: int = 14,
                             adx_threshold: float = 25.0,
                             adx_exit: float = 20.0,
                             donchian_period: int = 20,
                             ema_period: int = 50,
                             atr_period: int = 14,
                             atr_sl_mult: float = 2.0,
                             atr_tp_mult: float = 4.0,
                             cooldown_bars: int = 3,
                             require_all: bool = True) -> pd.Series:
    """PERF-051: Trend Composite — AND-filtered multi-indicator consensus.

    Entry requires ALL (if require_all=True) or ≥2-of-3 (if False) of:
      1. ADX > adx_threshold — trend strength confirmed
      2. Donchian breakout — momentum timing
      3. EMA slope direction — trend direction confirmed
      4. DI confirmation (+DI > -DI for LONG, -DI > +DI for SHORT)

    Exit on ANY:
      - ADX < adx_exit (trend exhaustion)
      - Reverse Donchian breakout
      - EMA slope reversal
      - ATR trailing stop

    Signals: 1=LONG, -1=SHORT, 0=FLAT.
    """
    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)

    min_bars = max(adx_period * 3, donchian_period, ema_period, atr_period) + 10
    if n < min_bars:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # ── ATR ──
    atr = _compute_atr(high, low, close, atr_period)

    # ADX + DI (PERF-060: shared utility, was ~20 lines inline)
    adx, plus_di, minus_di = _compute_adx(high, low, close, adx_period)

    # ── Donchian Channel (shifted to avoid look-ahead) ──
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    prev_series = pd.Series(prev_close, index=df.index)
    dc_upper = prev_series.rolling(donchian_period).max().values
    dc_lower = prev_series.rolling(donchian_period).min().values

    # ── EMA slope (5-bar change for direction) ──
    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]

    # ── Signal generation ──
    signals = np.zeros(n, dtype=int)
    pos = 0
    entry_price = 0.0
    entry_atr = 0.0
    trailing_stop = 0.0
    bars_since_trade = cooldown_bars + 1

    for i in range(min_bars, n):
        bars_since_trade += 1
        price = close[i]
        _adx = adx[i]
        _atr = atr[i]
        _ema = ema[i]
        _slope = ema_slope[i]
        _pdi = plus_di[i]
        _mdi = minus_di[i]
        _dcu = dc_upper[i]
        _dcl = dc_lower[i]

        if np.isnan(_adx) or np.isnan(_atr) or np.isnan(_ema) or _atr <= 0:
            continue

        # ── Breakouts ──
        break_up = (price > _dcu) and (i > 0 and close[i - 1] <= dc_upper[i - 1])
        break_down = (price < _dcl) and (i > 0 and close[i - 1] >= dc_lower[i - 1])

        # ── Composite entry conditions ──
        trend_strong = _adx >= adx_threshold
        slope_up = _slope > 0
        slope_down = _slope < 0
        di_long = _pdi > _mdi
        di_short = _mdi > _pdi

        long_votes = sum([trend_strong, break_up, slope_up, di_long])
        short_votes = sum([trend_strong, break_down, slope_down, di_short])

        required = 4 if require_all else 2

        # ── Position management ──
        if pos == 1:
            # Exit checks (any trigger = OR logic)
            exit_adx = _adx < adx_exit
            exit_dc = price < _dcl
            exit_trail = trailing_stop > 0 and price <= trailing_stop
            exit_slope = not slope_up  # EMA slope turned flat/down
            exit_tp = entry_atr > 0 and price >= entry_price + entry_atr * atr_tp_mult

            if exit_adx or exit_dc or exit_slope or exit_trail or exit_tp:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                trailing_stop = 0.0
                continue

            # Update trailing stop (ratchet up for longs)
            trailing_stop = max(trailing_stop, price - _atr * atr_sl_mult)
            signals[i] = 1
            continue

        elif pos == -1:
            exit_adx = _adx < adx_exit
            exit_dc = price > _dcu
            exit_trail = trailing_stop > 0 and price >= trailing_stop
            exit_slope = not slope_down  # EMA slope turned flat/up
            exit_tp = entry_atr > 0 and price <= entry_price - entry_atr * atr_tp_mult

            if exit_adx or exit_dc or exit_slope or exit_trail or exit_tp:
                signals[i] = 0
                pos = 0
                bars_since_trade = 0
                trailing_stop = 0.0
                continue

            # Update trailing stop (ratchet down for shorts)
            trailing_stop = min(trailing_stop, price + _atr * atr_sl_mult)
            signals[i] = -1
            continue

        # ── Entry ──
        if bars_since_trade <= cooldown_bars:
            continue

        if long_votes >= required:
            pos = 1
            entry_price = price
            entry_atr = _atr
            trailing_stop = price - _atr * atr_sl_mult
            signals[i] = 1
            bars_since_trade = 0
            continue

        if short_votes >= required:
            pos = -1
            entry_price = price
            entry_atr = _atr
            trailing_stop = price + _atr * atr_sl_mult
            signals[i] = -1
            bars_since_trade = 0
            continue

    return pd.Series(signals, index=df.index)
