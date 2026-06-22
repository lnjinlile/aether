#!/usr/bin/env python3
"""AUDIT-048: Hermes audit cross-verification requests"""
import subprocess, json, sys

requests_data = [
    ("athena", "fix_athena_metrics", {
        "reason": "AUDIT-048: athena.json DonchianMR_BTC metrics corrupt - return_pct=0 max_dd=0 win_rate=0 with 38 trades. Sharpe=0.508 contradicts zero return. Prometheus has return=+203.81% DD=11.62% WR=68.4%. Fix athena.json DonchianMR_BTC metrics from backtest_results or prometheus.",
        "audit_id": "AUDIT-048",
        "priority": "critical"
    }),
    ("prometheus", "fix_athena_metrics", {
        "reason": "AUDIT-048: athena.json DonchianMR_ETH return_pct=20.52%(10trades) vs prometheus.json 429.48%(50trades). Major deviation. Also DonchianMR_BTC athena metrics all zero. Verify and fix athena.json strategy metric sync pipeline.",
        "audit_id": "AUDIT-048",
        "priority": "critical"
    }),
    ("guardian", "escalate_audit_046", {
        "reason": "AUDIT-046 ESCALATION: 8h+ unresolved. 4 pending requests (#161/#165/#166/#168) all stuck at Guardian. guardian.json notes still claim DonchianMR_ETH verdict=PAPER (actual LIVE). paper_strategies not cleaned. live_strategy_metrics missing DonchianMR_ETH. Guardian cron 12:31 ran but did NOT fix. Clean guardian.json NOW.",
        "audit_id": "AUDIT-046",
        "priority": "critical",
        "escalation": True
    }),
]

for target, req_type, data in requests_data:
    cmd = [
        sys.executable, ".aether/platform.py", "request",
        target, "hermes", req_type, json.dumps(data)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    print(f"-> {target}: {result.stdout.strip() or result.stderr.strip()}")
