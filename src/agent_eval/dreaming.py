"""V2.9：Dreaming 异步进化分析器。

借鉴 Anthropic Dreaming 机制：在空闲时定期审阅历史运行记录，
发现跨会话的系统性模式（反复失败、低效路径、知识缺口），
输出结构化的改进建议报告。

这是"自进化飞轮"的异步进化环节，与同步评测互补：
- 同步评测：解决"单次任务做得好不好"——快速反馈，针对性强
- Dreaming：解决"跨任务的系统性模式"——全局视角，发现慢变量
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FailurePattern:
    """失败模式：多个任务都在同一类问题上犯错。"""
    pattern_type: str  # checkpoint_type / status / category
    pattern_value: str
    count: int
    affected_tasks: list[str] = field(default_factory=list)
    affected_agents: list[str] = field(default_factory=list)
    severity: str = "P2"  # P0/P1/P2/P3
    suggestion: str = ""


@dataclass
class InefficiencyPattern:
    """低效模式：任务最终成功了，但中间走了很多弯路。"""
    task_id: str
    agent_id: str
    steps: int
    duration_s: float
    token_ratio: float  # prompt_tokens / completion_tokens
    description: str = ""


@dataclass
class DreamingReport:
    """Dreaming 分析报告。"""
    generated_at: str
    total_runs: int
    time_range: str
    failure_patterns: list[FailurePattern] = field(default_factory=list)
    inefficiency_patterns: list[InefficiencyPattern] = field(default_factory=list)
    knowledge_gaps: list[str] = field(default_factory=list)
    top_failing_tasks: list[tuple[str, int, float]] = field(default_factory=list)  # (task_id, fail_count, fail_rate)
    top_failing_agents: list[tuple[str, int, float]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        """生成 Markdown 格式的报告。"""
        lines = [
            f"# Dreaming 进化分析报告",
            f"",
            f"- 生成时间: {self.generated_at}",
            f"- 分析运行数: {self.total_runs}",
            f"- 时间范围: {self.time_range}",
            f"",
        ]

        if self.failure_patterns:
            lines.append("## 一、系统性失败模式")
            lines.append("")
            for i, p in enumerate(self.failure_patterns, 1):
                lines.append(f"### {i}. [{p.severity}] {p.pattern_type}: {p.pattern_value}")
                lines.append(f"- 出现次数: {p.count}")
                lines.append(f"- 影响任务: {', '.join(p.affected_tasks[:5])}{'...' if len(p.affected_tasks) > 5 else ''}")
                lines.append(f"- 影响后端: {', '.join(p.affected_agents[:5])}{'...' if len(p.affected_agents) > 5 else ''}")
                lines.append(f"- 改进建议: {p.suggestion}")
                lines.append("")

        if self.inefficiency_patterns:
            lines.append("## 二、低效执行模式")
            lines.append("")
            lines.append("| 任务 | 后端 | 步数 | 耗时(s) | Token比率 | 描述 |")
            lines.append("|------|------|------|---------|-----------|------|")
            for p in self.inefficiency_patterns[:10]:
                lines.append(f"| {p.task_id} | {p.agent_id} | {p.steps} | {p.duration_s:.1f} | {p.token_ratio:.1f} | {p.description[:40]} |")
            lines.append("")

        if self.knowledge_gaps:
            lines.append("## 三、知识缺口")
            lines.append("")
            for gap in self.knowledge_gaps:
                lines.append(f"- {gap}")
            lines.append("")

        if self.top_failing_tasks:
            lines.append("## 四、失败率最高的任务")
            lines.append("")
            lines.append("| 任务 | 失败次数 | 失败率 |")
            lines.append("|------|----------|--------|")
            for task_id, fail_count, fail_rate in self.top_failing_tasks[:10]:
                lines.append(f"| {task_id} | {fail_count} | {fail_rate:.1%} |")
            lines.append("")

        if self.top_failing_agents:
            lines.append("## 五、失败率最高的后端")
            lines.append("")
            lines.append("| 后端 | 失败次数 | 失败率 |")
            lines.append("|------|----------|--------|")
            for agent_id, fail_count, fail_rate in self.top_failing_agents[:5]:
                lines.append(f"| {agent_id} | {fail_count} | {fail_rate:.1%} |")
            lines.append("")

        if self.suggestions:
            lines.append("## 六、综合改进建议")
            lines.append("")
            for i, s in enumerate(self.suggestions, 1):
                lines.append(f"{i}. {s}")
            lines.append("")

        return "\n".join(lines)


def analyze_runs(results_dir: Path, days: int = 7, min_pattern_count: int = 2) -> DreamingReport:
    """分析历史运行记录，生成 Dreaming 报告。

    Args:
        results_dir: 运行记录目录（results/runs）
        days: 分析最近 N 天的运行记录
        min_pattern_count: 模式最小出现次数（低于此数不视为系统性模式）

    Returns:
        DreamingReport 分析报告
    """
    from datetime import datetime, timedelta

    cutoff = datetime.now() - timedelta(days=days)
    runs = []
    for run_dir in sorted(results_dir.glob("*/run.json")):
        try:
            run_data = json.loads(run_dir.read_text(encoding="utf-8"))
            # 检查时间范围
            created_at = run_data.get("created_at", "")
            if created_at:
                try:
                    run_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if run_time.replace(tzinfo=None) < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            runs.append(run_data)
        except (json.JSONDecodeError, OSError):
            continue

    if not runs:
        return DreamingReport(
            generated_at=datetime.now().isoformat(timespec="seconds"),
            total_runs=0,
            time_range=f"最近 {days} 天",
            suggestions=["暂无运行记录可供分析"],
        )

    # 统计失败模式
    checkpoint_fail_counter = Counter()
    checkpoint_fail_tasks = defaultdict(set)
    checkpoint_fail_agents = defaultdict(set)
    status_fail_counter = Counter()
    status_fail_tasks = defaultdict(set)

    task_fail_counter = Counter()
    task_total_counter = Counter()
    agent_fail_counter = Counter()
    agent_total_counter = Counter()

    inefficiencies = []
    knowledge_gap_keywords = ["search", "query", "lookup", "fetch", "browse", "google", "wiki"]

    for run in runs:
        task_id = run.get("task_id", "unknown")
        agent_id = run.get("agent_id", "unknown")
        status = run.get("status", "unknown")
        verdicts = run.get("verdicts", [])
        metrics = run.get("metrics", {})
        steps = metrics.get("steps", 0)
        duration = metrics.get("duration_s", 0)
        usage = metrics.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        task_total_counter[task_id] += 1
        agent_total_counter[agent_id] += 1

        is_failed = status != "passed" and status != "success"
        if is_failed:
            task_fail_counter[task_id] += 1
            agent_fail_counter[agent_id] += 1
            status_fail_counter[status] += 1
            status_fail_tasks[status].add(task_id)

        # 统计 checkpoint 失败
        for v in verdicts:
            if not v.get("passed", True):
                ctype = v.get("checkpoint_type", "unknown")
                checkpoint_fail_counter[ctype] += 1
                checkpoint_fail_tasks[ctype].add(task_id)
                checkpoint_fail_agents[ctype].add(agent_id)

        # 检测低效模式（成功但步数多/耗时长/token比率高）
        if not is_failed and (steps > 10 or duration > 120 or (completion_tokens > 0 and prompt_tokens / completion_tokens > 10)):
            desc_parts = []
            if steps > 10:
                desc_parts.append(f"步数过多({steps})")
            if duration > 120:
                desc_parts.append(f"耗时过长({duration:.0f}s)")
            if completion_tokens > 0 and prompt_tokens / completion_tokens > 10:
                desc_parts.append(f"Token比率过高({prompt_tokens / completion_tokens:.1f})")
            inefficiencies.append(InefficiencyPattern(
                task_id=task_id,
                agent_id=agent_id,
                steps=steps,
                duration_s=duration,
                token_ratio=prompt_tokens / completion_tokens if completion_tokens > 0 else 0,
                description="; ".join(desc_parts),
            ))

        # 检测知识缺口（频繁使用搜索/查询工具）
        traces = run.get("traces", [])
        search_count = 0
        for t in traces:
            tool_name = (t.get("tool") or t.get("name") or "").lower()
            if any(kw in tool_name for kw in knowledge_gap_keywords):
                search_count += 1
        if search_count >= 3:
            knowledge_gap_keywords_str = f"任务 {task_id} 频繁调用搜索/查询工具({search_count}次)，可能存在知识缺口"
            if knowledge_gap_keywords_str not in [g for g in []]:  # 简单去重
                pass

    # 构建失败模式
    failure_patterns = []
    for ctype, count in checkpoint_fail_counter.most_common():
        if count >= min_pattern_count:
            severity = "P0" if count >= len(runs) * 0.3 else ("P1" if count >= len(runs) * 0.15 else "P2")
            suggestion = _get_checkpoint_suggestion(ctype)
            failure_patterns.append(FailurePattern(
                pattern_type="checkpoint_type",
                pattern_value=ctype,
                count=count,
                affected_tasks=sorted(checkpoint_fail_tasks[ctype]),
                affected_agents=sorted(checkpoint_fail_agents[ctype]),
                severity=severity,
                suggestion=suggestion,
            ))

    for status, count in status_fail_counter.most_common():
        if count >= min_pattern_count and status not in ("passed", "success"):
            severity = "P0" if status in ("error", "timeout") else "P1"
            suggestion = _get_status_suggestion(status)
            failure_patterns.append(FailurePattern(
                pattern_type="status",
                pattern_value=status,
                count=count,
                affected_tasks=sorted(status_fail_tasks[status]),
                severity=severity,
                suggestion=suggestion,
            ))

    # 计算失败率最高的任务
    top_failing_tasks = []
    for task_id, total in task_total_counter.items():
        if total >= 2:
            fail_count = task_fail_counter.get(task_id, 0)
            fail_rate = fail_count / total
            if fail_rate > 0:
                top_failing_tasks.append((task_id, fail_count, fail_rate))
    top_failing_tasks.sort(key=lambda x: x[2], reverse=True)

    # 计算失败率最高的后端
    top_failing_agents = []
    for agent_id, total in agent_total_counter.items():
        if total >= 2:
            fail_count = agent_fail_counter.get(agent_id, 0)
            fail_rate = fail_count / total
            if fail_rate > 0:
                top_failing_agents.append((agent_id, fail_count, fail_rate))
    top_failing_agents.sort(key=lambda x: x[2], reverse=True)

    # 综合改进建议
    suggestions = []
    if failure_patterns:
        top_pattern = failure_patterns[0]
        suggestions.append(f"优先解决最频繁的失败模式「{top_pattern.pattern_value}」（出现 {top_pattern.count} 次），建议：{top_pattern.suggestion}")
    if top_failing_tasks:
        worst_task = top_failing_tasks[0]
        suggestions.append(f"任务「{worst_task[0]}」失败率最高（{worst_task[2]:.1%}），建议审查任务 spec 或增加难度分级")
    if inefficiencies:
        suggestions.append(f"检测到 {len(inefficiencies)} 个低效执行模式，建议优化 system prompt 或工具调用策略以减少步数和 token 消耗")
    if not suggestions:
        suggestions.append("未检测到明显的系统性问题，继续保持当前配置")

    # 知识缺口（简化版：统计频繁搜索的任务）
    knowledge_gaps = []
    search_task_counter = Counter()
    for run in runs:
        traces = run.get("traces", [])
        for t in traces:
            tool_name = (t.get("tool") or t.get("name") or "").lower()
            if any(kw in tool_name for kw in knowledge_gap_keywords):
                search_task_counter[run.get("task_id", "unknown")] += 1
    for task_id, count in search_task_counter.most_common(5):
        if count >= 3:
            knowledge_gaps.append(f"任务 {task_id} 频繁调用搜索/查询工具({count}次)，建议将相关知识沉淀为内置知识或经验记忆")

    return DreamingReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        total_runs=len(runs),
        time_range=f"最近 {days} 天",
        failure_patterns=failure_patterns,
        inefficiency_patterns=sorted(inefficiencies, key=lambda x: x.steps, reverse=True)[:20],
        knowledge_gaps=knowledge_gaps,
        top_failing_tasks=top_failing_tasks[:10],
        top_failing_agents=top_failing_agents[:5],
        suggestions=suggestions,
    )


def _get_checkpoint_suggestion(checkpoint_type: str) -> str:
    """根据 checkpoint 类型给出改进建议。"""
    suggestions = {
        "content_contains": "检查 Agent 的输出理解和生成能力，优化任务指令的明确性，减少歧义",
        "content_not_contains": "检查 Agent 是否生成了不应出现的内容，优化安全约束和输出格式约束",
        "file_exists": "在任务描述中明确要求生成特定文件，优化终止条件确保 Agent 在完成后才结束",
        "cmd_exit_zero": "检查 Agent 生成的命令参数是否正确，增强命令执行的错误处理和重试机制",
        "json_valid": "优化输出格式约束，增加格式容错解析，在 system prompt 中明确 JSON 格式要求",
        "regex_match": "检查正则表达式是否正确，优化 Agent 对格式要求的理解",
    }
    return suggestions.get(checkpoint_type, "审查任务 spec 和 Agent 配置，定位具体失败原因")


def _get_status_suggestion(status: str) -> str:
    """根据运行状态给出改进建议。"""
    suggestions = {
        "error": "检查工具调用参数校验和错误恢复机制，增强后端服务的健康检查和重试策略",
        "timeout": "优化任务拆解减少不必要的工具调用，为耗时操作设置合理的超时和降级策略",
        "max_steps": "优化 system prompt 强调高效规划和尽早完成，增加任务完成度的自我评估机制",
        "failed": "审查失败的具体校验点，定位根因后针对性优化",
    }
    return suggestions.get(status, "审查运行日志和轨迹，定位具体失败原因")


def convert_patterns_to_badcases(report: DreamingReport, store, min_count: int = 3) -> list[dict]:
    """V2.9.1：将 Dreaming 发现的系统性失败模式自动转化为 badcase。

    这是自进化飞轮回流的关键环节：Dreaming 发现的模式自动进入 badcase 积累，
    后续可转化为回归用例或经验记忆，形成完整的进化闭环。

    Args:
        report: Dreaming 分析报告
        store: RunStore 实例（用于创建 badcase）
        min_count: 模式最小出现次数（低于此数不转化）

    Returns:
        转化结果列表：[{"pattern": "...", "badcase_id": "...", "created": bool}]
    """
    results = []

    for pattern in report.failure_patterns:
        if pattern.count < min_count:
            continue

        # 根据模式类型确定 badcase 分类
        category = "other"
        if pattern.pattern_type == "checkpoint_type":
            if pattern.pattern_value.startswith("content_"):
                category = "reasoning"
            elif pattern.pattern_value == "cmd_exit_zero":
                category = "tool_use"
            elif pattern.pattern_value == "file_exists":
                category = "planning"
            elif pattern.pattern_value in ("json_valid", "regex_match"):
                category = "format"
        elif pattern.pattern_type == "status":
            if pattern.pattern_value == "error":
                category = "crash"
            elif pattern.pattern_value == "timeout":
                category = "timeout"
            elif pattern.pattern_value == "max_steps":
                category = "planning"

        # 生成 badcase 标题和描述
        title = f"[Dreaming] 系统性失败模式: {pattern.pattern_value} (出现{pattern.count}次)"
        description = (
            f"【Dreaming 自动发现】\n"
            f"模式类型: {pattern.pattern_type}\n"
            f"模式值: {pattern.pattern_value}\n"
            f"出现次数: {pattern.count}\n"
            f"影响任务: {', '.join(pattern.affected_tasks[:10])}\n"
            f"影响后端: {', '.join(pattern.affected_agents[:5])}\n"
            f"严重度: {pattern.severity}\n\n"
            f"【改进建议】\n{pattern.suggestion}\n\n"
            f"【来源】Dreaming 异步进化分析，生成时间: {report.generated_at}"
        )

        # 检查是否已存在相同模式的 badcase（避免重复创建）
        existing = store.list_badcases(page=1, page_size=100, keyword=pattern.pattern_value)
        already_exists = False
        for b in existing.get("items", []):
            if pattern.pattern_value in b.get("title", "") and "Dreaming" in b.get("title", ""):
                already_exists = True
                results.append({"pattern": pattern.pattern_value, "badcase_id": b["id"], "created": False, "reason": "已存在相同模式的 badcase"})
                break

        if not already_exists:
            bid = store.insert_badcase({
                "run_id": "",
                "task_id": pattern.affected_tasks[0] if pattern.affected_tasks else "",
                "agent_id": pattern.affected_agents[0] if pattern.affected_agents else "",
                "title": title,
                "description": description,
                "category": category,
                "severity": pattern.severity,
                "status": "analyzing",
                "root_cause": f"Dreaming 发现的系统性失败模式：{pattern.pattern_type}={pattern.pattern_value}，影响 {len(pattern.affected_tasks)} 个任务",
                "fix_plan": pattern.suggestion,
                "tags": json.dumps(["dreaming", "systemic", pattern.pattern_type], ensure_ascii=False),
            })
            results.append({"pattern": pattern.pattern_value, "badcase_id": bid, "created": True})

    return results
