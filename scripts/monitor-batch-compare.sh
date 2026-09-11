#!/bin/bash
# 监控全量对比批次完成，完成后自动生成对比分析报告
# 用法: ./scripts/monitor-batch-compare.sh <batch_id>

BATCH_ID="${1:-0ba78cabe02f}"
PROJECT_DIR="/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval"
DB="${PROJECT_DIR}/results/run_history.db"
REPORT_FILE="${PROJECT_DIR}/logs/compare-analysis-${BATCH_ID}.md"
API="http://127.0.0.1:8000"

cd "${PROJECT_DIR}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始监控批次 ${BATCH_ID}..."
echo "报告将输出到: ${REPORT_FILE}"

# 等待批次完成
while true; do
    STATUS=$(curl -s "${API}/api/batches/${BATCH_ID}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
b = d.get('batch', d)
print(b.get('status', 'unknown'))
" 2>/dev/null)
    
    DONE=$(curl -s "${API}/api/batches/${BATCH_ID}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
b = d.get('batch', d)
print(f'{b.get(\"done_runs\", 0)}/{b.get(\"total_runs\", 0)}')
" 2>/dev/null)
    
    echo "[$(date '+%H:%M:%S')] 状态: ${STATUS} | 进度: ${DONE}"
    
    if [ "${STATUS}" = "done" ] || [ "${STATUS}" = "completed" ]; then
        echo "[$(date '+%H:%M:%S')] 批次完成！开始生成对比分析报告..."
        break
    fi
    
    if [ "${STATUS}" = "failed" ] || [ "${STATUS}" = "error" ]; then
        echo "[$(date '+%H:%M:%S')] 批次失败！"
        break
    fi
    
    sleep 30
done

# 生成对比分析报告
python3 << 'PYEOF'
import sqlite3
import json
from collections import defaultdict
from pathlib import Path

DB = "/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval/results/run_history.db"
BATCH_ID = "0ba78cabe02f"
REPORT_FILE = f"/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval/logs/compare-analysis-{BATCH_ID}.md"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 获取批次的所有运行
cur.execute("""
    SELECT r.* FROM runs r
    WHERE r.batch_id = ?
    ORDER BY r.agent, r.task_id
""", (BATCH_ID,))
runs = [dict(row) for row in cur.fetchall()]

print(f"获取到 {len(runs)} 条运行记录")

# 按 agent 分组
by_agent = defaultdict(list)
for r in runs:
    by_agent[r.get('agent', 'unknown')].append(r)

agents = sorted(by_agent.keys())
print(f"Agents: {agents}")

# 生成报告
report = []
report.append(f"# 全量对比分析报告: claude-code vs hermes-agent")
report.append(f"")
report.append(f"- **批次 ID**: {BATCH_ID}")
report.append(f"- **总运行数**: {len(runs)}")
report.append(f"- **任务数**: 32")
report.append(f"- **模型**: claude-opus-4-8")
report.append(f"- **生成时间**: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
report.append(f"")

# 总体对比
report.append(f"## 1. 总体对比")
report.append(f"")
report.append(f"| 指标 | claude-code | hermes-agent | 差异 |")
report.append(f"|------|-------------|--------------|------|")

metrics = {}
for agent in agents:
    agent_runs = by_agent[agent]
    total = len(agent_runs)
    passed = sum(1 for r in agent_runs if r.get('status') == 'completed' and r.get('score', 0) >= 1.0)
    completed = sum(1 for r in agent_runs if r.get('status') == 'completed')
    failed = sum(1 for r in agent_runs if r.get('status') == 'failed')
    errors = sum(1 for r in agent_runs if r.get('status') == 'error')
    avg_score = sum(r.get('score', 0) for r in agent_runs) / total if total else 0
    avg_duration = sum(r.get('duration_s', 0) for r in agent_runs) / total if total else 0
    total_tokens = sum(r.get('total_tokens', 0) for r in agent_runs)
    total_cost = sum(r.get('cost_usd', 0) for r in agent_runs)
    
    metrics[agent] = {
        'total': total, 'passed': passed, 'completed': completed,
        'failed': failed, 'errors': errors, 'avg_score': avg_score,
        'avg_duration': avg_duration, 'total_tokens': total_tokens,
        'total_cost': total_cost, 'pass_rate': passed/total if total else 0
    }

for label, key, fmt in [
    ('任务总数', 'total', '{:.0f}'),
    ('通过数', 'passed', '{:.0f}'),
    ('通过率', 'pass_rate', '{:.1%}'),
    ('完成数', 'completed', '{:.0f}'),
    ('失败数', 'failed', '{:.0f}'),
    ('错误数', 'errors', '{:.0f}'),
    ('平均得分', 'avg_score', '{:.2f}'),
    ('平均耗时(s)', 'avg_duration', '{:.1f}'),
    ('总 tokens', 'total_tokens', '{:,.0f}'),
    ('总成本(USD)', 'total_cost', '${:.4f}'),
]:
    a = metrics.get(agents[0], {}).get(key, 0) if len(agents) > 0 else 0
    b = metrics.get(agents[1], {}).get(key, 0) if len(agents) > 1 else 0
    diff = b - a
    diff_str = f"{diff:+.2f}" if isinstance(diff, float) else f"{diff:+d}"
    report.append(f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {diff_str} |")

report.append(f"")

# 按难度等级对比
report.append(f"## 2. 按难度等级对比")
report.append(f"")

levels = ['L1', 'L2', 'L3', 'L4', 'L5']
for level in levels:
    level_runs = [r for r in runs if r.get('task_id', '').startswith(level[0])]
    if not level_runs:
        continue
    
    report.append(f"### {level}")
    report.append(f"")
    report.append(f"| Agent | 任务数 | 通过数 | 通过率 | 平均得分 | 平均耗时(s) |")
    report.append(f"|-------|--------|--------|--------|----------|-------------|")
    
    for agent in agents:
        ar = [r for r in level_runs if r.get('agent') == agent]
        total = len(ar)
        passed = sum(1 for r in ar if r.get('score', 0) >= 1.0)
        avg_score = sum(r.get('score', 0) for r in ar) / total if total else 0
        avg_duration = sum(r.get('duration_s', 0) for r in ar) / total if total else 0
        report.append(f"| {agent} | {total} | {passed} | {passed/total:.1%} | {avg_score:.2f} | {avg_duration:.1f} |")
    
    report.append(f"")

# 未通过任务分析
report.append(f"## 3. 未通过任务分析")
report.append(f"")

for agent in agents:
    failed_tasks = [r for r in by_agent[agent] if r.get('score', 0) < 1.0]
    report.append(f"### {agent} 未通过任务 ({len(failed_tasks)} 个)")
    report.append(f"")
    if failed_tasks:
        report.append(f"| 任务 ID | 状态 | 得分 | 耗时(s) | 错误信息 |")
        report.append(f"|---------|------|------|---------|----------|")
        for r in failed_tasks:
            err = (r.get('error') or '')[:50].replace('|', '\\|')
            report.append(f"| {r.get('task_id')} | {r.get('status')} | {r.get('score', 0):.2f} | {r.get('duration_s', 0):.1f} | {err} |")
    else:
        report.append(f"全部通过！")
    report.append(f"")

# 能力差异分析
report.append(f"## 4. 能力差异分析")
report.append(f"")
report.append(f"### 4.1 任务完成稳定性")
report.append(f"")
for agent in agents:
    m = metrics[agent]
    report.append(f"- **{agent}**: 通过率 {m['pass_rate']:.1%}, 失败 {m['failed']} 个, 错误 {m['errors']} 个")
report.append(f"")

report.append(f"### 4.2 效率对比")
report.append(f"")
for agent in agents:
    m = metrics[agent]
    report.append(f"- **{agent}**: 平均耗时 {m['avg_duration']:.1f}s, 总 tokens {m['total_tokens']:,}, 总成本 ${m['total_cost']:.4f}")
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
    report.append(f"1. **通过率**: {a1} ({m1['pass_rate']:.1%}) 优于 {a2} ({m2['pass_rate']:.1%})")
elif m2.get('pass_rate', 0) > m1.get('pass_rate', 0):
    report.append(f"1. **通过率**: {a2} ({m2['pass_rate']:.1%}) 优于 {a1} ({m1['pass_rate']:.1%})")
else:
    report.append(f"1. **通过率**: 两者持平 ({m1['pass_rate']:.1%})")

if m1.get('avg_duration', 0) < m2.get('avg_duration', 0):
    report.append(f"2. **效率**: {a1} (平均 {m1['avg_duration']:.1f}s) 快于 {a2} (平均 {m2['avg_duration']:.1f}s)")
else:
    report.append(f"2. **效率**: {a2} (平均 {m2['avg_duration']:.1f}s) 快于 {a1} (平均 {m1['avg_duration']:.1f}s)")

if m1.get('total_cost', 0) < m2.get('total_cost', 0):
    report.append(f"3. **成本**: {a1} (${m1['total_cost']:.4f}) 低于 {a2} (${m2['total_cost']:.4f})")
else:
    report.append(f"3. **成本**: {a2} (${m2['total_cost']:.4f}) 低于 {a1} (${m1['total_cost']:.4f})")

report.append(f"")
report.append(f"### 改进建议")
report.append(f"")
report.append(f"1. 针对未通过任务，分析具体失败原因（是工具调用问题、理解偏差还是执行错误）")
report.append(f"2. 对比两者的步骤轨迹，分析决策路径差异")
report.append(f"3. 考虑增加 runs=3 采样，评估非确定性任务的稳定性")
report.append(f"4. 针对高难度任务（L4/L5），可能需要调整 max_steps 或超时设置")

report.append(f"")
report.append(f"---")
report.append(f"*报告由 monitor-batch-compare.sh 自动生成*")

# 写入报告
Path(REPORT_FILE).write_text('\n'.join(report), encoding='utf-8')
print(f"报告已生成: {REPORT_FILE}")
print(f"报告长度: {len(report)} 行")

conn.close()
PYEOF

echo ""
echo "[$(date '+%H:%M:%S')] 监控完成！"
echo "报告文件: ${REPORT_FILE}"
