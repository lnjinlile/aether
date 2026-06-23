"""Quick sweep: donchian_mr_signals with spread_z_entry filter."""
import sys, os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from backtest.signal_gen import donchian_mr_signals
from backtest.sweep_utils import load_data, compute_metrics_from_signals

if __name__ == '__main__':
    # Load ETH 1h data with symbol in attrs
    from data.db import get_market_db
    import pandas as pd
    
    db_path = os.path.join(os.path.dirname(__file__), 'data', 'market.db')
    db = get_market_db(db_path)
    
    df = pd.read_sql_query(
        "SELECT open_time as timestamp, open, high, low, close, volume "
        "FROM klines WHERE symbol='ETH/USDT' AND timeframe='1h' ORDER BY open_time",
        db, index_col='timestamp', parse_dates=['timestamp'])
    db.close()
    
    df.attrs['symbol'] = 'ETH/USDT'
    
    print(f"ETH 1h data: {len(df)} bars")
    
    # Baseline (no filter)
    sigs_base = donchian_mr_signals(
        df, donchian_period=8, rsi_period=14,
        oversold=30, overbought=75, exit_level=50,
        stop_loss_pct=0.015, take_profit_pct=0.015,
        cooldown_bars=3, volume_filter=1.2,
        spread_z_entry=0.0)
    
    # With spread filter
    sigs_filtered = donchian_mr_signals(
        df, donchian_period=8, rsi_period=14,
        oversold=30, overbought=75, exit_level=50,
        stop_loss_pct=0.015, take_profit_pct=0.015,
        cooldown_bars=3, volume_filter=1.2,
        spread_z_entry=-0.5)
    
    # Compute basic metrics
    from backtest.sweep_utils import compute_metrics_from_signals
    m_base = compute_metrics_from_signals(df, sigs_base)
    m_filt = compute_metrics_from_signals(df, sigs_filtered)
    
    print(f"\nBaseline (no filter): SR={m_base.get('sharpe',0):.4f} Ret={m_base.get('return_pct',0):.1f}% "
          f"DD={m_base.get('max_dd',0):.1f}% WR={m_base.get('win_rate',0):.1f}% T={m_base.get('trades',0)}")
    print(f"Spread z=-0.5:        SR={m_filt.get('sharpe',0):.4f} Ret={m_filt.get('return_pct',0):.1f}% "
          f"DD={m_filt.get('max_dd',0):.1f}% WR={m_filt.get('win_rate',0):.1f}% T={m_filt.get('trades',0)}")
    
    # Also test BandMR_ETH params with z=-1.0
    sigs_band = donchian_mr_signals(
        df, donchian_period=8, rsi_period=14,
        oversold=30, overbought=75, exit_level=50,
        stop_loss_pct=0.015, take_profit_pct=0.015,
        cooldown_bars=3, volume_filter=1.2,
        spread_z_entry=-1.0)
    m_band = compute_metrics_from_signals(df, sigs_band)
    print(f"Spread z=-1.0:        SR={m_band.get('sharpe',0):.4f} Ret={m_band.get('return_pct',0):.1f}% "
          f"DD={m_band.get('max_dd',0):.1f}% WR={m_band.get('win_rate',0):.1f}% T={m_band.get('trades',0)}")
