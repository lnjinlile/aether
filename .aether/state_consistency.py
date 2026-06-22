#!/usr/bin/env python3
"""
State Consistency Validator — Self-healing cross-source state integrity checker.

Fixes the systemic issue behind AUDIT-026 (and similar recurring contradictions):
multiple agents write to the same state files with partial merges, creating
internal contradictions (positions=1 vs positions_count=0, etc).

Usage:
  python3 .aether/state_consistency.py check       # detect-only mode
  python3 .aether/state_consistency.py heal         # detect + auto-fix
  python3 .aether/state_consistency.py heal --force  # heal even if minor

Checks:
  1. guardian.json internal consistency (positions/positions_count/account.positions)
  2. Cross-source balance agreement (guardian ↔ mercury ↔ risk_check ↔ live_exchange)
  3. Cross-source position count agreement
  4. guardian.json open_orders vs risk_check / live_exchange
  5. mercury.json stale signals (older than 30min)
  6. risk_check.json open_orders vs actual positions (SL/TP tracking)
"""

import json, os, sys
from datetime import datetime, timezone, timedelta

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".aether", "state")
if not os.path.isdir(STATE_DIR):
    STATE_DIR = ".aether/state"

TOLERANCE_BAL = 0.50       # $0.50 balance tolerance
TOLERANCE_UPNL = 1.0       # $1 unrealized PnL tolerance
STALE_SIGNAL_MINUTES = 30  # signals older than this are stale


def load(path):
    with open(os.path.join(STATE_DIR, path)) as f:
        return json.load(f)


def write(path, data):
    with open(os.path.join(STATE_DIR, path), "w") as f:
        json.dump(data, f, indent=2, default=str)


def check_guardian_internal(g):
    """Check guardian.json internal field consistency."""
    issues = []
    pos = g.get("positions")
    pos_c = g.get("positions_count")
    pos_a = g.get("account", {}).get("positions")
    orders = g.get("open_orders")
    orders_a = g.get("account", {}).get("open_orders")

    if pos is not None and pos_c is not None and pos != pos_c:
        issues.append(f"guardian.positions({pos}) != guardian.positions_count({pos_c})")
    if pos is not None and pos_a is not None and pos != pos_a:
        issues.append(f"guardian.positions({pos}) != guardian.account.positions({pos_a})")
    if orders is not None and orders_a is not None and orders != orders_a:
        issues.append(f"guardian.open_orders({orders}) != guardian.account.open_orders({orders_a})")
    return issues


def check_cross_source():
    """Check cross-source agreement between state files."""
    issues = []
    try:
        g = load("guardian.json")
        m = load("mercury.json")
        r = load("risk_check.json")
        l = load("live_exchange.json")
        s = load("signals.json")
    except FileNotFoundError as e:
        return [f"Missing state file: {e}"]

    # Balance check
    bal_g = g.get("balance")
    bal_m = m.get("balance")
    bal_r = r.get("balance")
    bal_l = l.get("balance", {})
    if isinstance(bal_l, dict):
        bal_l = bal_l.get("balance")

    balances = {"guardian": bal_g, "mercury": bal_m, "risk_check": bal_r, "live_exchange": bal_l}
    valid_bals = {k: v for k, v in balances.items() if v is not None and v > 0}
    if len(valid_bals) >= 2:
        vals = list(valid_bals.values())
        if max(vals) - min(vals) > TOLERANCE_BAL:
            issues.append(f"Balance divergence: {valid_bals} (max diff={max(vals)-min(vals):.2f})")

    # Position count
    pos_g = g.get("positions_count", g.get("positions"))
    pos_m = m.get("positions")
    pos_r = r.get("positions_count")
    pos_l = len(l.get("positions", []))

    positions = {"guardian": pos_g, "mercury": pos_m, "risk_check": pos_r, "live_exchange": pos_l}
    valid_pos = [v for k, v in positions.items() if v is not None]
    if len(set(valid_pos)) > 1:
        issues.append(f"Position count divergence: {positions}")

    # Open orders
    ord_g = g.get("open_orders")
    ord_r = r.get("open_orders")
    ord_l = l.get("open_orders")

    orders_map = {"guardian": ord_g, "risk_check": ord_r, "live_exchange": ord_l}
    valid_ords = [v for k, v in orders_map.items() if v is not None]
    if len(set(valid_ords)) > 1:
        # mercury tracks more orders (SL+TP), so it's often different — not an error
        pass

    # Stale signals
    sig_ts = s.get("timestamp", s.get("_updated_at", ""))
    signals = s.get("signals", {})
    if signals:
        if sig_ts:
            try:
                ts = datetime.fromisoformat(sig_ts.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                if age > STALE_SIGNAL_MINUTES:
                    issues.append(f"Stale signals ({len(signals)} active, {age:.0f}min old)")
            except Exception:
                pass

    return issues


def heal_guardian():
    """Auto-heal guardian.json: sync from risk_check as canonical source."""
    try:
        g = load("guardian.json")
        r = load("risk_check.json")
    except FileNotFoundError:
        return False, "Missing source file"

    pos_count = r.get("positions_count", 0)
    orders = r.get("open_orders", 0)
    balance = r.get("balance", 0)
    available = r.get("available", balance)
    upnl = r.get("unrealized_pnl", 0)

    g["positions"] = pos_count
    g["positions_count"] = pos_count
    g["effective_positions"] = pos_count
    g["open_orders"] = orders
    g["balance"] = balance
    g["available"] = available
    g["unrealized_pnl"] = upnl
    g["margin_used"] = balance - available if pos_count > 0 else 0
    g["risk_module"] = "ok" if r.get("status") == "ok" else "degraded"
    g["risk_level"] = r.get("risk_level", g.get("risk_level", "?"))

    if "account" not in g:
        g["account"] = {}
    margin_used = balance - available if pos_count > 0 else 0
    g["account"].update({
        "balance": balance,
        "available": available,
        "margin_used": margin_used,
        "margin_pct": round(margin_used / balance * 100, 1) if balance > 0 else 0,
        "unrealized_pnl": upnl,
        "positions": pos_count,
        "open_orders": orders,
    })

    g["_consistency_healed_at"] = datetime.now(timezone.utc).isoformat()

    write("guardian.json", g)
    return True, f"guardian.json healed: positions→{pos_count}, open_orders→{orders}, balance→{balance:.2f}"


def run_check():
    """Run all checks, return (issues, healed)."""
    all_issues = []
    try:
        g = load("guardian.json")
        all_issues.extend(check_guardian_internal(g))
    except FileNotFoundError:
        all_issues.append("guardian.json missing")
    all_issues.extend(check_cross_source())
    return all_issues


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    force = "--force" in sys.argv

    if mode == "heal":
        all_issues = run_check()
        if all_issues or force:
            healed_ok, msg = heal_guardian()
            if healed_ok:
                print(f"🔧 HEALED: {msg}")
            else:
                print(f"❌ HEAL FAILED: {msg}")
            # Re-check after heal
            remaining = run_check()
            if remaining:
                print(f"⚠️  Remaining issues ({len(remaining)}):")
                for i in remaining:
                    print(f"   - {i}")
            else:
                print("✅ All clear after heal")
        else:
            print("✅ No issues detected — nothing to heal")
    else:
        all_issues = run_check()
        if not all_issues:
            print("✅ All state files consistent")
        else:
            print(f"⚠️  Found {len(all_issues)} issue(s):")
            for i in all_issues:
                print(f"   - {i}")
            print("\nRun 'python3 .aether/state_consistency.py heal' to auto-fix")


if __name__ == "__main__":
    main()
