#!/usr/bin/env python3
"""Generate Aether dashboard HTML from shared state."""
import json, os
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, ".aether", "state")
REQ_FILE = os.path.join(BASE, ".aether", "requests", "requests.json")
BULLETIN = os.path.join(BASE, ".aether", "bulletin.md")
OUTPUT = os.path.join(BASE, "dashboard.html")

AGENTS = ["oracle", "athena", "guardian", "mercury", "prometheus"]
AGENT_NAMES = {
    "oracle": "Oracle 数据", "athena": "Athena 策略",
    "guardian": "Guardian 风控", "mercury": "Mercury 交易",
    "prometheus": "Prometheus 优化"
}
AGENT_ICONS = {"oracle":"🔵","athena":"🟢","guardian":"🛡️","mercury":"💹","prometheus":"🔥"}

def load_state(agent):
    path = os.path.join(STATE_DIR, f"{agent}.json")
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except: return {}

def load_requests():
    if not os.path.exists(REQ_FILE): return []
    try:
        with open(REQ_FILE) as f: return json.load(f)
    except: return []

def load_bulletin():
    if not os.path.exists(BULLETIN): return []
    with open(BULLETIN) as f:
        return [l.strip().lstrip("#").strip() for l in f.readlines()[-30:] if l.strip() and not l.startswith("|")]

def load_trades():
    """Read trades from SQLite."""
    import sqlite3
    db = os.path.join(BASE, "data", "market.db")
    if not os.path.exists(db): return []
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades_log ORDER BY id DESC LIMIT 15").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except: return []

def agent_card(agent):
    s = load_state(agent)
    updated = s.get("_updated_at", "never")[:16]
    status = s.get("status", "unknown")
    color = {"ok":"#22c55e","active":"#22c55e","error":"#ef4444","warn":"#f59e0b"}.get(status, "#6b7280")
    extras = ""
    for k, v in s.items():
        if k.startswith("_"): continue
        if isinstance(v, (int, float)):
            extras += f'<span class="stat">{k}: <b>{v}</b></span>'
    return f'''
    <div class="card">
      <div class="card-header">
        <span class="icon">{AGENT_ICONS[agent]}</span>
        <span class="name">{AGENT_NAMES[agent]}</span>
        <span class="dot" style="background:{color}"></span>
      </div>
      <div class="card-body">
        <div class="stat-row">{extras or '<span class="stat">状态: <b>'+status+'</b></span>'}</div>
      </div>
      <div class="card-footer">更新: {updated}</div>
    </div>'''

def request_table():
    reqs = load_requests()
    if not reqs: return '<div class="empty">无请求记录</div>'
    rows = ""
    for r in reqs[-15:]:
        st = r["status"]
        sc = {"pending":"#f59e0b","fulfilled":"#22c55e","rejected":"#ef4444"}.get(st,"#6b7280")
        rows += f'''<tr>
          <td>#{r["id"]}</td>
          <td>{r.get("from","?")}</td>
          <td>→ {r.get("target","?")}</td>
          <td>{r.get("type","?")}</td>
          <td><span style="color:{sc}">● {st}</span></td>
          <td class="reason">{r.get("data",{}).get("reason","")[:50]}</td>
        </tr>'''
    return f'<table><tr><th>ID</th><th>发起</th><th>接收</th><th>类型</th><th>状态</th><th>原因</th></tr>{rows}</table>'

def trade_table():
    trades = load_trades()
    if not trades: return '<div class="empty">无交易记录</div>'
    rows = ""
    for t in trades:
        pnl = t.get("pnl") or 0
        pnl_color = "#22c55e" if float(pnl) > 0 else ("#ef4444" if float(pnl) < 0 else "#6b7280")
        rows += f'''<tr>
          <td>#{t["id"]}</td>
          <td>{t.get("symbol","")}</td>
          <td>{t.get("side","")}</td>
          <td>{t.get("entry_price","")}</td>
          <td>{t.get("exit_price") or "—"}</td>
          <td style="color:{pnl_color}">{float(pnl):+.4f}</td>
          <td><span class="badge">{t.get("status","")}</span></td>
          <td class="reason">{t.get("strategy_name","")}</td>
        </tr>'''
    return f'<table><tr><th>ID</th><th>标的</th><th>方向</th><th>入场</th><th>出场</th><th>PnL</th><th>状态</th><th>策略</th></tr>{rows}</table>'

def bulletin_feed():
    lines = load_bulletin()
    if not lines: return '<div class="empty">公告板为空</div>'
    html = ""
    for line in lines[-15:]:
        line = line.replace("---","").strip()
        if not line: continue
        if "Oracle" in line: cls = "oracle"
        elif "Athena" in line: cls = "athena"
        elif "Guardian" in line: cls = "guardian"
        elif "Mercury" in line: cls = "mercury"
        elif "Prometheus" in line: cls = "prometheus"
        else: cls = ""
        html += f'<div class="bulletin-item {cls}">{line[:120]}</div>'
    return html

html = f'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>Aether 运营仪表盘</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f1117;color:#e5e7eb;font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:20px}}
h1{{font-size:24px;margin-bottom:5px}}
h1 span{{color:#f59e0b}}
.subtitle{{color:#6b7280;margin-bottom:20px;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px}}
.card{{background:#1a1d2e;border-radius:10px;padding:14px;border:1px solid #2d3148}}
.card-header{{display:flex;align-items:center;gap:8px;margin-bottom:8px}}
.icon{{font-size:20px}}
.name{{font-weight:600;font-size:14px;flex:1}}
.dot{{width:8px;height:8px;border-radius:50%}}
.card-body{{font-size:12px}}
.stat-row{{display:flex;flex-wrap:wrap;gap:6px}}
.stat{{background:#252840;padding:3px 8px;border-radius:4px;font-size:11px}}
.stat b{{color:#f59e0b}}
.card-footer{{color:#6b7280;font-size:10px;margin-top:8px}}
.section{{background:#1a1d2e;border-radius:10px;padding:16px;margin-bottom:16px;border:1px solid #2d3148}}
.section h2{{font-size:16px;margin-bottom:12px;color:#f59e0b}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{text-align:left;padding:8px 6px;border-bottom:1px solid #2d3148;color:#6b7280;font-weight:500}}
td{{padding:6px;border-bottom:1px solid #1f2237}}
.reason{{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9ca3af}}
.badge{{background:#252840;padding:2px 7px;border-radius:4px;font-size:10px}}
.empty{{color:#6b7280;text-align:center;padding:20px}}
.bulletin-item{{padding:6px 10px;border-left:3px solid #2d3148;margin:4px 0;font-size:12px;border-radius:0 4px 4px 0}}
.bulletin-item.oracle{{border-left-color:#3b82f6}}
.bulletin-item.mercury{{border-left-color:#22c55e}}
.bulletin-item.athena{{border-left-color:#a855f7}}
.bulletin-item.guardian{{border-left-color:#ef4444}}
.bulletin-item.prometheus{{border-left-color:#f59e0b}}
.footer{{text-align:center;color:#374151;font-size:11px;margin-top:20px}}
</style>
</head>
<body>
<h1>⚡ Aether <span>运营仪表盘</span></h1>
<div class="subtitle">自动刷新 · 每5分钟 · {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>

<div class="grid">
{''.join(agent_card(a) for a in AGENTS)}
</div>

<div class="section">
  <h2>📋 公告板</h2>
  {bulletin_feed()}
</div>

<div class="section">
  <h2>📨 请求队列</h2>
  {request_table()}
</div>

<div class="section">
  <h2>📊 交易记录</h2>
  {trade_table()}
</div>

<div class="footer">Aether Dashboard · Generated by platform.py</div>
</body>
</html>'''

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"Dashboard: {OUTPUT} ({len(html)} bytes)")
