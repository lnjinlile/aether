#!/usr/bin/env python3
"""DB cleanup: drop unused zero-row tables + WAL checkpoint"""
import sqlite3, os

db_path = "/home/rinnen/binance_quant/data/market.db"
db = sqlite3.connect(db_path)

# Drop unused tables (0 rows, no active code references)
tables_to_drop = ['trades', 'long_short_ratio', 'taker_volume', 'orderbook']
for t in tables_to_drop:
    try:
        cnt = db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        if cnt == 0:
            db.execute(f'DROP TABLE IF EXISTS {t}')
            print(f'Dropped: {t} (was {cnt} rows)')
        else:
            print(f'SKIP {t}: has {cnt} rows')
    except Exception as e:
        print(f'Failed to drop {t}: {e}')

db.commit()

# WAL checkpoint
wal_frames = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
print(f'WAL checkpoint: {wal_frames}')

# Verify remaining tables
tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
print(f'Remaining tables ({len(tables)}): {[t[0] for t in tables]}')

db.close()

# File sizes
size = os.path.getsize(db_path)
wal_path = db_path + '-wal'
wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
print(f'market.db: {size/1024/1024:.2f}MB, WAL: {wal_size}B')
