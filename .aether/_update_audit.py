#!/usr/bin/env python3
"""Update audit findings to reflect fixes applied."""
import json, os
from datetime import datetime, timezone

af_path = '.aether/state/audit_findings.json'
with open(af_path) as f:
    af = json.load(f)

now = datetime.now(timezone.utc).isoformat()

updates = {
    "AUDIT-008": {
        "status": "resolved",
        "resolved": now,
        "resolution": "MLEnsemble_BTC和RegimeSwitch_BTC已在strategies.yaml中禁用(enabled:false)。ML盲飞风险已消除。backtest引擎参数签名已修复(engine.py ML调度对齐athena_backtest函数签名)，所有14策略backtest_results.json状态=ok无error。MLEnsemble_BTC 30d结果: +1.16% Sharpe +0.12 — 确认低预测力但已禁用安全。"
    },
    "AUDIT-012": {
        "status": "resolved", 
        "resolved": now,
        "resolution": "Oracle单方面启用的TrendFollow_BTC_1h和RSI_MR_ETH已重新禁用(AUDIT-012确认)。策略启停决策现在由Argus审计→Hermes Dispatcher→专员确认的治理流程控制，冲突仲裁机制(AUDIT-018)防止单方面撤销。"
    },
    "AUDIT-013": {
        "status": "resolved",
        "resolved": now,
        "resolution": "TrendFollow_BTC_1h已禁用。7d回测确认为CONSISTENT_LOSER(Sharpe -1.97, WR 0%, 3笔)。30d(+82.24%)与7d(-3.43%)矛盾已查明为时间窗口差异——近期市场条件下策略失效，长期回测受幸存者偏差影响。"
    },
    "AUDIT-014": {
        "status": "resolved",
        "resolved": now,
        "resolution": "RSI_MR_ETH已禁用。参数os=20/ob=80极端导致7d零信号零交易。30d回测确认-8.54% Sharpe -0.53——该策略在当前参数下不具可交易性。"
    },
    "AUDIT-017": {
        "status": "resolved",
        "resolved": now,
        "resolution": "engine.py ML调度参数签名已修复：MLAlpha→5参数(对齐mlalpha_signals)，MLEnsemble→6参数(对齐mlensemble_signals)，RegimeSwitch→13参数(对齐regimeswitch_signals)。backtest_results.json所有14策略status=ok，0 error。固定参数签名消除了engine实盘与backtest之间的调用不一致。"
    },
    "AUDIT-018": {
        "status": "resolved",
        "resolved": now,
        "resolution": "冲突仲裁机制已添加至engine.py run_signal_check()。当同一标的多策略产生信号时，按(1)backtest验证状态(2)Sharpe比率(3)收益率的优先级排序。同向信号取最优策略，反向信号按Score比较胜出。平局则全部HOLD不执行。日志记录所有仲裁决策。"
    },
}

for fid, update in updates.items():
    for f in af['findings']:
        if f['id'] == fid and f['status'] in ('open', 'escalated'):
            f['status'] = update['status']
            f['resolved'] = update['resolved']
            if 'resolution' in update:
                f['resolution'] = update['resolution']
            print(f"  ✅ {fid} → {update['status']}")

# Update AUDIT-009 note — engine restarted, needs verification
for f in af['findings']:
    if f['id'] == 'AUDIT-009':
        f['note'] = "Audit #19: engine.py PID已重启。risk_check.json显示balance=0(engine重启后首次运行前)。需下一轮心跳后验证。风控模块代码未变，根因为engine.py进程中断。"

# Update AUDIT-016 note
for f in af['findings']:
    if f['id'] == 'AUDIT-016':
        f['note'] = "Audit #19: 90d数据已在oracle.json和prometheus.json专用区域。7d主数据区与90d的矛盾源于不同时间窗口自然差异(如TrendFollow_BTC_1h 7d=-3.43% vs 30d=+82.24%)。建议Oracle统一回测窗口或标注窗口差异。"

af['last_audit'] = now
with open(af_path, 'w') as f:
    json.dump(af, f, indent=2, ensure_ascii=False)
print(f"\n📝 audit_findings.json updated ({len(updates)} resolved)")
