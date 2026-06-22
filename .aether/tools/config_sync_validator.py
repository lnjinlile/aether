#!/usr/bin/env python3
"""
Config Sync Validator — validates consistency across all Aether configuration and state files.

Usage:
    python3 .aether/tools/config_sync_validator.py           # full check
    python3 .aether/tools/config_sync_validator.py --json    # JSON output
    python3 .aether/tools/config_sync_validator.py --fix     # auto-fix minor issues

Checks:
    1. strategies.yaml ↔ oracle.json (strategies_enabled match)
    2. strategies.yaml ↔ athena.json (live strategies match)
    3. strategies.yaml ↔ backtest_results.json (enabled strategies match)
    4. strategies.yaml ↔ prometheus.json (tracked strategies)
    5. Duplicate strategy names in YAML
    6. Invalid class paths (modules that can't be imported)
    7. Metadata consistency (symbols, timeframes match across files)
    8. State file freshness (staleness detection)
"""

import json, os, sys, yaml
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATE_DIR = ROOT / ".aether" / "state"
STRATEGIES_YAML = ROOT / "config" / "strategies.yaml"

# Ensure project root is in sys.path for strategy module imports
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def check_all(fix=False):
    issues = []
    warnings = []
    fixes_applied = []

    # ── 1. Load all sources ──
    if not STRATEGIES_YAML.exists():
        return [{"severity": "critical", "check": "strategies.yaml", "msg": "File not found"}]

    cfg = load_yaml(STRATEGIES_YAML)
    strategies = cfg.get("strategies", [])
    yaml_enabled = sorted([s["name"] for s in strategies if s.get("enabled", False)])
    yaml_all = sorted([s["name"] for s in strategies])
    yaml_by_name = {s["name"]: s for s in strategies}

    oracle = load_json(STATE_DIR / "oracle.json")
    athena = load_json(STATE_DIR / "athena.json")
    bt = load_json(STATE_DIR / "backtest_results.json")
    prometheus = load_json(STATE_DIR / "prometheus.json")

    # ── 2. oracle.json strategies_enabled ──
    oracle_enabled = oracle.get("strategies_enabled", [])
    if isinstance(oracle_enabled, list):
        oracle_enabled = sorted(oracle_enabled)
        if oracle_enabled != yaml_enabled:
            issues.append({
                "severity": "warning",
                "check": "oracle.json ↔ strategies.yaml",
                "msg": f"oracle.json strategies_enabled={oracle_enabled} ≠ yaml enabled={yaml_enabled}",
                "fixable": True,
            })
            if fix:
                oracle["strategies_enabled"] = yaml_enabled
                with open(STATE_DIR / "oracle.json", "w") as f:
                    json.dump(oracle, f, indent=2, ensure_ascii=False)
                fixes_applied.append("oracle.json strategies_enabled synced")

    # ── 3. athena.json strategy status ──
    athena_strats = athena.get("strategies", {})
    athena_live = sorted([k for k, v in athena_strats.items() if v.get("status") == "ok"])
    if athena_live != yaml_enabled:
        issues.append({
            "severity": "warning",
            "check": "athena.json ↔ strategies.yaml",
            "msg": f"athena.json live(ok)={athena_live} ≠ yaml enabled={yaml_enabled}",
            "fixable": False,
        })

    # Check athena has entries for all strategies
    athena_missing = [n for n in yaml_all if n not in athena_strats]
    if athena_missing:
        warnings.append({
            "severity": "info",
            "check": "athena.json completeness",
            "msg": f"athena.json missing strategy entries: {athena_missing}",
        })

    # ── 4. backtest_results.json enabled ──
    bt_strats = bt.get("strategies", {})
    bt_enabled = sorted([k for k, v in bt_strats.items() if v.get("enabled") == True])
    if bt_enabled != yaml_enabled:
        issues.append({
            "severity": "warning",
            "check": "backtest_results.json ↔ strategies.yaml",
            "msg": f"backtest_results enabled={bt_enabled} ≠ yaml enabled={yaml_enabled}",
            "fixable": False,
        })

    # ── 5. prometheus.json strategy tracking ──
    prom_strats = prometheus.get("strategies", {})
    prom_tracked = sorted(prom_strats.keys())
    missing_in_prom = [s for s in yaml_enabled if s not in prom_strats]
    if missing_in_prom:
        warnings.append({
            "severity": "info",
            "check": "prometheus.json tracking",
            "msg": f"prometheus.json missing metrics for enabled strategies: {missing_in_prom}",
        })

    # ── 6. Duplicate names ──
    names = [s["name"] for s in strategies]
    dupes = sorted(set(n for n in names if names.count(n) > 1))
    if dupes:
        issues.append({
            "severity": "critical",
            "check": "strategies.yaml duplicates",
            "msg": f"Duplicate strategy names: {dupes}",
            "fixable": False,
        })

    # ── 7. Class path validation ──
    import importlib
    for s in strategies:
        cls = s["class"]
        mod_path, cls_name = cls.rsplit(".", 1)
        try:
            importlib.import_module(mod_path)
        except ImportError as e:
            issues.append({
                "severity": "critical",
                "check": "class path",
                "msg": f'{s["name"]}: class {cls} — module not importable: {e}',
                "fixable": False,
            })

    # ── 8. Metadata consistency: symbols/timeframes ──
    for name in yaml_all:
        yaml_s = yaml_by_name[name]
        yaml_sym = yaml_s.get("params", {}).get("symbols", [None])[0]
        yaml_tf = yaml_s.get("params", {}).get("timeframes", [None])[0]

        # vs backtest_results
        if name in bt_strats:
            bt_s = bt_strats[name]
            bt_sym = bt_s.get("symbol", "unknown")
            bt_tf = bt_s.get("timeframe", "unknown")
            if yaml_sym and bt_sym != "unknown" and yaml_sym != bt_sym:
                warnings.append({
                    "severity": "info",
                    "check": "metadata: symbols",
                    "msg": f"{name}: yaml symbol={yaml_sym} ≠ backtest_results symbol={bt_sym}",
                })
            if yaml_tf and bt_tf != "unknown" and yaml_tf != bt_tf:
                warnings.append({
                    "severity": "info",
                    "check": "metadata: timeframes",
                    "msg": f"{name}: yaml timeframe={yaml_tf} ≠ backtest_results timeframe={bt_tf}",
                })

    # ── 9. State file freshness ──
    now = datetime.now(timezone.utc)
    for agent in ["oracle", "athena", "prometheus", "mercury", "guardian"]:
        path = STATE_DIR / f"{agent}.json"
        if path.exists():
            data = load_json(path)
            ts = data.get("_updated_at", "")
            if ts:
                try:
                    age = (now - datetime.fromisoformat(ts)).total_seconds()
                    if age > 3600:
                        warnings.append({
                            "severity": "warning",
                            "check": f"{agent}.json freshness",
                            "msg": f"{agent}.json last updated {age/60:.0f}min ago (stale: >60min)",
                        })
                except Exception:
                    pass

    return {
        "timestamp": now.isoformat(),
        "issues": issues,
        "warnings": warnings,
        "fixes_applied": fixes_applied,
        "status": "clean" if not issues else ("warning" if all(i["severity"] == "warning" for i in issues) else "critical"),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Config Sync Validator")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--fix", action="store_true", help="Auto-fix minor issues")
    parser.add_argument("--quiet", action="store_true", help="Silent if clean")
    args = parser.parse_args()

    result = check_all(fix=args.fix)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    issues = result["issues"]
    warnings = result["warnings"]
    fixes = result["fixes_applied"]

    if not issues and not warnings:
        if not args.quiet:
            print("✅ CONFIG SYNC: ALL CLEAN")
        sys.exit(0)

    for f in fixes:
        print(f"🔧 FIXED: {f}")

    for i in issues:
        icon = {"critical": "🔴", "warning": "🟡", "info": "ℹ️"}.get(i["severity"], "⚠️")
        print(f"{icon} [{i['check']}] {i['msg']}")

    for w in warnings:
        icon = {"critical": "🔴", "warning": "🟡", "info": "ℹ️"}.get(w["severity"], "⚠️")
        print(f"{icon} [{w['check']}] {w['msg']}")

    if issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
