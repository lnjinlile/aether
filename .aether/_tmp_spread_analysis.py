import sqlite3, numpy as np

db = sqlite3.connect('data/market.db')

btc_rows = db.execute("SELECT open_time, close FROM klines WHERE symbol='BTC/USDT' AND timeframe='1h' ORDER BY open_time").fetchall()
eth_rows = db.execute("SELECT open_time, close FROM klines WHERE symbol='ETH/USDT' AND timeframe='1h' ORDER BY open_time").fetchall()

print(f'BTC 1h bars: {len(btc_rows)}, ETH 1h bars: {len(eth_rows)}')

btc_dict = {r[0]: r[1] for r in btc_rows}
eth_dict = {r[0]: r[1] for r in eth_rows}

common_times = sorted(set(btc_dict.keys()) & set(eth_dict.keys()))
print(f'Common bars: {len(common_times)}')

spreads = np.array([eth_dict[t] / btc_dict[t] for t in common_times])

mean_s = np.mean(spreads)
std_s = np.std(spreads)
print(f'ETH/BTC ratio: mean={mean_s:.6f}, std={std_s:.6f}')
cur_z = (spreads[-1] - mean_s) / std_s
print(f'Current: {spreads[-1]:.6f} ({cur_z:.2f} sigma)')

# Hurst exponent
if len(spreads) > 120:
    recent = spreads[-200:]
    lags = [2, 4, 8, 16, 32, 64]
    tau = []
    for lag in lags:
        if lag < len(recent) / 2:
            diff = np.subtract(recent[lag:], recent[:-lag])
            tau.append(np.sqrt(np.std(diff)))
    if len(tau) > 3:
        log_lags = np.log(lags[:len(tau)])
        log_tau = np.log(tau)
        A = np.vstack([log_lags, np.ones(len(log_lags))]).T
        hurst, _ = np.linalg.lstsq(A, log_tau, rcond=None)[0]
        label = "mean-reverting" if hurst < 0.5 else "trending"
        print(f'Hurst (200-bar): {hurst:.3f} ({label})')
    
    # Half-life
    z = recent - mean_s
    z_lag = z[:-1]
    z_diff = np.diff(z)
    A_half = np.vstack([z_lag, np.ones(len(z_lag))]).T
    theta, _ = np.linalg.lstsq(A_half, z_diff, rcond=None)[0]
    if theta < 0:
        hl = -np.log(2) / theta
        print(f'Half-life: {hl:.1f} bars ({hl:.1f} hours)')
    else:
        print(f'Theta={theta:.4f} — not mean-reverting')

# Distribution
print(f'Min ratio: {np.min(spreads):.6f}, Max: {np.max(spreads):.6f}')
z_scores = (spreads - mean_s) / std_s
print(f'|Z|>2: {np.mean(np.abs(z_scores)>2):.1%}, |Z|>1.5: {np.mean(np.abs(z_scores)>1.5):.1%}')

# First/last dates
from datetime import datetime, timezone
print(f'Date range: {datetime.fromtimestamp(common_times[0]/1000, tz=timezone.utc)} to {datetime.fromtimestamp(common_times[-1]/1000, tz=timezone.utc)}')

db.close()
