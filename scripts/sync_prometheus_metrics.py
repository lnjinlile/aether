#!/usr/bin/env python3
"""
Sync PERF-069 365d DSR + engine metrics from backtest_results.json to
prometheus.json strategies section. Fixes Kelly sizing inputs for mercury_run.py.

Problem: mercury_run.py reads prometheus.json["strategies"][name]["dsr"]
but prometheus.json only has flat fields (return_pct, sharpe, win_rate, trades, max_dd).
Engine writes proper avg_win_pct, avg_loss_pct, deflated_sharpe_ratio to
backtest_results.json["strategies"][name]["metrics"].

This sync copies those engine-computed metrics into prometheus.json so
mercury_run.py gets actual values instead of defaults (DSR=0.5, avg_loss=2.0%).
"""
import json, os

PROMETHEUS = ".aether/state/prometheus.json"
BACKTEST = ".aether/state/backtest_results.json"

with open(PROMETHEUS) as f:
    prom = json.load(f)
with open(BACKTEST) as f:
    bt = json.load(f)

bt_strategies = bt.get("strategies", {})
prom_strategies = prom.get("strategies", {})

updated_count = 0
for name, ps in prom_strategies.items():
    bts = bt_strategies.get(name, {})
    if not bts:
        continue

    metrics = bts.get("metrics", {})
    if not metrics:
        continue

    # Pull engine-computed metrics
    dsr = metrics.get("deflated_sharpe_ratio")
    avg_win = metrics.get("avg_win_pct")
    avg_loss = metrics.get("avg_loss_pct")

    changed = False
    if dsr is not None:
        if ps.get("dsr") != dsr:
            ps["dsr"] = dsr
            changed = True
    if avg_win is not None:
        if ps.get("avg_win_pct") != avg_win:
            ps["avg_win_pct"] = avg_win
            changed = True
    if avg_loss is not None:
        if ps.get("avg_loss_pct") != avg_loss:
            ps["avg_loss_pct"] = avg_loss
            changed = True

    # Also pull n_trials if available
    n_trials = metrics.get("n_trials")
    if n_trials is not None:
        if ps.get("n_trials") != n_trials:
            ps["n_trials"] = n_trials
            changed = True

    if changed:
        updated_count += 1
        print(f"  {name}: dsr={dsr}, avg_win={avg_win}%, avg_loss={avg_loss}%")

# Also overlay PERF-069 365d DSR where available
dsr_365d = prom.get("dsr_summary_365d", {})
for name, d365 in dsr_365d.items():
    if name in prom_strategies:
        prom_strategies[name]["dsr_365d"] = d365["dsr_365d"]
        prom_strategies[name]["sharpe_365d"] = d365["sharpe_365d"]

prom["_updated_at"] = "2026-06-23T10:10:00Z"
prom["_perf_069_sync"] = f"Synced {updated_count} strategies with engine metrics (avg_win_pct, avg_loss_pct, dsr) from backtest_results.json"

with open(PROMETHEUS, "w") as f:
    json.dump(prom, f, indent=2, ensure_ascii=False)

print(f"\nSynced {updated_count} strategies")
print("Kelly sizing inputs now available:")
for name in ["RSI_MR_ETH", "KeltnerMR_ETH", "DonchianMR_ETH", "BandMR_ETH"]:
    ps = prom_strategies.get(name, {})
    dsr = ps.get("dsr", "N/A")
    avg_win = ps.get("avg_win_pct", "N/A")
    avg_loss = ps.get("avg_loss_pct", "N/A")
    dsr_365 = ps.get("dsr_365d", "N/A")
    print(f"  {name:20s} dsr={dsr} dsr_365d={dsr_365} avg_win={avg_win} avg_loss={avg_loss}")
