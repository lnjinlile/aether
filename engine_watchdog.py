#!/usr/bin/env python3
"""Watchdog: restart engine.py and pipeline.py if not running."""
import json, os, subprocess, sys
from datetime import datetime, timezone, timedelta

def check_and_restart(name, state_file, timeout_min=10):
    if not os.path.exists(state_file): return True
    try:
        with open(state_file) as f:
            data = json.load(f)
        last = datetime.fromisoformat(data.get("last_run", data.get("_updated_at", "2000-01-01")))
        if (datetime.now(timezone.utc) - last) > timedelta(minutes=timeout_min):
            return True
    except: return True
    return False

restarted = []
for name, script, state in [
    ("pipeline", "pipeline.py", ".aether/state/pipeline.json"),
    ("engine", "engine.py", ".aether/state/risk_check.json"),
]:
    if check_and_restart(name, state):
        subprocess.Popen([sys.executable, script], cwd=os.path.dirname(os.path.abspath(__file__)),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restarted.append(name)

if restarted:
    print(f"Restarted: {restarted}")
else:
    print("All systems alive")
