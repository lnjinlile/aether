#!/usr/bin/env python3
"""Watchdog: restart pipeline.py if it's not running or stalled."""
import json, os, subprocess, sys
from datetime import datetime, timezone, timedelta

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state", "pipeline.json")
TIMEOUT_MIN = 10

def is_alive():
    if not os.path.exists(STATE): return False
    try:
        with open(STATE) as f:
            data = json.load(f)
        last = datetime.fromisoformat(data.get("last_run", "2000-01-01"))
        return (datetime.now(timezone.utc) - last) < timedelta(minutes=TIMEOUT_MIN)
    except Exception:
        return False

if not is_alive():
    print(f"Pipeline stalled (> {TIMEOUT_MIN}min), restarting...")
    # Kill old pipeline process first
    try:
        subprocess.run(["pkill", "-f", "python3 pipeline.py"], timeout=10, capture_output=True)
    except:
        pass
    # Start new pipeline
    base_dir = os.path.dirname(os.path.abspath(__file__))
    subprocess.Popen(
        ["/usr/bin/bash", "-lic",
         f"set +m; cd {base_dir} && source venv/bin/activate && "
         f"python3 pipeline.py 2>&1 | tee logs/pipeline.log"],
        cwd=base_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("Pipeline restarted")
else:
    print("Pipeline alive")
