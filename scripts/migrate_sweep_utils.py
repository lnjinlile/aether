#!/usr/bin/env python3
"""PERF-053 completion: Migrate backtest sweep scripts to use sweep_utils.load_data().

Replaces 6 identical local load_data() implementations with a single import
from backtest.sweep_utils, eliminating ~72 lines of duplicated code.
"""
import os, re, sys

BASE = "/home/rinnen/binance_quant/backtests"

# Group A: files with identical load_data(symbol, timeframe, lookback_days=365)
GROUP_A = [
    "keltner_mr_sweep.py",
    "rsi_mr_sweep.py",
    "rsi_mr_refined_sweep.py",
    "supertrend_sweep.py",
    "trendfollow_adx_sweep.py",
    "trendfollow_btc_sweep.py",
]

LOAD_DATA_PATTERN = re.compile(
    r'\ndef load_data\(symbol, timeframe, lookback_days=365\):.*?(?=\n\ndef |\n\nclass |\n\n# |\Z)',
    re.DOTALL
)

IMPORT_STORAGE = "from data.storage import MarketStorage\n"
IMPORT_SWEEP = "from backtest.sweep_utils import load_data\n"

def migrate_file(filepath):
    with open(filepath) as f:
        content = f.read()
    original = content

    # 1. Replace MarketStorage import with sweep_utils import
    if IMPORT_STORAGE in content:
        content = content.replace(IMPORT_STORAGE, IMPORT_SWEEP)
    else:
        print(f"  WARN: MarketStorage import not found in {filepath}")
        return False

    # 2. Remove the local load_data function
    match = LOAD_DATA_PATTERN.search(content)
    if match:
        content = content[:match.start()] + content[match.end():]
    else:
        print(f"  WARN: load_data function not matched in {filepath}")
        # Try simpler pattern
        lines = content.split('\n')
        new_lines = []
        in_load_data = False
        for line in lines:
            if line.startswith('def load_data('):
                in_load_data = True
                continue
            if in_load_data:
                if line and not line[0].isspace():
                    in_load_data = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        content = '\n'.join(new_lines)

    # 3. Clean up double blank lines
    while '\n\n\n' in content:
        content = content.replace('\n\n\n', '\n\n')

    if content != original:
        with open(filepath, 'w') as f:
            f.write(content)
        return True
    return False

def main():
    print("PERF-053 Completion: Migrating load_data to sweep_utils")
    print("=" * 60)

    migrated = 0
    for filename in GROUP_A:
        filepath = os.path.join(BASE, filename)
        if not os.path.exists(filepath):
            print(f"  SKIP: {filename} not found")
            continue
        lines_before = len(open(filepath).readlines())
        if migrate_file(filepath):
            lines_after = len(open(filepath).readlines())
            print(f"  ✅ {filename}: {lines_before}→{lines_after} lines ({lines_before - lines_after} removed)")
            migrated += 1
        else:
            print(f"  ⏭️  {filename}: no changes needed")

    print(f"\nMigrated: {migrated}/{len(GROUP_A)} files")
    print(f"Lines saved: ~{migrated * 12} (load_data function bodies)")

    # Verify imports are correct
    print("\nVerification:")
    for filename in GROUP_A:
        filepath = os.path.join(BASE, filename)
        with open(filepath) as f:
            content = f.read()
        has_sweep = 'from backtest.sweep_utils import' in content
        has_local = 'def load_data(' in content
        has_import_storage = 'from data.storage import MarketStorage' in content
        status = "✅" if has_sweep and not has_local and not has_import_storage else "⚠️"
        print(f"  {status} {filename}: sweep_utils={has_sweep} local_load={has_local} leftover_storage={has_import_storage}")

if __name__ == '__main__':
    main()
