#!/usr/bin/env python3
"""Sync local trades_log with exchange reality (Binance testnet futures).

Usage:
    python scripts/sync_exchange.py          # sync only
    python scripts/sync_exchange.py --force  # sync + force-close stale DB records
    python scripts/sync_exchange.py --dry-run # print what would change

This script is the single source of truth for reconciling local DB with exchange.
All agents should call this before reading/writing trades_log.
"""
import os, sys, json, hmac, hashlib, time, argparse, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / '.env')

API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
BASE = 'https://testnet.binancefuture.com'
DB_PATH = PROJECT_ROOT / 'data' / 'market.db'


def signed_request(endpoint, params=None):
    if params is None:
        params = {}
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 5000
    query = '&'.join([f'{k}={v}' for k, v in sorted(params.items())])
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f'{BASE}{endpoint}?{query}&signature={sig}'
    resp = __import__('requests').get(url, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_exchange_state():
    """Return exchange ground truth."""
    pos_data = signed_request('/fapi/v2/positionRisk')
    acct = signed_request('/fapi/v2/account')
    
    positions = []
    for p in pos_data:
        amt = float(p['positionAmt'])
        if amt != 0:
            positions.append({
                'symbol': p['symbol'],
                'side': 'SHORT' if amt < 0 else 'LONG',
                'quantity': abs(amt),
                'entry_price': float(p['entryPrice']),
                'mark_price': float(p['markPrice']),
                'unrealized_pnl': float(p['unRealizedProfit']),
                'leverage': int(p['leverage']),
                'liquidation_price': float(p['liquidationPrice']),
            })
    
    balance = {
        'total': float(acct['totalWalletBalance']),
        'available': float(acct['availableBalance']),
        'margin': float(acct['totalMarginBalance']),
        'unrealized_pnl': float(acct['totalUnrealizedProfit']),
    }
    
    return positions, balance


def get_db_state():
    """Return local DB state."""
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA busy_timeout=10000")  # 10s timeout for concurrent access
    cur = db.cursor()
    cur.execute("""
        SELECT id, symbol, side, entry_price, quantity, status, entry_time
        FROM trades_log WHERE status='OPEN'
    """)
    cols = ['id', 'symbol', 'side', 'entry_price', 'quantity', 'status', 'entry_time']
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    db.close()
    return rows


def reconcile(dry_run=False):
    """Reconcile local DB with exchange."""
    positions, balance = get_exchange_state()
    db_open = get_db_state()
    
    changes = []
    
    # Map exchange positions by symbol+side
    ex_map = {}
    for p in positions:
        key = f"{p['symbol']}:{p['side']}"
        ex_map[key] = p
    
    # Check DB positions against exchange
    db_keys = set()
    for d in db_open:
        key = f"{d['symbol']}:{d['side']}"
        db_keys.add(key)
        if key not in ex_map:
            changes.append({
                'action': 'CLOSE_STALE',
                'db_id': d['id'],
                'detail': f"DB ID#{d['id']}: {d['symbol']} {d['side']} x{d['quantity']} @ {d['entry_price']} — no matching exchange position",
            })
    
    # Check exchange positions not in DB
    for key, p in ex_map.items():
        if key not in db_keys:
            changes.append({
                'action': 'INSERT_MISSING',
                'detail': f"Exchange: {p['symbol']} {p['side']} x{p['quantity']} @ {p['entry_price']} (uPNL={p['unrealized_pnl']})",
                'data': p,
            })
    
    # Execute if not dry run
    if dry_run:
        print("=== DRY RUN ===")
        for c in changes:
            print(f"  [{c['action']}] {c['detail']}")
        if not changes:
            print("  ✅ In sync — no changes needed")
        return
    
    if not changes:
        print("✅ DB in sync with exchange — no changes needed")
        return
    
    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA busy_timeout=10000")  # 10s timeout for concurrent access
    cur = db.cursor()
    now = time.time()

    for c in changes:
        if c['action'] == 'CLOSE_STALE':
            cur.execute(
                "UPDATE trades_log SET status='CLOSED', exit_time=?, reason=reason || ' [SYNC: position not on exchange]' WHERE id=?",
                (now, c['db_id'])
            )
            print(f"  CLOSED DB ID#{c['db_id']}")
        
        elif c['action'] == 'INSERT_MISSING':
            p = c['data']
            cur.execute("""
                INSERT INTO trades_log (symbol, side, entry_time, entry_price, quantity, pnl, pnl_pct, fee, strategy_name, reason, status)
                VALUES (?, ?, ?, ?, ?, 0.0, 0.0, 0.0, 'TrendFollow', '[SYNC: from exchange]', 'OPEN')
            """, (p['symbol'], p['side'], now, p['entry_price'], p['quantity'],))
            print(f"  INSERTED {p['symbol']} {p['side']} x{p['quantity']} @ {p['entry_price']}")
    
    db.commit()
    db.close()
    
    # Print summary
    final = get_db_state()
    print(f"\n  Balance: {balance['total']:.2f} USDT | uPNL: {balance['unrealized_pnl']:.2f}")
    print(f"  Open positions: {len(final)}")
    for d in final:
        print(f"    {d['symbol']} {d['side']} x{d['quantity']} @ {d['entry_price']}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync local DB with Binance testnet exchange')
    parser.add_argument('--force', action='store_true', help='Force close stale records')
    parser.add_argument('--dry-run', action='store_true', help='Show what would change')
    args = parser.parse_args()
    
    if not API_KEY or not API_SECRET:
        print("ERROR: BINANCE_API_KEY and BINANCE_API_SECRET required in .env")
        sys.exit(1)
    
    reconcile(dry_run=args.dry_run)
