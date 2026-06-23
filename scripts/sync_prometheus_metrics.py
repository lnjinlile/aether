#!/usr/bin/env python3
"""
Auto-generate prometheus.json recommendation field from live_strategies
and strategies sections. Prevents AUDIT-138 (stale manual recommendation).

Usage: python3 scripts/sync_prometheus_metrics.py
       python3 scripts/sync_prometheus_metrics.py --recommendation-only
"""
import json, os, sys
from datetime import datetime, timezone

PROMETHEUS = ".aether/state/prometheus.json"
BACKTEST = ".aether/state/backtest_results.json"


def rebuild_recommendation(prom):
    """Auto-generate recommendation from live_strategies + strategies metrics."""
    live = prom.get("portfolio_concentration", {}).get("live_strategies", [])
    strategies = prom.get("strategies", {})
    paper_strategies = prom.get("portfolio_concentration", {}).get("paper_strategies", [])
    
    live_lines = []
    for name in live:
        s = strategies.get(name, {})
        sr = s.get("sharpe", "?")
        dd = s.get("max_dd", "?")
        trades = s.get("trades", "?")
        if isinstance(sr, (int, float)):
            sr = f"{sr:.2f}"
        live_lines.append(f"{name} (SR={sr} DD={dd}% {trades}t)")
    
    paper_lines = []
    for name in paper_strategies:
        s = strategies.get(name, {})
        sr = s.get("sharpe", "?")
        dd = s.get("max_dd", "?")
        trades = s.get("trades", "?")
        dsr = prom.get("dsr_summary_365d", {}).get(name, {}).get("dsr_365d", "?")
        if isinstance(sr, (int, float)):
            sr = f"{sr:.2f}"
        paper_lines.append(f"{name} (SR={sr} DD={dd}% {trades}t, DSR={dsr})")
    
    parts = [f"LIVE ({len(live)}): " + ", ".join(live_lines) + "."]
    if paper_lines:
        parts.append(f"PAPER ({len(paper_strategies)}): " + ", ".join(paper_lines) + ".")
    
    # Append any static notes from existing recommendation if present
    old_rec = prom.get("recommendation", "")
    # Extract trailing notes that are not part of LIVE/PAPER listings
    for marker in ["4h ETH MR:", "PERF-078", "monitor DD weekly"]:
        if marker in old_rec:
            # Find last sentence start
            idx = old_rec.rfind(". ") 
            if idx > 0 and any(m in old_rec[idx:] for m in ["4h ETH", "PERF-078"]):
                tail = old_rec[idx+2:]  # after ". "
                if tail not in " ".join(parts):
                    parts.append(tail)
            break
    
    return " ".join(parts)


def sync_metrics():
    """Sync engine metrics from backtest_results.json to prometheus.json strategies."""
    with open(PROMETHEUS) as f:
        prom = json.load(f)
    
    rec_only = "--recommendation-only" in sys.argv
    
    if not rec_only and os.path.exists(BACKTEST):
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

            dsr = metrics.get("deflated_sharpe_ratio")
            avg_win = metrics.get("avg_win_pct")
            avg_loss = metrics.get("avg_loss_pct")
            n_trials = metrics.get("n_trials")

            changed = False
            if dsr is not None and ps.get("dsr") != dsr:
                ps["dsr"] = dsr
                changed = True
            if avg_win is not None and ps.get("avg_win_pct") != avg_win:
                ps["avg_win_pct"] = avg_win
                changed = True
            if avg_loss is not None and ps.get("avg_loss_pct") != avg_loss:
                ps["avg_loss_pct"] = avg_loss
                changed = True
            if n_trials is not None and ps.get("n_trials") != n_trials:
                ps["n_trials"] = n_trials
                changed = True

            if changed:
                updated_count += 1
                print(f"  {name}: dsr={dsr}, avg_win={avg_win}%, avg_loss={avg_loss}%")

        # Overlay PERF-069 365d DSR
        dsr_365d = prom.get("dsr_summary_365d", {})
        for name, d365 in dsr_365d.items():
            if name in prom_strategies:
                prom_strategies[name]["dsr_365d"] = d365["dsr_365d"]
                prom_strategies[name]["sharpe_365d"] = d365["sharpe_365d"]

        print(f"\nSynced {updated_count} strategies")

    # Auto-generate recommendation (always)
    old_rec = prom.get("recommendation", "")
    new_rec = rebuild_recommendation(prom)
    if old_rec != new_rec:
        prom["recommendation"] = new_rec
        print(f"recommendation: AUTO-GENERATED ({len(prom.get('portfolio_concentration', {}).get('live_strategies', []))} LIVE)")
    else:
        print("recommendation: unchanged")

    prom["_recommendation_auto"] = True
    prom["_recommendation_updated_at"] = datetime.now(timezone.utc).isoformat()
    prom["_updated_at"] = datetime.now(timezone.utc).isoformat()

    with open(PROMETHEUS, "w") as f:
        json.dump(prom, f, indent=2, ensure_ascii=False)

    if not rec_only:
        prom_strategies = prom.get("strategies", {})
        print("\nKelly sizing inputs:")
        for name in ["RSI_MR_ETH", "KeltnerMR_ETH", "DonchianMR_ETH", "BandMR_ETH",
                      "DonchianMR_BTC", "KeltnerMR_BTC", "RSI_MR_BTC"]:
            ps = prom_strategies.get(name, {})
            if not ps:
                continue
            dsr = ps.get("dsr", "N/A")
            avg_win = ps.get("avg_win_pct", "N/A")
            avg_loss = ps.get("avg_loss_pct", "N/A")
            dsr_365 = ps.get("dsr_365d", "N/A")
            print(f"  {name:20s} dsr={dsr} dsr_365d={dsr_365} avg_win={avg_win} avg_loss={avg_loss}")


if __name__ == "__main__":
    sync_metrics()
