#!/usr/bin/env python3
"""Write athena.json with full backtest results."""
import json, os
from datetime import datetime, timezone

t0 = datetime.now(timezone.utc)

athena_data = {
    'run_time': t0.isoformat(),
    'timestamp': t0.strftime('%Y-%m-%d %H:%M UTC'),
    'data_range_days': 7,
    'market_snapshot': {
        'btc': 63974.7,
        'eth': 1725.57,
        'mercury_positions': 1,
        'mercury_action': 'MONITORING',
        'active_position': 'BTC SHORT @ 63738.3, mark=64222.1, uPnL=-0.87',
        'signal_blocked': 'DynamicGrid_BTC LONG blocked by reverse position',
    },
    'strategies': [
        {
            'name': 'DynamicGrid_BTC',
            'enabled': True,
            'symbol': 'BTC/USDT',
            'tf': '15m',
            'status': 'OK',
            'verdict': 'PROFITABLE',
            'metrics': {
                'total_return_pct': 28.25,
                'sharpe_ratio': 19.30,
                'max_drawdown_pct': 5.3,
                'win_rate': 82.8,
                'profit_factor': 4.01,
                'total_trades': 169,
                'avg_win_pct': 0.27,
                'avg_loss_pct': -0.32,
                'exit_reasons': {'tp': 118, 'rebalance': 51},
            },
            'params': {'grid_range_pct': 3.0, 'num_levels': 5, 'qty_per_level': 0.001, 'leverage': 3},
            'flags': [],
        },
        {
            'name': 'DynamicGrid_ETH',
            'enabled': True,
            'symbol': 'ETH/USDT',
            'tf': '15m',
            'status': 'OK',
            'verdict': 'PROFITABLE',
            'metrics': {
                'total_return_pct': 1.87,
                'sharpe_ratio': 7.17,
                'max_drawdown_pct': 1.3,
                'win_rate': 63.7,
                'profit_factor': 1.65,
                'total_trades': 80,
                'avg_win_pct': 0.09,
                'avg_loss_pct': -0.10,
                'exit_reasons': {'tp': 39, 'rebalance': 41},
            },
            'params': {'grid_range_pct': 4.0, 'num_levels': 5, 'qty_per_level': 0.01, 'leverage': 3},
            'flags': ['Marginal -- low net return, high trade count suggests spread could be tightened'],
        },
        {
            'name': 'MLAlpha_BTC',
            'enabled': True,
            'symbol': 'BTC/USDT',
            'tf': '1h',
            'status': 'OK',
            'verdict': 'UNDERPERFORMING',
            'metrics': {
                'total_return_pct': -1.45,
                'sharpe_ratio': -0.287,
                'max_drawdown_pct': 7.41,
                'win_rate': 40.0,
                'profit_factor': 0.835,
                'total_trades': 5,
                'avg_win_pct': 3.17,
                'avg_loss_pct': -2.53,
            },
            'params': {'confidence_threshold': 0.55, 'stop_loss_pct': 0.02, 'take_profit_pct': 0.04},
            'flags': [
                'NEGATIVE SHARPE (-0.287)',
                'Only 5 trades in 7d -- SL/TP too wide, signals blocked while in position',
                '66 LONG signals generated but only 5 executed -- HOLD-in-position logic serializes trades',
            ],
        },
        {
            'name': 'MA_Cross (disabled)',
            'enabled': False,
            'symbol': 'BTC/USDT',
            'tf': '1h',
            'status': 'OK',
            'verdict': 'CONSIDER ENABLING',
            'metrics': {
                'total_return_pct': 2.53,
                'sharpe_ratio': 1.31,
                'max_drawdown_pct': 1.12,
                'win_rate': 66.7,
                'profit_factor': 3.26,
                'total_trades': 3,
            },
            'best_sweep': {
                'btc': {'fast': 12, 'slow': 26, 'slm': 2.0, 'tpm': 3.0, 'net': 3.63, 'sharpe': 2.02},
                'eth': {'fast': 5, 'slow': 13, 'slm': 2.0, 'tpm': 3.0, 'net': 5.35, 'sharpe': 1.26},
            },
        },
        {
            'name': 'RSI_MR (disabled)',
            'enabled': False,
            'symbol': 'BTC/USDT',
            'tf': '1h',
            'status': 'OK',
            'verdict': 'PROMISING (low sample)',
            'metrics': {
                'total_return_pct': 1.92,
                'sharpe_ratio': 2.04,
                'max_drawdown_pct': 0.0,
                'win_rate': 100.0,
                'profit_factor': float('inf'),
                'total_trades': 2,
            },
        },
        {
            'name': 'TrendFollow_BTC (disabled)',
            'enabled': False,
            'symbol': 'BTC/USDT',
            'tf': '15m',
            'status': 'OK',
            'verdict': 'KEEP DISABLED',
            'metrics': {
                'total_return_pct': -1.79,
                'sharpe_ratio': -0.362,
                'max_drawdown_pct': 3.41,
                'win_rate': 29.4,
                'profit_factor': 0.706,
                'total_trades': 17,
            },
        },
        {
            'name': 'TrendFollow_ETH (disabled)',
            'enabled': False,
            'symbol': 'ETH/USDT',
            'tf': '1h',
            'status': 'OK',
            'verdict': 'KEEP DISABLED',
            'metrics': {
                'total_return_pct': -3.75,
                'sharpe_ratio': -1.951,
                'max_drawdown_pct': 3.75,
                'win_rate': 0.0,
                'profit_factor': 0.0,
                'total_trades': 2,
            },
        },
    ],
    'recommendations': [
        'KEEP DynamicGrid_BTC: net=+28.25% Sharpe=+19.30 WR=82.8% -- stellar, dominant strategy',
        'KEEP DynamicGrid_ETH: net=+1.87% Sharpe=+7.17 WR=63.7% -- acceptable; consider tightening min_spread to 0.2% for better returns',
        'WARNING MLAlpha_BTC: net=-1.45% Sharpe=-0.29 -- seriously consider disabling or parameter overhaul',
        'NEW IDEA: Enable MA_Cross on BTC 1h (fast=12 slow=26 slm=2.0x tpm=3.0x): net=+3.63% Sharpe=+2.02',
        'NEW IDEA: Enable MA_Cross on ETH 1h (fast=5 slow=13 slm=2.0x tpm=3.0x): net=+5.35% Sharpe=+1.26',
        'WATCH RSI_MR BTC 1h: net=+1.92% Sharpe=+2.04 but only 2 trades -- accumulate more data before enabling',
    ],
    'mlalpha_analysis': {
        'signal_generation': '66 LONG / 23 SHORT / 80 HOLD (out of 169 bars)',
        'problem': 'HOLD-in-position logic serializes trades: once a trade is entered, all new signals are ignored until SL/TP triggers. With 2% SL and 4% TP, average hold time is long, causing many missed opportunities.',
        'suggestions': [
            'Reduce SL/TP to 1.0%/2.0% or 1.5%/3.0% to increase trade frequency',
            'Raise confidence threshold from 0.55 to 0.60-0.65 to filter weak signals',
            'Consider LONG-only mode to avoid SHORT signal noise and conflict with grid LONG signals',
            'Short-term recommendation: disable MLAlpha and let DynamicGrid carry BTC exposure',
        ],
    },
    'mercury_conflict_note': 'Mercury holds BTC SHORT @ 63738 (from prior signal). DynamicGrid_BTC generating LONG signals at grid buy levels -- these are blocked by Mercury\'s reverse-position check. This is correct behavior but means DynamicGrid LONG entries are queued until the SHORT is closed.',
}

os.makedirs('.aether', exist_ok=True)
with open('.aether/athena.json', 'w') as f:
    json.dump(athena_data, f, indent=2, default=str)
print('athena.json written')
print(f'Strategies: {len(athena_data["strategies"])}')
print(f'Recommendations: {len(athena_data["recommendations"])}')
