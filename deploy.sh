#!/usr/bin/env python3
"""Aether 部署脚本 — dev → main 合并且重启生产"""
import subprocess, sys, os

BASE = os.path.dirname(os.path.abspath(__file__))

def run(cmd):
    result = subprocess.run(cmd, shell=True, cwd=BASE, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAIL: {cmd}\n{result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

print("🔄 Deploying dev → main...")

# 1. Stop production
print("1. Stopping production...")
run("pkill -f 'python3 engine.py' 2>/dev/null; pkill -f 'python3 pipeline.py' 2>/dev/null; sleep 1")

# 2. Merge dev into main
print("2. Merging dev → main...")
run("git checkout main")
run("git merge dev --no-edit")
run("git push origin main")

# 3. Start production
print("3. Starting production...")
subprocess.Popen([sys.executable, "pipeline.py"], cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.Popen([sys.executable, "engine.py"], cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

print("4. Switching back to dev...")
run("git checkout dev")

print("✅ Deployed. Production running on main.")
