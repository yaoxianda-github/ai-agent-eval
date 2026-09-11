#!/bin/bash
# 监控 hermes-agent 补跑批次，完成后自动生成合并对比报告
set -e

BATCH_ID="${1:-55d9e670344e}"
CLAUDE_BATCH="0ba78cabe02f"
REPORT_FILE="logs/compare-analysis-merged-$(date +%Y%m%d-%H%M%S).md"
API="http://127.0.0.1:8000"

echo "[$(date '+%H:%M:%S')] 开始监控 hermes-agent 补跑批次 $BATCH_ID..."
echo "报告将输出到: $REPORT_FILE"

while true; do
    STATUS=$(curl -s "$API/api/batches/$BATCH_ID" | python3 -c "
import sys, json
d = json.load(sys.stdin)
b = d.get('batch', d)
print(f\"{b.get('status')}|{b.get('done_runs',0)}|{b.get('total_runs',0)}\")
" 2>/dev/null || echo "error|0|0")
    
    STATE=$(echo "$STATUS" | cut -d'|' -f1)
    DONE=$(echo "$STATUS" | cut -d'|' -f2)
    TOTAL=$(echo "$STATUS" | cut -d'|' -f3)
    
    echo "[$(date '+%H:%M:%S')] 状态: $STATE | 进度: $DONE/$TOTAL"
    
    if [ "$STATE" = "done" ]; then
        echo "[$(date '+%H:%M:%S')] 批次完成！开始生成合并对比分析报告..."
        break
    fi
    
    if [ "$STATE" = "error" ]; then
        echo "[$(date '+%H:%M:%S')] 获取状态失败，重试..."
    fi
    
    sleep 30
done

# 生成合并对比分析报告
python3 << PYEOF
import sqlite3
from collections import defaultdict
from pathlib import Path
from datetime import datetime

DB = "results/run_history.db"
CLAUDE_BATCH = "$CLAUDE_BATCH"
HERMES_BATCH = "$BATCH_ID"
REPORT_FILE = "$REPORT_FILE"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 获取两个批次的所有运行
cur.execute("""
    SELECT * FROM runs WHERE batch_id IN (?, ?) ORDER BY agent_id, task_id
""", (CLAUDE_BATCH, HERMES_BATCH))
runs = [dict(row) for row in cur.fetchall()]

print(f"获取到 {len(runs)} 条运行记录（claude-code + hermes-agent）")

# 按 agent 分组
by_agent = defaultdict(list)
for r in runs:
    by_agent[r.get('agent_id', 'unknown')].append(r)

agents = sorted(by_agent.keys())
print(f"Agents: {agents}")

# 计算指标
metrics = {}
for agent in agents:
    agent_runs = by_agent[agent]
    total = len(agent_runs)
    passed = sum(1 for r in agent_runs if r.get('score', 0) >= 1.0)
    completed = sum(1 for r in agent_runs if r.get('status') == 'completed')
    failed = sum(1 for r in agent_runs if r.get('status') == 'failed')
    errors = sum(1 for r in agent_runs if r.get('status') not in ('completed', 'failed'))
    avg_score = sum(r.get('score', 0) for r in agent_runs) / total if total else 0
    avg_duration = sum(r.get('duration_s', 0) for r in agent_runs) / total if total else 0
    avg_steps = sum(r.get('steps', 0) for r in agent_runs) / total if total else 0
    total_steps = sum(r.get('steps', 0) for r in agent_runs)
    
    metrics[agent] = {
        'total': total, 'passed': passed, 'completed': completed,
        'failed': failed, 'errors': errors, 'avg_score': avg_score,
        'avg_duration': avg_duration, 'avg_steps': avg_steps,
        'total_steps': total_steps, 'pass_rate': passed/total if total else 0
    }

# 生成报告
report = []
report.append(f"# 全量对比分析报告: claude-code vs hermes-agent（合并数据）")
report.append(f"")
report.append(f"- **claude-code 批次**: {CLAUDE_BATCH}")
report.append(f"- **hermes-agent 批次**: {HERMES_BATCH}（修复 timeout_s 后补跑）")
report.append(f"- **总运行数**: {len(runs)}")
report.append(f"- **任务数**: 32")
report.append(f"- **模型**: claude-opus-4-8（两者相同）")
report.append(f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
report.append(f"")

# 总体对比
report.append(f"## 1. 总体对比")
report.append(f"")
report.append(f"| 指标 | claude-code | hermes-agent | 差异 |")
report.append(f"|------|-------------|--------------|------|")

for label, key, fmt in [
    ('任务总数', 'total', '{:.0f}'),
    ('通过数(score=1.0)', 'passed', '{:.0f}'),
    ('通过率', 'pass_rate', '{:.1%}'),
    ('完成数', 'completed', '{:.0f}'),
    ('失败数', 'failed', '{:.0f}'),
    ('异常数', 'errors', '{:.0f}'),
    ('平均得分', 'avg_score', '{:.3f}'),
    ('平均耗时(s)', 'avg_duration', '{:.1f}'),
    ('平均步骤数', 'avg_steps', '{:.1f}'),
    ('总步骤数', 'total_steps', '{:,.0f}'),
]:
    a = metrics.get(agents[0], {}).get(key, 0) if len(agents) > 0 else 0
    b = metrics.get(agents[1], {}).get(key, 0) if len(agents) > 1 else 0
    diff = b - a
    if isinstance(diff, float):
        diff_str = f"{diff:+.3f}" if 'score' in key or 'rate' in key else f"{diff:+.1f}"
    else:
        diff_str = f"{diff:+d}"
    report.append(f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {diff_str} |")

report.append(f"")

# 按难度等级对比
report.append(f"## 2. 按难度等级对比")
report.append(f"")

levels = ['L1', 'L2', 'L3', 'L4', 'L5']
for level in levels:
    level_runs = [r for r in runs if r.get('task_level', '') == level]
    if not level_runs:
        continue
    
    report.append(f"### {level}")
    report.append(f"")
    report.append(f"| Agent | 任务数 | 通过数 | 通过率 | 平均得分 | 平均耗时(s) | 平均步骤 |")
    report.append(f"|-------|--------|--------|--------|----------|-------------|----------|")
    
    for agent in agents:
        ar = [r for r in level_runs if r.get('agent_id') == agent]
        total = len(ar)
        passed = sum(1 for r in ar if r.get('score', 0) >= 1.0)
        avg_score = sum(r.get('score', 0) for r in ar) / total if total else 0
        avg_duration = sum(r.get('duration_s', 0) for r in ar) / total if total else 0
        avg_steps = sum(r.get('steps', 0) for r in ar) / total if total else 0
        report.append(f"| {agent} | {total} | {passed} | {passed/total:.1%} | {avg_score:.3f} | {avg_duration:.1f} | {avg_steps:.1f} |")
    
    report.append(f"")

# 未通过任务分析
report.append(f"## 3. 未通过任务分析")
report.append(f"")

for agent in agents:
    failed_tasks = [r for r in by_agent[agent] if r.get('score', 0) < 1.0]
    report.append(f"### {agent} 未通过任务 ({len(failed_tasks)} 个)")
    report.append(f"")
    if failed_tasks:
        report.append(f"| 任务 ID | 难度 | 状态 | 得分 | 耗时(s) | 步骤 |")
        report.append(f"|---------|------|------|------|---------|------|")
        for r in failed_tasks:
            report.append(f"| {r.get('task_id')} | {r.get('task_level')} | {r.get('status')} | {r.get('score', 0):.3f} | {r.get('duration_s', 0):.1f} | {r.get('steps', 0)} |")
    else:
        report.append(f"全部通过！")
    report.append(f"")

# 两者都未通过的任务
report.append(f"### 两者都未通过的任务")
report.append(f"")
a1_failed = set(r['task_id'] for r in by_agent[agents[0]] if r.get('score', 0) < 1.0) if len(agents) > 0 else set()
a2_failed = set(r['task_id'] for r in by_agent[agents[1]] if r.get('score', 0) < 1.0) if len(agents) > 1 else set()
both_failed = a1_failed & a2_failed
if both_failed:
    report.append(f"共 {len(both_failed)} 个任务两者都未通过：{', '.join(sorted(both_failed))}")
else:
    report.append(f"没有两者都未通过的任务")
report.append(f"")

# 仅一方未通过的任务
report.append(f"### 仅 claude-code 未通过")
report.append(f"")
only_a1 = a1_failed - a2_failed
if only_a1:
    report.append(f"共 {len(only_a1)} 个：{', '.join(sorted(only_a1))}")
else:
    report.append(f"无")
report.append(f"")

report.append(f"### 仅 hermes-agent 未通过")
report.append(f"")
only_a2 = a2_failed - a1_failed
if only_a2:
    report.append(f"共 {len(only_a2)} 个：{', '.join(sorted(only_a2))}")
else:
    report.append(f"无")
report.append(f"")

# 能力差异分析
report.append(f"## 4. 能力差异分析")
report.append(f"")

report.append(f"### 4.1 任务完成稳定性")
report.append(f"")
for agent in agents:
    m = metrics[agent]
    report.append(f"- **{agent}**: 通过率 {m['pass_rate']:.1%}, 完成 {m['completed']} 个, 失败 {m['failed']} 个, 异常 {m['errors']} 个")
report.append(f"")

report.append(f"### 4.2 效率对比")
report.append(f"")
for agent in agents:
    m = metrics[agent]
    report.append(f"- **{agent}**: 平均耗时 {m['avg_duration']:.1f}s, 平均步骤 {m['avg_steps']:.1f}, 总步骤 {m['total_steps']:,}")
report.append(f"")

report.append(f"### 4.3 难度适应性")
report.append(f"")
report.append(f"分析各 Agent 在 L1-L5 不同难度下的表现差异（见上方表格）。")
report.append(f"")

# 结论与建议
report.append(f"## 5. 结论与建议")
report.append(f"")

a1, a2 = agents[0], agents[1] if len(agents) > 1 else ''
m1, m2 = metrics.get(a1, {}), metrics.get(a2, {})

if m1.get('pass_rate', 0) > m2.get('pass_rate', 0):
    report.append(f"1. **通过率**: {a1} ({m1['pass_rate']:.1%}) 优于 {a2} ({m2['pass_rate']:.1%})，领先 {(m1['pass_rate']-m2['pass_rate'])*100:.1f} 个百分点")
elif m2.get('pass_rate', 0) > m1.get('pass_rate', 0):
    report.append(f"1. **通过率**: {a2} ({m2['pass_rate']:.1%}) 优于 {a1} ({m1['pass_rate']:.1%})，领先 {(m2['pass_rate']-m1['pass_rate'])*100:.1f} 个百分点")
else:
    report.append(f"1. **通过率**: 两者持平 ({m1['pass_rate']:.1%})")

if m1.get('avg_duration', 0) < m2.get('avg_duration', 0):
    report.append(f"2. **效率**: {a1} (平均 {m1['avg_duration']:.1f}s) 快于 {a2} (平均 {m2['avg_duration']:.1f}s)，快 {(m2['avg_duration']-m1['avg_duration'])/m2['avg_duration']*100:.1f}%")
else:
    report.append(f"2. **效率**: {a2} (平均 {m2['avg_duration']:.1f}s) 快于 {a1} (平均 {m1['avg_duration']:.1f}s)，快 {(m1['avg_duration']-m2['avg_duration'])/m1['avg_duration']*100:.1f}%")

if m1.get('avg_steps', 0) < m2.get('avg_steps', 0):
    report.append(f"3. **步骤效率**: {a1} (平均 {m1['avg_steps']:.1f} 步) 比 {a2} (平均 {m2['avg_steps']:.1f} 步) 更简洁")
else:
    report.append(f"3. **步骤效率**: {a2} (平均 {m2['avg_steps']:.1f} 步) 比 {a1} (平均 {m1['avg_steps']:.1f} 步) 更简洁")

report.append(f"")
report.append(f"### 改进建议")
report.append(f"")
report.append(f"1. 针对未通过任务，查看运行详情中的步骤轨迹，分析具体失败原因（工具调用问题、理解偏差、执行错误）")
report.append(f"2. 对比两者在相同任务上的步骤轨迹，分析决策路径差异和工具使用策略")
report.append(f"3. 考虑增加 runs=3 采样，评估非确定性任务的稳定性和方差")
report.append(f"4. 针对高难度任务（L4/L5），可能需要调整 max_steps 或超时设置")
report.append(f"5. hermes-agent 后端可进一步优化：从 state.db 提取更完整的 observation 和 token 用量")

report.append(f"")
report.append(f"---")
report.append(f"*报告由 compare-analysis 自动生成（合并 claude-code 批次 {CLAUDE_BATCH} + hermes-agent 批次 {HERMES_BATCH}）*")

# 写入报告
Path(REPORT_FILE).write_text('\n'.join(report), encoding='utf-8')
print(f"报告已生成: {REPORT_FILE}")
print(f"报告长度: {len(report)} 行")

conn.close()
PYEOF

echo "[$(date '+%H:%M:%S')] 监控完成！"
echo "报告文件: $REPORT_FILE"
