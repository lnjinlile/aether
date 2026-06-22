#!/usr/bin/env python3
"""
Regime Monitor — Lightweight market regime classifier for live monitoring.

Detects whether ETH 1h is TRENDING or RANGING to protect RSI_MR_ETH
(the only viable strategy) from regime shifts. RSI_MR_ETH thrives in
RANGING markets; TRENDING → high risk of mean-reversion failure.

Model: LightGBM trained on 5 volatility + ADX + autocorr + volume features.
Horizon: 6 hours ahead, threshold: 1% move.

Usage: python3 regime_monitor.py
Output: .aether/state/regime_monitor.json + feed post on shift
"""
import sys, os, json, warnings
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib
from data.storage import MarketStorage
from config.settings import get_config

warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

MODEL_PATH = 'ml_alpha/regime_model.pkl'
STATE_PATH = '.aether/state/regime_monitor.json'
SYMBOL = 'ETH/USDT'
TF = '1h'
MIN_BARS = 120  # need enough history for rolling features


def build_regime_features(df):
    """Mirror of prometheus_regime_classifier feature builder."""
    close = df['close'].astype(float).values
    high = df['high'].astype(float).values
    low = df['low'].astype(float).values
    volume = df['volume'].astype(float).values
    n = len(close)

    feats = pd.DataFrame(index=df.index)
    ret = np.diff(np.log(close))
    ret = np.insert(ret, 0, 0.0)

    # Volatility
    for w in [5, 10, 20, 50]:
        feats[f'vol_{w}'] = pd.Series(ret).rolling(w).std().values
        feats[f'vol_{w}_norm'] = (feats[f'vol_{w}'] /
                                   (feats[f'vol_{w}'].rolling(50).mean().values + 1e-10))

    # High-low range
    hl_range = (high - low) / close
    for w in [5, 10, 20]:
        feats[f'hl_range_{w}'] = pd.Series(hl_range).rolling(w).mean().values

    # ADX
    for w in [14, 28]:
        plus_dm = np.maximum(high[1:] - high[:-1], 0)
        minus_dm = np.maximum(low[:-1] - low[1:], 0)
        tr = np.maximum(np.maximum(high[1:] - low[1:],
                                    np.abs(high[1:] - close[:-1])),
                         np.abs(low[1:] - close[:-1]))
        atr = pd.Series(np.insert(tr, 0, np.nan)).ewm(span=w, adjust=False).mean().values
        plus_di = (pd.Series(np.insert(plus_dm, 0, np.nan))
                   .ewm(span=w, adjust=False).mean().values / atr * 100)
        minus_di = (pd.Series(np.insert(minus_dm, 0, np.nan))
                    .ewm(span=w, adjust=False).mean().values / atr * 100)
        feats[f'adx_{w}'] = (np.abs(plus_di - minus_di) /
                              (plus_di + minus_di + 1e-10) * 100)
        feats[f'adx_{w}_norm'] = (feats[f'adx_{w}'] /
                                   (feats[f'adx_{w}'].rolling(50).mean().values + 1e-10))

    # MA dispersion
    for w_fast, w_slow in [(5, 20), (10, 50), (20, 100)]:
        ma_fast = pd.Series(close).rolling(w_fast).mean().values
        ma_slow = pd.Series(close).rolling(w_slow).mean().values
        feats[f'ma_disp_{w_fast}_{w_slow}'] = np.abs(ma_fast - ma_slow) / close

    # Autocorrelation
    for w in [10, 20]:
        feats[f'autocorr_{w}'] = pd.Series(ret).rolling(w).apply(
            lambda x: x.autocorr() if len(x) > 2 else 0, raw=False
        ).values

    # Volume
    vol_ma20 = pd.Series(volume).rolling(20).mean().values
    feats['vol_ratio'] = volume / (vol_ma20 + 1e-10)
    feats['vol_trend'] = (pd.Series(volume).rolling(5).mean().values /
                           (vol_ma20 + 1e-10))

    # Kurtosis
    for w in [20, 50]:
        feats[f'kurtosis_{w}'] = pd.Series(ret).rolling(w).kurt().values

    feats = feats.replace([np.inf, -np.inf], np.nan)
    feats = feats.ffill().bfill().fillna(0.0)
    return feats


def main():
    now = datetime.now(timezone.utc)

    # Load model
    if not os.path.exists(MODEL_PATH):
        print(f"❌ Model not found: {MODEL_PATH}", file=sys.stderr)
        sys.exit(1)

    model_data = joblib.load(MODEL_PATH)
    model = model_data['model']
    feature_names = model_data['feature_names']
    horizon = model_data.get('horizon', 6)
    threshold = model_data.get('threshold', 0.01)

    # Load data
    cfg = get_config()
    storage = MarketStorage(cfg.db_path)
    df = storage.load_klines(SYMBOL, TF)

    if df is None or len(df) < MIN_BARS:
        print(f"❌ Insufficient data: {len(df) if df is not None else 0} bars", file=sys.stderr)
        sys.exit(1)

    df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    df.set_index('open_time', inplace=True)
    df.sort_index(inplace=True)

    # Build features
    feats = build_regime_features(df)

    # Predict
    X_latest = feats[feature_names].iloc[-1:].values
    proba = model.predict_proba(X_latest)[0]
    pred = model.predict(X_latest)[0]

    regime = "TRENDING" if pred == 1 else "RANGING"
    p_trending = float(proba[1])
    p_ranging = float(proba[0])

    # Recent history (last 24 bars)
    X_recent = feats[feature_names].iloc[-24:].values
    recent_preds = model.predict(X_recent)
    recent_probas = model.predict_proba(X_recent)
    recent_regimes = ["TRENDING" if p == 1 else "RANGING" for p in recent_preds]

    # Count recent trending bars
    trending_count = sum(1 for r in recent_regimes[-12:] if r == "TRENDING")
    ranging_count = 12 - trending_count

    # Load previous state for regime shift detection
    prev_regime = None
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                prev = json.load(f)
            prev_regime = prev.get('regime')
        except Exception:
            pass

    # Build output
    result = {
        'updated_at': now.isoformat(),
        'symbol': SYMBOL,
        'tf': TF,
        'horizon_hours': horizon,
        'threshold_pct': threshold,
        'regime': regime,
        'p_trending': round(p_trending, 4),
        'p_ranging': round(p_ranging, 4),
        'confidence': round(max(p_trending, p_ranging), 4),
        'recent_12h': {
            'trending_bars': trending_count,
            'ranging_bars': ranging_count,
            'dominant': 'TRENDING' if trending_count >= 6 else 'RANGING',
            'trend_ratio': round(trending_count / 12, 3),
        },
        'last_5_regimes': recent_regimes[-5:],
        'regime_shift': (prev_regime is not None and prev_regime != regime),
        'prev_regime': prev_regime,
        'model': {
            'test_acc': model_data.get('test_acc'),
            'train_acc': model_data.get('train_acc'),
            'features': len(feature_names),
        },
        'rsi_mr_eth_favorable': regime == 'RANGING',
    }

    # Write state
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    # Summary
    favorable = "✅ FAVORABLE" if regime == 'RANGING' else "⚠️ RISK"
    print(f"Regime: {regime} {favorable} | P(Trend)={p_trending:.3f} P(Range)={p_ranging:.3f} | "
          f"Recent 12h: {trending_count}T/{ranging_count}R")

    # Post to feed on regime shift
    if result['regime_shift']:
        shift_msg = f"REGIME SHIFT: {prev_regime} → {regime} | P(Trend)={p_trending:.3f} | " \
                    f"RSI_MR_ETH {'FAVORABLE' if regime=='RANGING' else 'AT RISK'}"
        import subprocess
        subprocess.run(['python3', '.aether/feed.py', 'post', 'prometheus',
                        'alert', shift_msg], capture_output=True)
        print(f"📢 Posted regime shift alert to feed")

    return result


if __name__ == '__main__':
    main()
