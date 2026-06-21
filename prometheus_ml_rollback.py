#!/usr/bin/env python3
"""
Prometheus — ML Rolling Backtest & Market State Switching

滚动回测框架：
  - 滚动窗口训练/验证 ML 模型
  - 市场状态分类 (趋势/震荡/高波动)
  - Walk-Forward Efficiency (WFE) 评估

Usage:
    python3 prometheus_ml_rollback.py
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
import numpy as np
import pandas as pd

from data.storage import MarketStorage
from config.settings import get_config
from ml_alpha.features import FeatureEngineer
from ml_alpha.trainer import AlphaModel
from backtest.engine import BacktestEngine
from strategy.base import SignalType


# ═══════════════════════════════════════════════════════════════
# Market State Classifier
# ═══════════════════════════════════════════════════════════════

def classify_market_state(df: pd.DataFrame, lookback: int = 50) -> pd.Series:
    """
    将市场分为三种状态：
      - TREND:   趋势市 (ADX > 25 或 价格远离MA)
      - RANGE:   震荡市 (ADX < 20 且 价格在BB内)
      - VOLATILE: 高波动 (ATR比率 > 历史2σ)

    返回每根bar的市场状态 Series。
    """
    close = df['close'].astype(float)
    high = df['high'].astype(float)
    low = df['low'].astype(float)

    # ADX (简化版，无+DI/-DI，用trend_strength替代)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean()

    # Trend strength: |price - MA50| / ATR
    ma50 = close.rolling(50).mean()
    trend_strength = ((close - ma50).abs() / atr.replace(0, np.nan))

    # Bollinger Band width (volatility proxy)
    bb_std = close.rolling(20).std()
    bb_width = (4 * bb_std) / close.rolling(20).mean()

    # ATR ratio relative to history
    atr_ma50 = atr.rolling(50).mean()
    atr_std50 = atr.rolling(50).std()
    atr_zscore = (atr - atr_ma50) / atr_std50.replace(0, np.nan)

    states = pd.Series('RANGE', index=df.index)

    # TREND: strong directional move
    states[trend_strength > 2.5] = 'TREND'

    # VOLATILE: ATR spike
    states[atr_zscore > 2.0] = 'VOLATILE'

    # FIX: TREND takes priority over VOLATILE when both true
    # (TREND + VOLATILE = TREND)

    # Fill NaN edges
    states = states.ffill().bfill()

    return states


# ═══════════════════════════════════════════════════════════════
# Rolling Walk-Forward Backtest for ML Strategies
# ═══════════════════════════════════════════════════════════════

def ml_walk_forward(
    df: pd.DataFrame,
    train_days: int = 60,
    test_days: int = 15,
    step_days: int = 15,
    min_train_bars: int = 200,
    min_test_bars: int = 50,
    confidence_threshold: float = 0.55,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.04,
    n_estimators: int = 200,
    max_depth: int = 3,
    learning_rate: float = 0.03,
) -> dict:
    """
    ML 滚动窗口 Walk-Forward 验证。

    每个窗口：
      1. 用 train 数据构建特征 + 训练 LightGBM
      2. 用 test 数据生成信号
      3. 回测 test 段
      4. 窗口向前滑动

    Returns:
        dict with windows[], total_oos_return, total_oos_sharpe, WFE, etc.
    """
    engineer = FeatureEngineer()
    engine = BacktestEngine(initial_capital=10000.0, commission=0.0004, slippage=0.0001)

    train_td = pd.Timedelta(days=train_days)
    test_td = pd.Timedelta(days=test_days)
    step_td = pd.Timedelta(days=step_days)

    start_time = df.index[0] + train_td
    end_time = df.index[-1]

    windows = []
    current_start = start_time

    while current_start + test_td <= end_time:
        train_end = current_start
        test_start = current_start
        test_end = current_start + test_td

        train_df = df[df.index < train_end]
        test_df = df[(df.index >= test_start) & (df.index < test_end)]

        if len(train_df) < min_train_bars or len(test_df) < min_test_bars:
            current_start += step_td
            continue

        # ── Build features on train data ──
        try:
            X_train, y_train = engineer.build_features(train_df)
            X_test, y_test = engineer.build_features(test_df)
        except Exception as e:
            windows.append({
                'train_start': str(train_df.index[0]),
                'train_end': str(train_df.index[-1]),
                'test_start': str(test_df.index[0]),
                'test_end': str(test_df.index[-1]),
                'status': 'FEATURE_ERROR',
                'error': str(e),
            })
            current_start += step_td
            continue

        if len(X_train) < 100 or len(X_test) < 20:
            current_start += step_td
            continue

        # ── Train model ──
        model = AlphaModel(
            n_estimators=n_estimators,
            max_depth=max_depth,
            learning_rate=learning_rate,
        )
        try:
            train_acc = model.train(X_train, y_train)
        except Exception as e:
            windows.append({
                'train_start': str(train_df.index[0]),
                'train_end': str(train_df.index[-1]),
                'test_start': str(test_df.index[0]),
                'test_end': str(test_df.index[-1]),
                'status': 'TRAIN_ERROR',
                'error': str(e),
            })
            current_start += step_td
            continue

        # ── Generate signals for test period ──
        signals = np.zeros(len(test_df), dtype=int)
        pos = 0
        entry_price = 0.0

        for i in range(len(test_df)):
            row = X_test.iloc[[i]]
            try:
                prob = float(model.predict(row)[0])
            except Exception:
                continue

            price = float(test_df['close'].iloc[i])

            # Exit logic (SL/TP)
            if pos == 1:
                if price <= entry_price * (1 - stop_loss_pct):
                    signals[i] = 0; pos = 0; continue
                elif price >= entry_price * (1 + take_profit_pct):
                    signals[i] = 0; pos = 0; continue
                else:
                    signals[i] = 1; continue
            elif pos == -1:
                if price >= entry_price * (1 + stop_loss_pct):
                    signals[i] = 0; pos = 0; continue
                elif price <= entry_price * (1 - take_profit_pct):
                    signals[i] = 0; pos = 0; continue
                else:
                    signals[i] = -1; continue

            # Entry logic
            if pos == 0:
                if prob > confidence_threshold:
                    pos = 1; entry_price = price; signals[i] = 1
                elif prob < (1 - confidence_threshold):
                    pos = -1; entry_price = price; signals[i] = -1

        sig_series = pd.Series(signals, index=test_df.index)

        # ── Backtest ──
        result = engine.run(test_df, sig_series, n_trials=100)
        m = result['metrics']

        # ── Market state distribution in this window ──
        states = classify_market_state(test_df)
        state_dist = states.value_counts().to_dict()

        window = {
            'train_start': str(train_df.index[0]),
            'train_end': str(train_df.index[-1]),
            'test_start': str(test_df.index[0]),
            'test_end': str(test_df.index[-1]),
            'train_bars': len(train_df),
            'test_bars': len(test_df),
            'train_acc': round(train_acc, 4),
            'test_acc': round(model.model.score(X_test, y_test), 4),
            'net_return_pct': round(m['total_return_pct'], 2),
            'sharpe': round(m['sharpe_ratio'], 2),
            'max_dd_pct': round(m['max_drawdown_pct'], 2),
            'win_rate': round(m['win_rate'], 1),
            'total_trades': m['total_trades'],
            'profit_factor': round(m['profit_factor'], 2),
            'status': 'OK',
            'state_distribution': state_dist,
        }
        windows.append(window)

        current_start += step_td

    # ── Aggregate ──
    ok_windows = [w for w in windows if w['status'] == 'OK']
    n_windows = len(ok_windows)
    n_passed = sum(1 for w in ok_windows if w['net_return_pct'] > 0)

    if ok_windows:
        total_oos_return = sum(w['net_return_pct'] for w in ok_windows)
        avg_sharpe = np.mean([w['sharpe'] for w in ok_windows])
        total_trades = sum(w['total_trades'] for w in ok_windows)
        avg_win_rate = np.mean([w['win_rate'] for w in ok_windows])

        # WFE = OOS return / IS return proxy (train accuracy bias)
        avg_train_acc = np.mean([w['train_acc'] for w in ok_windows])
        wfe = total_oos_return / max(avg_train_acc * n_windows, 0.01)
    else:
        total_oos_return = 0
        avg_sharpe = 0
        total_trades = 0
        avg_win_rate = 0
        wfe = 0

    return {
        'windows': windows,
        'n_windows': n_windows,
        'n_passed': n_passed,
        'pass_rate': round(n_passed / max(n_windows, 1) * 100, 1),
        'total_oos_return_pct': round(total_oos_return, 2),
        'avg_window_return_pct': round(total_oos_return / max(n_windows, 1), 2),
        'avg_sharpe': round(avg_sharpe, 2),
        'avg_win_rate': round(avg_win_rate, 1),
        'total_trades': total_trades,
        'wfe': round(wfe, 2),
        'verdict': _verdict(n_windows, n_passed, total_oos_return, avg_sharpe),
    }


def _verdict(n_windows: int, n_passed: int, total_return: float, avg_sharpe: float) -> str:
    """判断策略是否可通过 WF 验证"""
    if n_windows < 2:
        return "BROKEN (窗口不足，无法评估)"
    if n_passed == n_windows and total_return > 0 and avg_sharpe > 1.0:
        return "PASS (全窗口盈利，可启用)"
    elif n_passed >= n_windows * 0.6 and total_return > 0 and avg_sharpe > 0.5:
        return "MARGINAL (多数窗口盈利，谨慎启用)"
    elif total_return <= 0:
        return "FAIL (OOS 净亏损)"
    else:
        return f"INCONCLUSIVE ({n_passed}/{n_windows} 窗口盈利)"


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("🔥 Prometheus — ML 滚动回测 + 市场状态分类")
    print("=" * 70)
    t0 = datetime.now(timezone.utc)
    print(f"Run: {t0.strftime('%Y-%m-%d %H:%M UTC')}\n")

    cfg = get_config()
    storage = MarketStorage(cfg.db_path)

    # ── Load BTC 1h data ──
    print("[1/4] Loading BTC/USDT 1h data...")
    df = storage.load_klines('BTC/USDT', '1h')
    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)
    days = (df.index[-1] - df.index[0]).days
    print(f"      {len(df)} bars, {days}d [{df.index[0].date()} → {df.index[-1].date()}]")

    # ── Market state analysis ──
    print("\n[2/4] Market State Classification...")
    states = classify_market_state(df)
    state_counts = states.value_counts()
    print(f"      TREND:    {state_counts.get('TREND', 0):5d} bars ({state_counts.get('TREND',0)/len(df)*100:.1f}%)")
    print(f"      RANGE:    {state_counts.get('RANGE', 0):5d} bars ({state_counts.get('RANGE',0)/len(df)*100:.1f}%)")
    print(f"      VOLATILE: {state_counts.get('VOLATILE', 0):5d} bars ({state_counts.get('VOLATILE',0)/len(df)*100:.1f}%)")

    # ── Rolling WF backtest ──
    print("\n[3/4] ML Walk-Forward Rolling Backtest...")
    print(f"      Config: 60d train / 15d test / 15d step, threshold={0.55}, SL={0.02}, TP={0.04}")
    result = ml_walk_forward(
        df,
        train_days=60,
        test_days=15,
        step_days=15,
        confidence_threshold=0.55,
        stop_loss_pct=0.02,
        take_profit_pct=0.04,
    )

    print(f"\n      ── WF Results ──")
    print(f"      Total windows:     {result['n_windows']}")
    print(f"      Passed windows:    {result['n_passed']}/{result['n_windows']} ({result['pass_rate']}%)")
    print(f"      Total OOS return:  {result['total_oos_return_pct']:+.2f}%")
    print(f"      Avg OOS return:    {result['avg_window_return_pct']:+.2f}%/window")
    print(f"      Avg Sharpe:        {result['avg_sharpe']:+.2f}")
    print(f"      Avg Win Rate:      {result['avg_win_rate']:.1f}%")
    print(f"      Total trades:      {result['total_trades']}")
    print(f"      WFE:               {result['wfe']:.2f}")
    print(f"      Verdict:           {result['verdict']}")

    if result['windows']:
        print(f"\n      Per-window breakdown:")
        print(f"      {'Window':^6s} {'Train':^12s} {'Test':^12s} {'Net%':>7s} {'Shp':>6s} {'DD%':>6s} {'WR%':>5s} {'#T':>3s} {'TrAcc':>6s}")
        for i, w in enumerate(result['windows']):
            if w['status'] == 'OK':
                print(f"      [{i+1:2d}]   {w['train_bars']:4d}b      {w['test_bars']:4d}b    "
                      f"{w['net_return_pct']:+6.1f}% {w['sharpe']:+5.2f} {w['max_dd_pct']:5.1f}% "
                      f"{w['win_rate']:4.0f}% {w['total_trades']:3d} {w['train_acc']:.3f}")
            else:
                print(f"      [{i+1:2d}]   {'--':>4s}      {'--':>4s}    {w['status']} {w.get('error','')[:40]}")

    # ── State-stratified performance ──
    print("\n[4/4] State-conditional performance analysis...")
    state_perf = {'TREND': [], 'RANGE': [], 'VOLATILE': []}
    for w in result['windows']:
        if w['status'] != 'OK':
            continue
        # Determine dominant state in this window
        sd = w.get('state_distribution', {})
        dominant = max(sd, key=sd.get) if sd else 'RANGE'
        state_perf[dominant].append(w['net_return_pct'])

    print(f"      State-conditional returns:")
    for state, returns in state_perf.items():
        if returns:
            avg_ret = np.mean(returns)
            n = len(returns)
            pos_pct = sum(1 for r in returns if r > 0) / n * 100
            print(f"        {state:10s}: avg={avg_ret:+.1f}% (n={n}, {pos_pct:.0f}% positive)")
        else:
            print(f"        {state:10s}: no data")

    # ── Save results ──
    os.makedirs('.aether', exist_ok=True)
    ml_data = {
        'run_time': t0.isoformat(),
        'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
        'data_days': days,
        'data_bars': len(df),
        'market_states': {k: int(v) for k, v in state_counts.items()},
        'walk_forward': {
            'config': {'train_days': 60, 'test_days': 15, 'step_days': 15, 'threshold': 0.55},
            'n_windows': result['n_windows'],
            'n_passed': result['n_passed'],
            'pass_rate': result['pass_rate'],
            'total_oos_return_pct': result['total_oos_return_pct'],
            'avg_sharpe': result['avg_sharpe'],
            'wfe': result['wfe'],
            'verdict': result['verdict'],
            'windows': result['windows'],
        },
        'state_performance': {k: {'avg_return': round(np.mean(v), 2) if v else 0, 'count': len(v)} for k, v in state_perf.items()},
    }
    with open('.aether/prometheus_ml.json', 'w') as f:
        json.dump(ml_data, f, indent=2, default=str)

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    print(f"\n⏱️  Runtime: {elapsed:.1f}s")
    print("💾 prometheus_ml.json written")
    print("🔥 ML Rolling Backtest complete")

    return result, state_perf


if __name__ == '__main__':
    result, state_perf = main()
