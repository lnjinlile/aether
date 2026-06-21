#!/usr/bin/env python3
"""Watchdog: restart pipeline.py if it's not running."""
import json, os, subprocess, sys

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aether", "state", "pipeline.json")

def is_alive():
    if not os.path.exists(STATE): return False
    try:
        with open(STATE) as f:
            data = json.load(f)
        # Check last update was within 10 minutes
        from datetime import datetime, timezone, timedelta
        last = datetime.fromisoformat(data.get("last_run", "2000-01-01"))
        return (datetime.now(timezone.utc) - last) < timedelta(minutes=10)
    except: return False

if not is_alive():
    print("Pipeline dead, restarting...")
    subprocess.Popen(
        [sys.executable, "pipeline.py"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
else:
    print("Pipeline alive")
