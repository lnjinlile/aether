#!/usr/bin/env python3
"""Aether 运营指挥中心 — 全量数据仪表盘"""
import json, os, sqlite3, yaml
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE, ".aether", "state")
REQ_FILE = os.path.join(BASE, ".aether", "requests", "requests.json")
BULLETIN = os.path.join(BASE, ".aether", "bulletin.md")
DB = os.path.join(BASE, "data", "market.db")
STRATEGIES = os.path.join(BASE, "config", "strategies.yaml")
OUTPUT = os.path.join(BASE, "dashboard.html")

AGENTS = {
    "oracle":    {"name":"Oracle 数据","icon":"🔵","color":"#3b82f6","role":"数据采集与质量管理"},
    "mercury":   {"name":"Mercury 交易","icon":"💹","color":"#22c55e","role":"信号执行与订单管理"},
    "athena":    {"name":"Athena 策略","icon":"🧠","color":"#a855f7","role":"策略评估与优化建议"},
    "guardian":  {"name":"Guardian 风控","icon":"🛡️","color":"#ef4444","role":"风险监控与告警"},
    "prometheus":{"name":"Prometheus 优化","icon":"🔥","color":"#f59e0b","role":"系统自优化引擎"},
}

now_utc = datetime.now(timezone.utc)
now_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

def load_json(path):
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except: return {}

def db_query(sql, params=()):
    if not os.path.exists(DB): return []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def load_bulletin():
    if not os.path.exists(BULLETIN): return []
    with open(BULLETIN) as f: return [l.strip() for l in f.readlines() if l.strip() and not l.startswith("|")]

def load_strategies():
    if not os.path.exists(STRATEGIES): return []
    try:
        with open(STRATEGIES) as f: return yaml.safe_load(f).get("strategies", [])
    except: return []

# ============ DATA COLLECTION ============

# DB stats
db_stats = {}
try:
    conn = sqlite3.connect(DB)
    for table in ["klines","trades","trades_log"]:
        try:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            db_stats[table] = cnt
        except: db_stats[table] = 0
    # klines by symbol/timeframe
    kline_data = conn.execute("SELECT symbol, timeframe, COUNT(*) as cnt, MAX(open_time) as latest FROM klines GROUP BY symbol, timeframe").fetchall()
    kline_detail = [{"symbol":r[0],"timeframe":r[1],"count":r[2],"latest":r[3]} for r in kline_data]
    conn.close()
except: kline_detail = []

# Trades
trades = db_query("SELECT * FROM trades_log ORDER BY id DESC LIMIT 100")
open_trades = [t for t in trades if t["status"] == "OPEN"]
closed_trades = [t for t in trades if t["status"] == "CLOSED"]
total_pnl = sum(t.get("pnl",0) or 0 for t in closed_trades)
wins = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
win_rate = len(wins)/len(closed_trades)*100 if closed_trades else 0

# Requests v2
import sys; sys.path.insert(0, os.path.join(BASE, ".aether"))
from request_system import get_all as get_all_requests, get_stats as get_req_stats
from task_system import get_tasks, TASK_STATUS
all_requests = get_all_requests()
req_stats = get_req_stats()

# Strategies
strategies = load_strategies()
active_strats = [s for s in strategies if s.get("enabled")]
inactive_strats = [s for s in strategies if not s.get("enabled")]

# Bulletin
bulletin_lines = load_bulletin()
recent_bulletin = [l for l in bulletin_lines[-40:] if l.strip()]

# ============ HTML GENERATION ============

def card(title, content, color="#3b82f6"):
    return f'<div class="card" style="border-top:3px solid {color}"><h3>{title}</h3><div class="card-content">{content}</div></div>'

def stat_row(items):
    return '<div class="stat-row">' + "".join(
        f'<div class="stat"><span class="stat-label">{k}</span><span class="stat-value">{v}</span></div>'
        for k,v in items
    ) + '</div>'

def badge(text, color="#252840"):
    return f'<span class="badge" style="background:{color}">{text}</span>'

def progress_bar(pct, label="", color="#22c55e"):
    return f'''<div class="progress-wrap">
      <span class="progress-label">{label}</span>
      <div class="progress-bar"><div class="progress-fill" style="width:{min(pct,100)}%;background:{color}"></div></div>
      <span class="progress-pct">{pct:.0f}%</span></div>'''

def agent_tasks(agent):
    """Render per-agent task queue."""
    tasks = get_tasks(agent)
    if not tasks: return ""
    h = '<div style="margin-top:8px;border-top:1px solid #1e2540;padding-top:6px"><div style="font-size:10px;color:#6b7280;margin-bottom:4px">📋 工作排期</div>'
    for t in tasks:
        ts = TASK_STATUS.get(t["status"], {"icon":"?","color":"#6b7280","label":t["status"]})
        h += f'<div style="display:flex;align-items:center;gap:6px;font-size:11px;padding:2px 0"><span style="color:{ts["color"]}">{ts["icon"]}</span><span style="flex:1">{t["title"]}</span><span style="color:{ts["color"]};font-size:10px">{ts["label"]}</span></div>'
    h += '</div>'
    return h

# ---- ORACLE SECTION ----
oracle_state = load_json(os.path.join(STATE_DIR, "oracle.json"))
pipeline_state = load_json(os.path.join(STATE_DIR, "pipeline.json"))
oracle_lines = []
pipe_status = pipeline_state.get("status", "unknown")
pipe_color = "#22c55e" if pipe_status == "running" else "#ef4444"
oracle_lines.append(stat_row([
    ("数据管道", f'<span style="color:{pipe_color}">● {pipe_status}</span>'),
    ("K线数据", str(db_stats.get("klines",0))+"行"),
    ("交易记录", str(db_stats.get("trades_log",0))+"笔"),
    ("最近采集", pipeline_state.get("last_run","N/A")[:16]),
]))
if kline_detail:
    oracle_lines.append('<table class="mini-table"><tr><th>标的</th><th>周期</th><th>K线数</th><th>最新时间</th></tr>')
    for k in kline_detail:
        oracle_lines.append(f'<tr><td>{k["symbol"]}</td><td>{k["timeframe"]}</td><td>{k["count"]}</td><td>{str(k["latest"])[:16]}</td></tr>')
    oracle_lines.append('</table>')
oracle_lines.append('<div class="note">数据消费者: Athena(回测), Mercury(信号), Prometheus(参数扫描)</div>')
oracle_lines.append(agent_tasks("oracle"))

# ---- ATHENA SECTION ----
athena_state = load_json(os.path.join(STATE_DIR, "athena.json"))
athena_lines = []
athena_lines.append(stat_row([
    ("活跃策略",str(len(active_strats))+"个"),
    ("禁用策略",str(len(inactive_strats))+"个"),
    ("最近评估",athena_state.get("_updated_at","N/A")[:16]),
]))
if active_strats:
    athena_lines.append('<table class="mini-table"><tr><th>策略名</th><th>标的</th><th>周期</th><th>状态</th></tr>')
    for s in active_strats:
        syms = ",".join(s.get("params",{}).get("symbols",["?"]))
        tfs = ",".join(s.get("params",{}).get("timeframes",["?"]))
        athena_lines.append(f'<tr><td>{s["name"]}</td><td>{syms}</td><td>{tfs}</td><td>{badge("启用","#22c55e")}</td></tr>')
    athena_lines.append('</table>')
# Try to get backtest metrics from athena state
bt = athena_state.get("backtest", {})
if bt:
    athena_lines.append(stat_row([
        ("净收益",f'{bt.get("net","?"):+.1f}%'),
        ("夏普",f'{bt.get("sharpe","?"):.2f}'),
        ("回撤",f'{bt.get("dd","?"):.1f}%'),
        ("胜率",f'{bt.get("wr","?"):.0f}%'),
    ]))

athena_lines.append(agent_tasks("athena"))
# ---- GUARDIAN SECTION ----
guardian_state = load_json(os.path.join(STATE_DIR, "guardian.json"))
guardian_lines = []
guardian_lines.append(stat_row([
    ("持仓数",str(len(open_trades))+"笔"),
    ("风险等级","🟢 正常" if len(open_trades)<3 else "🟡 关注"),
    ("最近检查",guardian_state.get("_updated_at","N/A")[:16]),
]))
if open_trades:
    guardian_lines.append('<table class="mini-table"><tr><th>标的</th><th>方向</th><th>入场</th><th>数量</th></tr>')
    for t in open_trades:
        guardian_lines.append(f'<tr><td>{t["symbol"]}</td><td>{t["side"]}</td><td>{t["entry_price"]}</td><td>{t["quantity"]}</td></tr>')
    guardian_lines.append('</table>')

guardian_lines.append(agent_tasks("guardian"))
# ---- MERCURY SECTION ----
mercury_state = load_json(os.path.join(STATE_DIR, "mercury.json"))
mercury_lines = []
mercury_lines.append(stat_row([
    ("总交易",str(len(trades))+"笔"),
    ("持仓中",str(len(open_trades))+"笔"),
    ("已平仓",str(len(closed_trades))+"笔"),
    ("胜率",f'{win_rate:.0f}%'),
    ("累计盈亏",f'{total_pnl:+.4f}'),
]))
if trades:
    mercury_lines.append(progress_bar(win_rate, "胜率", "#22c55e" if win_rate>50 else "#f59e0b"))
    mercury_lines.append('<table class="mini-table"><tr><th>ID</th><th>标的</th><th>方向</th><th>入场</th><th>出场</th><th>PnL</th><th>状态</th></tr>')
    for t in trades[:15]:
        pnl = t.get("pnl") or 0
        pc = "#22c55e" if float(pnl)>0 else ("#ef4444" if float(pnl)<0 else "#6b7280")
        mercury_lines.append(f'<tr><td>#{t["id"]}</td><td>{t["symbol"]}</td><td>{t["side"]}</td><td>{t["entry_price"]}</td><td>{t.get("exit_price") or "—"}</td><td style="color:{pc}">{float(pnl):+.4f}</td><td>{badge(t["status"],"#22c55e" if t["status"]=="OPEN" else "#6b7280")}</td></tr>')
    mercury_lines.append('</table>')

mercury_lines.append(agent_tasks("mercury"))
# ---- PROMETHEUS SECTION ----
prom_state = load_json(os.path.join(STATE_DIR, "prometheus.json"))
prom_lines = []
prom_lines.append(stat_row([
    ("状态",prom_state.get("status","active")),
    ("最近行动",prom_state.get("last_action","初始化中")),
    ("更新",prom_state.get("_updated_at","N/A")[:16]),
]))
# Research-based tasks
prom_lines.append('<div class="task-list">')
prom_lines.append('<div class="task-item"><span class="task-dot" style="background:#22c55e"></span> 论文调研完成 — 7篇,6个可行方向</div>')
prom_lines.append('<div class="task-item"><span class="task-dot" style="background:#f59e0b"></span> 动态网格(DGT)策略 — 评估中</div>')
prom_lines.append('<div class="task-item"><span class="task-dot" style="background:#6b7280"></span> 防过拟合框架 — 待启动</div>')
prom_lines.append('<div class="task-item"><span class="task-dot" style="background:#6b7280"></span> 波动率预测ML — 待启动</div>')
prom_lines.append('</div>')

prom_lines.append(agent_tasks("prometheus"))
# ---- REQUESTS TABLE ----
req_html = ""
p_count = req_stats["pending"] + req_stats["acknowledged"] + req_stats["processing"]
f_count = req_stats["fulfilled"]
r_count = req_stats["rejected"]
req_html = f'<div class="stat-row">'
req_html += f'<div class="stat"><span class="stat-label">待处理</span><span class="stat-value" style="color:#f59e0b">{p_count}</span></div>'
req_html += f'<div class="stat"><span class="stat-label">已完成</span><span class="stat-value" style="color:#22c55e">{f_count}</span></div>'
req_html += f'<div class="stat"><span class="stat-label">已拒绝</span><span class="stat-value" style="color:#ef4444">{r_count}</span></div>'
req_html += f'<div class="stat"><span class="stat-label">总计</span><span class="stat-value">{req_stats["total"]}</span></div>'
req_html += '</div>'

if all_requests:
    status_names = {"pending":"⏳ 待接收","acknowledged":"📨 已接收","processing":"🔄 处理中","fulfilled":"✅ 已完成","rejected":"❌ 已拒绝"}
    status_colors = {"pending":"#f59e0b","acknowledged":"#3b82f6","processing":"#a855f7","fulfilled":"#22c55e","rejected":"#ef4444"}
    for r in all_requests[-15:]:
        sc = status_colors.get(r["status"],"#6b7280")
        sn = status_names.get(r["status"],r["status"])
        req_html += f'<div class="request-card">'
        req_html += f'<div class="req-header"><span class="req-id">#{r["id"]}</span> <b>{r.get("from","?")}</b> → <b>{r.get("target","?")}</b> · {r.get("type","?")}</div>'
        req_html += f'<div class="req-reason">📝 {r.get("data",{}).get("reason","无说明")}</div>'
        # Timeline
        timeline = r.get("timeline", [])
        if timeline:
            req_html += '<div class="req-timeline">'
            for step in timeline:
                tsc = status_colors.get(step.get("status",""),"#6b7280")
                req_html += f'<div class="tl-step"><span class="tl-dot" style="background:{tsc}"></span> <span class="tl-time">{step.get("time","?")}</span> <span class="tl-msg">{step.get("msg","")}</span></div>'
            req_html += '</div>'
        # Result
        if r.get("result"):
            req_html += f'<div class="req-result">📊 结果: {json.dumps(r["result"], ensure_ascii=False)}</div>'
        req_html += f'<span class="req-status" style="color:{sc}">{sn}</span>'
        req_html += '</div>'
else:
    req_html += '<div class="empty">暂无请求记录</div>'

# ---- BULLETIN FEED ----
bulletin_html = ""
agent_classes = {"Oracle":"oracle","Athena":"athena","Guardian":"guardian","Mercury":"mercury","Prometheus":"prometheus"}
for line in recent_bulletin[-30:]:
    line_clean = line.replace("#","").replace("---","").strip()
    if not line_clean: continue
    cls = ""
    for aname, aclass in agent_classes.items():
        if aname in line_clean: cls = aclass; break
    bulletin_html += f'<div class="bulletin-item {cls}">{line_clean[:150]}</div>'

# ---- FULL HTML ----
html = f'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>Aether 运营指挥中心</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0c15;color:#d1d5db;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:24px;line-height:1.5}}
h1{{font-size:26px;margin-bottom:4px;color:#f8fafc}} h1 span{{color:#f59e0b;font-weight:300}}
.subtitle{{color:#6b7280;font-size:12px;margin-bottom:24px}}
.grid2{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px;margin-bottom:20px}}
.grid3{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:20px}}
.card{{background:#141829;border-radius:12px;padding:18px;border:1px solid #1e2540}}
.card h3{{font-size:15px;margin-bottom:12px;display:flex;align-items:center;gap:8px}}
.card-content{{font-size:13px}}
.stat-row{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.stat{{background:#1a1f35;padding:8px 12px;border-radius:8px;flex:1;min-width:80px;text-align:center}}
.stat-label{{display:block;font-size:10px;color:#6b7280;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.5px}}
.stat-value{{display:block;font-size:16px;font-weight:700;color:#f8fafc}}
.progress-wrap{{display:flex;align-items:center;gap:10px;margin:10px 0;font-size:12px}}
.progress-label{{width:50px;color:#9ca3af}}
.progress-bar{{flex:1;height:6px;background:#1e2540;border-radius:3px;overflow:hidden}}
.progress-fill{{height:100%;border-radius:3px;transition:width 0.5s}}
.progress-pct{{width:40px;text-align:right;color:#9ca3af;font-variant-numeric:tabular-nums}}
.mini-table{{width:100%;border-collapse:collapse;font-size:11px;margin:8px 0}}
.mini-table th{{text-align:left;padding:6px 8px;border-bottom:1px solid #1e2540;color:#6b7280;font-weight:500}}
.mini-table td{{padding:5px 8px;border-bottom:1px solid #141829}}
.badge{{padding:2px 8px;border-radius:4px;font-size:10px;color:#fff;white-space:nowrap}}
.reason{{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9ca3af}}
.note{{color:#6b7280;font-size:11px;margin-top:6px;font-style:italic}}
.task-list{{margin-top:8px}}
.task-item{{display:flex;align-items:center;gap:8px;padding:5px 0;font-size:12px;color:#9ca3af}}
.task-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.bulletin-item{{padding:6px 12px;border-left:3px solid #1e2540;margin:3px 0;font-size:11px;border-radius:0 6px 6px 0;background:#0d101e}}
.bulletin-item.oracle{{border-left-color:#3b82f6}}
.bulletin-item.mercury{{border-left-color:#22c55e}}
.bulletin-item.athena{{border-left-color:#a855f7}}
.bulletin-item.guardian{{border-left-color:#ef4444}}
.bulletin-item.prometheus{{border-left-color:#f59e0b}}
.empty{{color:#6b7280;text-align:center;padding:30px;font-size:13px}}
.request-card{{background:#0d101e;border-radius:8px;padding:12px;margin:8px 0;border:1px solid #1e2540}}
.req-header{{font-size:13px;margin-bottom:6px}}
.req-id{{background:#1e2540;padding:2px 8px;border-radius:4px;font-size:11px;margin-right:8px}}
.req-reason{{font-size:11px;color:#9ca3af;margin-bottom:8px}}
.req-timeline{{margin:6px 0;padding-left:4px}}
.tl-step{{display:flex;align-items:center;gap:8px;font-size:11px;padding:2px 0;color:#9ca3af}}
.tl-dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0}}
.tl-time{{color:#6b7280;font-size:10px;width:85px;flex-shrink:0}}
.tl-msg{{flex:1}}
.req-result{{background:#141829;padding:6px 10px;border-radius:4px;font-size:11px;margin:6px 0;color:#22c55e}}
.req-status{{font-size:11px;font-weight:600}}
.footer{{text-align:center;color:#374151;font-size:11px;margin-top:30px;padding:16px;border-top:1px solid #1e2540}}
.section-title{{font-size:18px;color:#f8fafc;margin:24px 0 12px;padding-bottom:8px;border-bottom:1px solid #1e2540}}
</style>
</head>
<body>
<h1>⚡ Aether <span>运营指挥中心</span></h1>
<div class="subtitle">自动刷新 · 每5分钟 · {now_str} · <span id="ago">刚刚</span></div>

<div class="grid3">
{card("🔵 Oracle 数据专员","".join(oracle_lines), "#3b82f6")}
{card("🧠 Athena 策略专员","".join(athena_lines), "#a855f7")}
{card("🛡️ Guardian 风控专员","".join(guardian_lines), "#ef4444")}
</div>

<div class="grid2">
{card("💹 Mercury 交易专员","".join(mercury_lines), "#22c55e")}
{card("🔥 Prometheus 优化专员","".join(prom_lines), "#f59e0b")}
</div>

<div class="section-title">📨 专员间协作请求 · 生命周期追踪</div>
<div class="card">
<h3>请求队列 (⏳{p_count}处理中 / ✅{f_count}已完成)</h3>
{req_html}
</div>

<div class="section-title">📋 实时公告板</div>
<div class="card">
{bulletin_html or '<div class="empty">公告板为空,等待专员首次报告...</div>'}
</div>

<div class="footer">
Aether Dashboard · 每5分钟自动刷新 · {now_str}<br>
专员: Oracle(15m) | Mercury(15m) | Athena(20m) | Guardian(20m) | Prometheus(20m)
</div>
<script>
document.getElementById("ago").textContent = new Date().toLocaleString();
</script>
</body>
</html>'''

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w", encoding="utf-8") as f:
    f.write(html)
print(f"Dashboard: {OUTPUT} ({len(html)} bytes)")
