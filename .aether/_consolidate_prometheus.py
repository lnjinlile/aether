#!/usr/bin/env python3
"""Consolidate prometheus.json: archive obsolete sweep/verdict/fix keys into _history."""
import json, os, shutil
from datetime import datetime, timezone

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
PATH = os.path.join(STATE_DIR, "prometheus.json")

# Backup
shutil.copy2(PATH, PATH + ".bak")

with open(PATH) as f:
    data = json.load(f)

# Keys to keep active (current/relevant state)
KEEP = {
    "status", "dsr_implemented", "walk_forward_implemented", "anti_overfitting_run",
    "regime_model", "live_validation",
    "ml_alpha_status", "ml_validation", "regime_classifier",
    "strategies", "recommendation", "next",
    "last_optimization", "last_run",
    "strategy_landscape_20260622", "rsi_mr_eth_live_confirmed",
    "regime_monitor", "engine_stale", "engine_pid",
    "_updated_at"
}

# Keys to archive (completed sweeps, fixed issues, dead paths)
ARCHIVE = {
    "dgt_deployed", "dgt_btc_pnl", "dgt_eth_pnl",
    "engine_fix", "wf_findings", "wf_validation", "wf_fresh",
    "bband_rsi_integration", "athena_90d_fresh",
    "timestamp_skew_fix", "new_strategy",
    "pause_rsi_mr_eth", "rsi_mr_eth_revival",
    "rsi_mr_btc_full_sweep", "rsi_mr_btc_verdict_final",
    "bband_rsi_full_sweep", "bband_rsi_eth_sweep",
    "dynamic_grid_verdict", "triple_barrier_verdict",
    "supertrend_sweep", "stoch_rsi_sweep", "macd_sweep",
    "donchian_mr_sweep", "donchian_mr_eth_wf",
    "regime_switch_opt",
}

# Build _history
history = {"sweeps": {}, "fixes": {}, "dead_ends": {}}

for key in ARCHIVE:
    if key in data:
        val = data.pop(key)
        # Categorize
        if 'sweep' in key.lower() or 'wf' in key.lower():
            history["sweeps"][key] = val
        elif 'fix' in key.lower() or 'skew' in key.lower() or 'fresh' in key.lower() or 'pause' in key.lower() or 'revival' in key.lower():
            history["fixes"][key] = val
        else:
            history["dead_ends"][key] = val

data["_history"] = history
data["_consolidated_at"] = datetime.now(timezone.utc).isoformat()
data["_consolidated_keys"] = sorted(ARCHIVE)
data["_updated_at"] = datetime.now(timezone.utc).isoformat()

with open(PATH, "w") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

# Report
size_before = os.path.getsize(PATH + ".bak")
size_after = os.path.getsize(PATH)
print(f"Consolidated: {size_before} → {size_after} bytes ({size_after/size_before*100:.0f}%)")
print(f"Active keys: {len([k for k in data if not k.startswith('_')])}")
print(f"Archived keys: {len(ARCHIVE)}")
print(f"New keys in _history: sweeps={len(history['sweeps'])}, fixes={len(history['fixes'])}, dead_ends={len(history['dead_ends'])}")
