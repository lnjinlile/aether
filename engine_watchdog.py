#!/usr/bin/env python3
"""Watchdog: restart pipeline.py or engine.py if stalled."""
import json, os, subprocess, sys
from datetime import datetime, timezone, timedelta

TIMEOUT_MIN = 10
BASE = os.path.dirname(os.path.abspath(__file__))

def check_and_restart(name, state_file, script, timeout_min=TIMEOUT_MIN):
    state_path = os.path.join(BASE, state_file)
    if not os.path.exists(state_path):
        return True  # state file missing → restart

    try:
        with open(state_path) as f:
            data = json.load(f)
        last_str = data.get("last_run", data.get("_updated_at", "2000-01-01"))
        last_dt = datetime.fromisoformat(last_str)
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return age > (timeout_min * 60)
    except:
        return True  # parse error → restart

def kill_and_restart(name, script):
    """Kill old process then start new one."""
    # Kill old
    try:
        subprocess.run(["pkill", "-f", f"python3 {script}"], timeout=10, capture_output=True)
    except:
        pass
    # Start new
    subprocess.Popen(
        ["/usr/bin/bash", "-lic",
         f"set +m; cd {BASE} && source venv/bin/activate && "
         f"python3 {script} 2>&1 | tee logs/{script.replace('.py','')}.log"],
        cwd=BASE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

restarted = []
for name, script, state in [
    ("pipeline", "pipeline.py", ".aether/state/pipeline.json"),
    ("engine", "engine.py", ".aether/state/risk_check.json"),
]:
    if check_and_restart(name, state, script):
        kill_and_restart(name, script)
        restarted.append(name)

if restarted:
    print(f"Restarted: {restarted}")
else:
    print("All systems alive")
