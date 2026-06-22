#!/usr/bin/env python3
"""
Athena — Strategy Brain backtest engine.
Loads strategies.yaml → pulls data from DB → backtests → scores → writes athena.json
Usage: python3 athena_backtest.py [--days N]  (default: 30 days)
"""
import argparse, sys, os, json, yaml, warnings
from datetime import datetime, timezone
from collections import defaultdict

# Suppress sklearn feature-name warnings (LightGBM 4.6.0 bug: predict_disable_shape_check ignored)
warnings.filterwarnings('ignore', message='X does not have valid feature names')
warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

sys.path.insert(0, '/home/rinnen/binance_quant')

import pandas as pd
import numpy as np
from config.settings import get_config
from data.storage import MarketStorage
from backtest.engine import BacktestEngine
from backtest.signal_gen import (
    trendfollow_signals, rsi_mr_signals,
    dynamic_grid_signals, ma_cross_signals,
    bband_rsi_signals, adx_trend_signals,
    momentum_signals, trend_pullback_signals,
)


# ══════════════════════════════════════════════════════════════════════
# ML Strategy Signal Generators (unique to athena_backtest)
# ══════════════════════════════════════════════════════════════════════

def mlalpha_signals(df: pd.DataFrame, model_path: str, confidence_threshold: float = 0.55,
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

    # Check feature compatibility (LightGBM 4.6.0 predict_disable_shape_check broken)
    expected_features = getattr(model.model, 'n_features_in_', None)
    if expected_features is not None and X_full.shape[1] != expected_features:
        print(f"  ⚠️ MLAlpha feature mismatch: model expects {expected_features}, got {X_full.shape[1]}. Skipping.")
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

        # Exit logic
        if pos == 1:
            if price <= entry_price * (1 - sl_pct):
                pos = 0; continue
            elif price >= entry_price * (1 + tp_pct):
                pos = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if price >= entry_price * (1 + sl_pct):
                pos = 0; continue
            elif price <= entry_price * (1 - tp_pct):
                pos = 0; continue
            signals[i] = -1; continue

        # Entry
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
    except ImportError as e:
        print(f"  ⚠️ MLEnsemble: import failed ({e})")
        return pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    volume = df.get('volume', pd.Series(1.0, index=df.index)).values.astype(float)
    n = len(df)

    if n < min_train_samples + 20:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # Build features (replicating ml_ensemble.compute_features logic)
    feats = pd.DataFrame(index=df.index)
    feats['log_return_1'] = np.log(close / np.roll(close, 1))
    feats['log_return_3'] = np.log(close / np.roll(close, 3))
    feats['log_return_5'] = np.log(close / np.roll(close, 5))
    feats['volatility_10'] = feats['log_return_1'].rolling(10).std()
    feats['volatility_20'] = feats['log_return_1'].rolling(20).std()
    feats['hilo_pct'] = (high - low) / close * 100
    feats['volume_ratio_5'] = volume / pd.Series(volume).rolling(5).mean().values

    # Momentum features
    feats['price_ma5'] = pd.Series(close).rolling(5).mean()
    feats['price_ma20'] = pd.Series(close).rolling(20).mean()
    feats['ma5_div_ma20'] = feats['price_ma5'] / feats['price_ma20'] - 1.0
    feats['price_div_ma50'] = close / pd.Series(close).rolling(50).mean().values - 1.0

    # RSI
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=14, adjust=False).mean()
    avg_loss = loss.ewm(span=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    feats['rsi_14'] = 100 - (100 / (1 + rs))

    feats = feats.replace([np.inf, -np.inf], np.nan).ffill().fillna(0.0)

    # Build labels: 3-class (UP=2, FLAT=1, DOWN=0)
    future_close = np.roll(close, -prediction_horizon)
    future_close[-prediction_horizon:] = np.nan
    pct_change = (future_close - close) / close
    labels = np.full(n, 1, dtype=int)  # FLAT=1
    labels[pct_change > 0.003] = 2      # UP=2
    labels[pct_change < -0.003] = 0     # DOWN=0
    labels[-prediction_horizon:] = 1

    # Train/test split
    train_end = int(n * 0.70)
    if train_end < min_train_samples:
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    X_all = feats.values.astype(float)
    y_all = labels

    # Train models on training portion
    X_train = X_all[:train_end]
    y_train = y_all[:train_end]

    try:
        lgb = LGBMClassifier(n_estimators=100, max_depth=5, learning_rate=0.05, verbosity=-1, random_state=42, force_col_wise=True, predict_disable_shape_check=True)
        xgb = XGBClassifier(n_estimators=100, max_depth=5, learning_rate=0.05, verbosity=0, random_state=42, eval_metric='mlogloss')
        rf = RandomForestClassifier(n_estimators=100, max_depth=8, random_state=42, n_jobs=-1)

        lgb.fit(X_train, y_train)
        xgb.fit(X_train, y_train)
        rf.fit(X_train, y_train)
    except Exception as e:
        print(f"  ⚠️ MLEnsemble train failed: {e}")
        return pd.Series(np.zeros(n, dtype=int), index=df.index)

    # Generate signals on test portion
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

        # Remap: 0=DOWN->SHORT(-1), 1=FLAT->HOLD(0), 2=UP->LONG(1)
        direction = pred_class - 1  # 0->-1, 1->0, 2->1

        # Exit
        if pos == 1:
            if direction == -1 or price <= entry_price * (1 - sl_pct) or price >= entry_price * (1 + tp_pct):
                pos = 0; continue
            signals[i] = 1; continue
        elif pos == -1:
            if direction == 1 or price >= entry_price * (1 + sl_pct) or price <= entry_price * (1 - tp_pct):
                pos = 0; continue
            signals[i] = -1; continue

        # Entry
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

    # Compute regime features
    returns = np.diff(np.log(close))
    returns = np.insert(returns, 0, 0.0)
    vol = pd.Series(returns).rolling(vol_window).std().fillna(0).values

    # EMA slope for trend
    ema = pd.Series(close).ewm(span=trend_ema_period, adjust=False).mean().values
    ema_slope = np.zeros(n)
    ema_slope[5:] = ema[5:] - ema[:-5]

    # RSI for MR
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/mr_rsi_period, adjust=False).mean().values
    avg_loss = loss.ewm(alpha=1/mr_rsi_period, adjust=False).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.inf), where=avg_loss != 0)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi[avg_loss == 0] = 100.0
    rsi[avg_gain == 0] = 0.0

    # MA cross for trend strength
    ma20 = pd.Series(close).rolling(20).mean().values
    ma50 = pd.Series(close).rolling(50).mean().values

    # ATR for position management (matching live strategy)
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

        # Determine regime
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

        # HIGH_VOL: hold or reduce position
        if regime == 'HIGH_VOL':
            if pos != 0:
                signals[i] = 0; pos = 0
                bars_since_trade = 0
            continue

        # Exit logic — aligned with live RegimeSwitchStrategy.generate_signal()
        if pos == 1:
            exit_trigger = False
            # RANGING early profit-taking (matching live: price >= entry + 2*ATR)
            if regime == 'RANGING' and atr[i] > 0 and price >= entry_price + atr[i] * 2:
                exit_trigger = True
            else:
                # SL: use trend_sl_pct for TRENDING, mr_sl_pct for RANGING/LOW_VOL
                sl_pct = trend_sl_pct if regime == 'TRENDING' else mr_sl_pct
                tp_pct = trend_tp_pct if regime == 'TRENDING' else mr_tp_pct
                if price <= entry_price * (1 - sl_pct):
                    exit_trigger = True
                elif price >= entry_price * (1 + tp_pct):
                    exit_trigger = True
            if exit_trigger:
                signals[i] = 0; pos = 0
                bars_since_trade = 0
                continue

        elif pos == -1:
            exit_trigger = False
            sl_pct = trend_sl_pct if regime == 'TRENDING' else mr_sl_pct
            tp_pct = trend_tp_pct if regime == 'TRENDING' else mr_tp_pct
            if price >= entry_price * (1 + sl_pct):
                exit_trigger = True
            elif price <= entry_price * (1 - tp_pct):
                exit_trigger = True
            if exit_trigger:
                signals[i] = 0; pos = 0
                bars_since_trade = 0
                continue

        if pos != 0:
            signals[i] = pos
            continue

        # Entry
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
# Load data
# ══════════════════════════════════════════════════════════════════════

def load_df(storage, symbol, timeframe, days=7):
    """Load klines, return as DataFrame with datetime index."""
    df = storage.load_klines(symbol, timeframe)
    if df.empty:
        return None
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)
    # Filter to last N days
    cutoff = df.index[-1] - pd.Timedelta(days=days)
    df = df[df.index >= cutoff]
    return df



if __name__ == '__main__':
    # ── CLI args ──
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=30, help='Lookback days (default: 30)')
    args = parser.parse_args()
    lookback_days = args.days

    # ── Load config ──
    cfg = get_config()
    storage = MarketStorage(cfg.db_path)

    with open('config/strategies.yaml') as f:
        strat_cfg = yaml.safe_load(f)

    strategies_list = strat_cfg['strategies']

    print("🦉 Athena — Strategy Brain Backtest")
    print("=" * 70)
    t0 = datetime.now(timezone.utc)
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')} | Lookback: {lookback_days}d")
    print()

    # Load all needed data
    data = {}
    for sym in ['BTC/USDT', 'ETH/USDT']:
        for tf in ['15m', '1h']:
            df = load_df(storage, sym, tf, days=lookback_days)
            if df is not None and len(df) > 0:
                data[(sym, tf)] = df
                days_span = (df.index[-1] - df.index[0]).days + (df.index[-1] - df.index[0]).seconds / 86400
                print(f"  📊 {sym:10s} {tf:4s}: {len(df):5d} bars, {days_span:.1f}d "
                      f"[{df.index[0].strftime('%m/%d %H:%M')} → {df.index[-1].strftime('%m/%d %H:%M')}]")


    # ══════════════════════════════════════════════════════════════════════
    # Backtest each strategy
    # ══════════════════════════════════════════════════════════════════════

    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    results_summary = []

    print()
    print("=" * 80)
    print(f"STRATEGY BACKTEST RESULTS (last {lookback_days} days)")
    print("=" * 80)

    for s in strategies_list:
        name = s['name']
        enabled = s['enabled']
        p = s['params']
        sym = p['symbols'][0]
        tf = p['timeframes'][0]
        key = (sym, tf)
        df = data.get(key)

        if df is None or len(df) < 50:
            print(f"\n  ⚠️ {name}: insufficient data ({len(df) if df is not None else 0} bars) — skipping")
            results_summary.append({
                'name': name, 'enabled': enabled, 'symbol': sym, 'tf': tf,
                'status': 'SKIPPED', 'reason': 'insufficient data'
            })
            continue

        # Generate signals
        strategy_type = s['class'].split('.')[-1]

        if strategy_type == 'TrendFollow':
            signals = trendfollow_signals(df, p['ema_period'], p['stop_loss_pct'],
                                           p['take_profit_pct'], p['cooldown_bars'])
        elif strategy_type == 'RSIMeanReversionStrategy':
            signals = rsi_mr_signals(df, p['rsi_period'], p['oversold'], p['overbought'],
                                      p['exit_rsi'], p['stop_loss_pct'], p['take_profit_pct'],
                                      p['cooldown_bars'])
        elif strategy_type == 'MACrossoverStrategy':
            signals = ma_cross_signals(df, p['fast_period'], p['slow_period'],
                                        p['atr_period'], p['atr_sl_mult'], p['atr_tp_mult'],
                                        p['cooldown_bars'])
        elif strategy_type == 'DynamicGridStrategy':
            signals = dynamic_grid_signals(df, p['grid_range_pct'], p['num_levels'],
                                            p['qty_per_level'], p['rebalance_interval_bars'],
                                            p['min_spread_pct'], p.get('leverage', 3))
        elif strategy_type == 'MLAlphaStrategy':
            signals = mlalpha_signals(df, p.get('model_path', 'ml_alpha/model.pkl'),
                                      p.get('confidence_threshold', 0.55))
        elif strategy_type == 'MLEnsembleStrategy':
            signals = mlensemble_signals(df,
                                         p.get('prediction_horizon', 5),
                                         p.get('confidence_threshold', 0.60),
                                         p.get('min_train_samples', 200),
                                         p.get('atr_sl_mult', 2.0) * 0.01,
                                         p.get('atr_tp_mult', 3.0) * 0.01)
        elif strategy_type == 'RegimeSwitchStrategy':
            signals = regimeswitch_signals(df,
                                           p.get('trend_ema_period', 50),
                                           p.get('trend_sl_pct', 0.02),
                                           p.get('trend_tp_pct', 0.05),
                                           p.get('mr_rsi_period', 14),
                                           p.get('mr_oversold', 30),
                                           p.get('mr_overbought', 70),
                                           p.get('mr_sl_pct', 0.02),
                                           p.get('mr_tp_pct', 0.04),
                                           p.get('vol_window', 20),
                                           p.get('regime_lookback', 100),
                                           p.get('cooldown_bars', 5),
                                           p.get('high_vol_capital_pct', 0.25))
        elif strategy_type == 'BBandMeanReversion':
            signals = bband_rsi_signals(df,
                                        p.get('bb_period', 20),
                                        p.get('bb_std', 2.5),
                                        p.get('rsi_period', 14),
                                        p.get('rsi_oversold', 30),
                                        p.get('rsi_overbought', 70),
                                        p.get('stop_loss_pct', 0.02),
                                        p.get('take_profit_pct', 0.05),
                                        p.get('cooldown_bars', 3))
        elif strategy_type == 'ADXTrendStrategy':
            signals = adx_trend_signals(df,
                                        p.get('adx_period', 14),
                                        p.get('adx_threshold', 25),
                                        p.get('adx_exit', 20),
                                        p.get('ema_period', 50),
                                        p.get('atr_period', 14),
                                        p.get('atr_sl_mult', 2.0),
                                        p.get('atr_tp_mult', 4.0),
                                        p.get('cooldown_bars', 3))
        elif strategy_type == 'MomentumStrategy':
            signals = momentum_signals(df,
                                       p.get('fast_ema', 12),
                                       p.get('slow_ema', 26),
                                       p.get('signal_period', 9),
                                       p.get('atr_period', 14),
                                       p.get('atr_sl_mult', 2.0),
                                       p.get('atr_tp_mult', 3.5))
        elif strategy_type == 'TrendPullback':
            signals = trend_pullback_signals(df,
                                             p.get('ema_period', 100),
                                             p.get('atr_period', 14),
                                             p.get('atr_sl_mult', 1.5),
                                             p.get('atr_tp_mult', 3.0),
                                             p.get('cooldown_bars', 5))
        else:
            print(f"\n  ⚠️ {name}: unknown strategy type {strategy_type} — skipping")
            results_summary.append({
                'name': name, 'enabled': enabled, 'symbol': sym, 'tf': tf,
                'status': 'SKIPPED', 'reason': f'unknown type: {strategy_type}'
            })
            continue

        # Leverage from strategy config (default 1 if not specified)
        leverage = p.get('leverage', 1)
        result = engine.run(df, signals, leverage=leverage)
        m = result['metrics']

        status_icon = "✅" if enabled else "⏸️"
        print(f"\n  {status_icon} {name} ({sym} {tf})")
        print(f"     Return: {m['total_return_pct']:+.2f}% | Sharpe: {m['sharpe_ratio']:+.3f} | "
              f"MaxDD: {m['max_drawdown_pct']:.2f}%")
        print(f"     Trades: {m['total_trades']} | WinRate: {m['win_rate']:.1f}% | "
              f"PF: {m['profit_factor']:.3f}")
        print(f"     AvgWin: {m['avg_win_pct']:+.2f}% | AvgLoss: {m['avg_loss_pct']:+.2f}% | "
              f"Best: {m['best_trade_pct']:+.2f}% | Worst: {m['worst_trade_pct']:+.2f}%")
        print(f"     Final Equity: ${m['final_equity']:,.2f}")

        # Flag issues
        flags = []
        if m['win_rate'] < 30 and m['total_trades'] >= 3:
            flags.append(f"⚠️ LOW WINRATE ({m['win_rate']:.0f}% < 30%)")
        if m['sharpe_ratio'] < 0 and m['total_trades'] >= 3:
            flags.append(f"⚠️ NEGATIVE SHARPE ({m['sharpe_ratio']:+.3f})")
        if m['total_return_pct'] < -5:
            flags.append(f"⚠️ LARGE LOSS ({m['total_return_pct']:+.2f}%)")
        if m['max_drawdown_pct'] > 10:
            flags.append(f"⚠️ HIGH DRAWDOWN ({m['max_drawdown_pct']:.1f}%)")

        if flags:
            for f in flags:
                print(f"     {f}")

        # Trade log preview
        if not result['trade_log'].empty:
            recent = result['trade_log'].tail(5)
            print(f"     Recent trades:")
            for _, t in recent.iterrows():
                print(f"       {t['direction']:5s} | {t['entry_price']:>10,.2f} → {t['exit_price']:>10,.2f} "
                      f"| PnL: {t['pnl_pct']:+.2f}%")

        results_summary.append({
            'name': name,
            'enabled': enabled,
            'symbol': sym,
            'tf': tf,
            'class': s['class'],
            'params': p,
            'status': 'OK',
            'metrics': m,
            'flags': flags,
            'bars': len(df),
            'data_start': str(df.index[0]),
            'data_end': str(df.index[-1]),
        })


    # ══════════════════════════════════════════════════════════════════════
    # Additional: test MA_Cross with different configs on BTC/ETH 1h
    # ══════════════════════════════════════════════════════════════════════

    print()
    print("=" * 80)
    print("MA_CROSS PARAMETER SWEEP (disabled strategy — evaluation)")
    print("=" * 80)

    ma_cross_sweep = []
    for sym in ['BTC/USDT', 'ETH/USDT']:
        key = (sym, '1h')
        df = data.get(key)
        if df is None or len(df) < 50:
            continue

        for fp, sp in [(7, 25), (5, 20), (10, 30), (12, 26), (5, 13)]:
            for slm, tpm in [(2.0, 3.0), (1.5, 3.0), (2.0, 4.0), (2.5, 4.0)]:
                signals = ma_cross_signals(df, fp, sp, 14, slm, tpm, 5)
                result = engine.run(df, signals, leverage=5)
                m = result['metrics']
                ma_cross_sweep.append({
                    'symbol': sym, 'fast': fp, 'slow': sp,
                    'atr_sl': slm, 'atr_tp': tpm,
                    'net': m['total_return_pct'], 'sharpe': m['sharpe_ratio'],
                    'dd': m['max_drawdown_pct'], 'wr': m['win_rate'],
                    'pf': m['profit_factor'], 'trades': m['total_trades'],
                })

    # Sort by net return
    ma_cross_sweep.sort(key=lambda x: x['net'], reverse=True)
    if ma_cross_sweep:
        print(f"\n  Top 10 MA_Cross configs (by net%):")
        print(f"  {'Sym':8s} {'Fast':>4s} {'Slow':>4s} {'SLm':>5s} {'TPm':>5s} "
              f"{'Net%':>7s} {'Shp':>7s} {'DD%':>6s} {'WR%':>5s} {'PF':>6s} {'#T':>4s}")
        for r in ma_cross_sweep[:10]:
            print(f"  {r['symbol']:8s} {r['fast']:4d} {r['slow']:4d} "
                  f"{r['atr_sl']:4.1f}x {r['atr_tp']:4.1f}x "
                  f"{r['net']:+7.2f}% {r['sharpe']:+7.2f} {r['dd']:5.1f}% "
                  f"{r['wr']:4.0f}% {r['pf']:5.2f} {r['trades']:4d}")

        best_btc = max([r for r in ma_cross_sweep if r['symbol'] == 'BTC/USDT'], key=lambda x: x['net'])
        best_eth = max([r for r in ma_cross_sweep if r['symbol'] == 'ETH/USDT'], key=lambda x: x['net'])
        print(f"\n  ▶ Best BTC: fast={best_btc['fast']} slow={best_btc['slow']} "
              f"slm={best_btc['atr_sl']}x tpm={best_btc['atr_tp']}x → "
              f"net={best_btc['net']:+.2f}% sharpe={best_btc['sharpe']:+.2f} "
              f"dd={best_btc['dd']:.1f}% wr={best_btc['wr']:.0f}%")
        print(f"  ▶ Best ETH: fast={best_eth['fast']} slow={best_eth['slow']} "
              f"slm={best_eth['atr_sl']}x tpm={best_eth['atr_tp']}x → "
              f"net={best_eth['net']:+.2f}% sharpe={best_eth['sharpe']:+.2f} "
              f"dd={best_eth['dd']:.1f}% wr={best_eth['wr']:.0f}%")


    # ══════════════════════════════════════════════════════════════════════
    # Also test new strategy ideas
    # ══════════════════════════════════════════════════════════════════════

    print()
    print("=" * 80)
    print("NEW STRATEGY IDEAS — EXPLORATION")
    print("=" * 80)

    # Idea 1: EMA20 on BTC 1h (from Prometheus finding: EMA20 beats EMA100)
    if ('BTC/USDT', '1h') in data:
        df = data[('BTC/USDT', '1h')]
        # Test EMA20, SL=1%, TP=3%
        for ema, sl, tp, cd in [(20, 0.01, 0.03, 5), (20, 0.015, 0.04, 8), (50, 0.01, 0.03, 5), (100, 0.015, 0.04, 8)]:
            sig = trendfollow_signals(df, ema, sl, tp, cd)
            res = engine.run(df, sig, leverage=3)
            m = res['metrics']
            print(f"  TF EMA{ema} SL={sl*100:.1f}% TP={tp*100:.1f}% CD={cd} "
                  f"(BTC 1h): net={m['total_return_pct']:+.2f}% "
                  f"sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% "
                  f"wr={m['win_rate']:.0f}% #T={m['total_trades']}")

    # Idea 2: RSI_MR on ETH 1h
    if ('ETH/USDT', '1h') in data:
        df = data[('ETH/USDT', '1h')]
        for rsi_p, os_level, ob_level, sl, tp in [(14, 30, 70, 0.03, 0.06), (14, 25, 75, 0.02, 0.05), (7, 35, 65, 0.03, 0.06)]:
            sig = rsi_mr_signals(df, rsi_p, os_level, ob_level, 50, sl, tp, 5)
            res = engine.run(df, sig, leverage=3)
            m = res['metrics']
            print(f"  RSI_MR(rsi={rsi_p} os={os_level} ob={ob_level}) ETH 1h: net={m['total_return_pct']:+.2f}% "
                  f"sharpe={m['sharpe_ratio']:+.2f} dd={m['max_drawdown_pct']:.1f}% "
                  f"wr={m['win_rate']:.0f}% #T={m['total_trades']}")


    # ══════════════════════════════════════════════════════════════════════
    # Recommendations
    # ══════════════════════════════════════════════════════════════════════

    print()
    print("=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)

    recommendations = []

    for r in results_summary:
        if r['status'] != 'OK':
            continue
        m = r['metrics']
        name = r['name']

        # Check pause conditions
        should_pause = False
        pause_reasons = []
        if m['total_trades'] >= 3 and m['win_rate'] < 30:
            should_pause = True
            pause_reasons.append(f"win rate {m['win_rate']:.0f}% < 30%")
        if m['total_trades'] >= 3 and m['sharpe_ratio'] < 0:
            should_pause = True
            pause_reasons.append(f"Sharpe {m['sharpe_ratio']:+.3f} < 0")
        if m['total_trades'] >= 3 and m['total_return_pct'] < -5:
            should_pause = True
            pause_reasons.append(f"return {m['total_return_pct']:+.2f}% < -5%")

        if should_pause and r['enabled']:
            rec = f"⛔ PAUSE {name}: {', '.join(pause_reasons)}"
            print(f"  {rec}")
            recommendations.append(rec)
        elif should_pause:
            print(f"  ✓ {name}: already disabled ({', '.join(pause_reasons)})")
        elif r['enabled']:
            rec = f"✓ KEEP {name}: Sharpe={m['sharpe_ratio']:+.2f} WR={m['win_rate']:.0f}% Net={m['total_return_pct']:+.2f}%"
            print(f"  {rec}")
            recommendations.append(rec)

    # MA_Cross re-enable?
    if ma_cross_sweep:
        best_btc = max([r for r in ma_cross_sweep if r['symbol'] == 'BTC/USDT'], key=lambda x: x['net'])
        if best_btc['net'] > 1 and best_btc['sharpe'] > 0.5:
            rec = (f"💡 CONSIDER MA_Cross on BTC 1h: fast={best_btc['fast']} slow={best_btc['slow']} "
                   f"slm={best_btc['atr_sl']}x tpm={best_btc['atr_tp']}x → net={best_btc['net']:+.2f}% "
                   f"sharpe={best_btc['sharpe']:+.2f}")
            print(f"  {rec}")
            recommendations.append(rec)
        else:
            print(f"  ✗ MA_Cross not recommended — best BTC net={best_btc['net']:+.2f}% "
                  f"sharpe={best_btc['sharpe']:+.2f} (below threshold)")

    if not recommendations:
        recommendations.append("✓ All strategies performing within acceptable parameters.")


    # ══════════════════════════════════════════════════════════════════════
    # Save athena.json
    # ══════════════════════════════════════════════════════════════════════

    os.makedirs('.aether', exist_ok=True)

    athena_data = {
        'run_time': t0.isoformat(),
        'data_range_days': lookback_days,
        'db_total_klines': storage.get_db_stats()['tables']['klines'],
        'strategies': [],
        'ma_cross_sweep_top5': ma_cross_sweep[:5] if ma_cross_sweep else [],
        'recommendations': recommendations,
        'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
    }

    for r in results_summary:
        entry = {
            'name': r['name'],
            'enabled': r['enabled'],
            'symbol': r['symbol'],
            'tf': r['tf'],
            'status': r['status'],
        }
        if r['status'] == 'OK':
            m = r['metrics']
            entry['metrics'] = {
                'total_return_pct': m['total_return_pct'],
                'sharpe_ratio': m['sharpe_ratio'],
                'max_drawdown_pct': m['max_drawdown_pct'],
                'win_rate': m['win_rate'],
                'profit_factor': m['profit_factor'],
                'total_trades': m['total_trades'],
                'avg_win_pct': m['avg_win_pct'],
                'avg_loss_pct': m['avg_loss_pct'],
            }
            entry['flags'] = r['flags']
            entry['bars'] = r['bars']
        athena_data['strategies'].append(entry)

    with open('.aether/athena.json', 'w') as f:
        json.dump(athena_data, f, indent=2, default=str)

    print(f"\n💾 athena.json written ({len(results_summary)} strategies evaluated)")

    # Print summary for bulletin
    print()
    print("═══ BULLETIN SUMMARY ═══")
    now_str = t0.strftime('%m-%d %H:%M')
    for r in results_summary:
        if r['status'] != 'OK':
            continue
        m = r['metrics']
        icon = "🟢" if r['enabled'] and not r['flags'] else "🔴" if r['flags'] and r['enabled'] else "⏸️"
        print(f"{icon} {r['name']}: net={m['total_return_pct']:+.2f}% "
              f"sharpe={m['sharpe_ratio']:+.2f} wr={m['win_rate']:.0f}% "
              f"dd={m['max_drawdown_pct']:.1f}% #T={m['total_trades']}")
    print(f"📋 {len(recommendations)} recommendation(s)")
    print(f"🦉 Athena pulse #{1} — {now_str} UTC")
