#!/usr/bin/env python3
"""Aether 部署: dev → main 合并, 推送, 重启生产"""
import subprocess, sys, os
BASE = os.path.dirname(os.path.abspath(__file__))

def run(cmd):
    r = subprocess.run(cmd, shell=True, cwd=BASE, capture_output=True, text=True)
    if r.returncode: print(f"WARN: {cmd}"); return ""
    return r.stdout.strip()

print("🔄 Deploy dev → main")
run("pkill -f 'python3 engine.py' 2>/dev/null; pkill -f 'python3 pipeline.py' 2>/dev/null")
run("git checkout main && git merge dev --no-edit && git push origin main")
subprocess.Popen([sys.executable, "pipeline.py"], cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.Popen([sys.executable, "engine.py"], cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
run("git checkout dev")
print("✅ Deployed. Prod=main, Dev=dev")
