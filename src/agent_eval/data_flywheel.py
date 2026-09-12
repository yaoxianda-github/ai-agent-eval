"""agent-eval 数据飞轮（V3.4 P4）——采样→标注→回流闭环。

参考：Agent评测体系全生命周期6步闭环——第6步：数据飞轮
线上采样 → 标注 → 加入数据集 → 评测 → 优化 → 上线

核心链路：
1. 每日采样（DailySampler）→ 失败运行自动创建待标注 badcase
2. badcase 标注（人工/LLM辅助）→ 根因分析 + 修复方案
3. badcase 回流 → 转化为回归任务 spec，加入评测集
4. 经验记忆沉淀 → 成功/失败模式提取为 memory，注入后续运行
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class FlywheelStats:
    """数据飞轮统计。"""
    # 采样阶段
    sampled_today: int = 0
    sampled_total: int = 0
    # 标注阶段
    pending_annotation: int = 0
    annotated: int = 0
    annotation_rate: float = 0.0  # 已标注/待标注+已标注
    # 回流阶段
    converted_to_task: int = 0
    conversion_rate: float = 0.0  # 已回流/已标注
    # 记忆沉淀
    memories_total: int = 0
    memories_active: int = 0
    # 整体效率
    flywheel_efficiency: float = 0.0  # 已回流/总采样

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_today": self.sampled_today,
            "sampled_total": self.sampled_total,
            "pending_annotation": self.pending_annotation,
            "annotated": self.annotated,
            "annotation_rate": round(self.annotation_rate, 3),
            "converted_to_task": self.converted_to_task,
            "conversion_rate": round(self.conversion_rate, 3),
            "memories_total": self.memories_total,
            "memories_active": self.memories_active,
            "flywheel_efficiency": round(self.flywheel_efficiency, 3),
        }


def analyze_run_failure(run_data: dict) -> dict[str, Any]:
    """分析运行失败原因，生成 badcase 标注建议。

    Args:
        run_data: run.json 的内容

    Returns:
        包含 category/severity/root_cause/fix_plan/tags 的标注建议
    """
    status = run_data.get("status", "")
    metrics = run_data.get("metrics", {}) or {}
    score = float(metrics.get("score", 0.0))
    verdicts = run_data.get("verdicts", []) or []
    steps = run_data.get("steps", []) or []
    traces = run_data.get("traces", []) or []
    error = run_data.get("error", "")

    # 失败分类
    category = "other"
    severity = "P2"
    root_cause = ""
    fix_plan = ""
    tags: list[str] = []

    if status == "timeout":
        category = "timeout"
        severity = "P1"
        root_cause = f"执行超时，共{len(steps)}步，可能陷入死循环或工具调用过慢"
        fix_plan = "1. 检查是否有死循环（重复调用同一工具）\n2. 优化工具调用效率\n3. 增加步数上限或超时阈值"
        tags = ["timeout", "performance"]
    elif status == "error":
        category = "execution_error"
        severity = "P0" if "API" in error or "key" in error.lower() else "P1"
        root_cause = f"执行报错: {error[:200]}"
        fix_plan = "1. 检查错误日志定位根因\n2. 修复后端或工具问题\n3. 增加错误处理和重试机制"
        tags = ["error", "bug"]
    elif score < 0.6:
        # 任务失败（校验点未通过）
        failed_checkpoints = [v for v in verdicts if not v.get("passed", True)]
        if failed_checkpoints:
            first_failed = failed_checkpoints[0]
            category = "task_failure"
            severity = "P1" if len(failed_checkpoints) > len(verdicts) / 2 else "P2"
            root_cause = f"{len(failed_checkpoints)}/{len(verdicts)} 个校验点未通过。首个失败: {first_failed.get('name', '')} - {first_failed.get('detail', '')[:150]}"
            fix_plan = "1. 分析失败校验点的预期与实际差异\n2. 优化 agent 的任务执行策略\n3. 调整 prompt 或工具配置"
            tags = ["task_failure", "regression_candidate"]
        else:
            category = "low_score"
            severity = "P3"
            root_cause = f"得分较低({score:.2f})，但无明确失败校验点"
            fix_plan = "1. 人工复核执行轨迹\n2. 评估评分标准是否合理\n3. 优化执行策略"
            tags = ["low_score"]
    else:
        # 部分通过但有改进空间
        category = "improvement"
        severity = "P3"
        root_cause = f"任务通过(score={score:.2f})，但有{len([v for v in verdicts if not v.get('passed', True)])}个校验点未完全通过"
        fix_plan = "1. 分析未完全通过的校验点\n2. 优化执行细节提升稳定性"
        tags = ["improvement"]

    # 步数超限检测
    if len(steps) > 20:
        tags.append("step_overflow")
        if severity == "P3":
            severity = "P2"

    # 工具调用分析
    tool_calls = [s for s in steps if s.get("action")]
    if tool_calls:
        tool_names = [s.get("action", "") for s in tool_calls]
        # 检测重复调用（可能死循环）
        from collections import Counter
        tool_counts = Counter(tool_names)
        most_common = tool_counts.most_common(1)
        if most_common and most_common[0][1] > 5:
            tags.append("potential_loop")
            root_cause += f"。注意: 工具'{most_common[0][0]}'被调用{most_common[0][1]}次，可能存在重复调用"

    return {
        "category": category,
        "severity": severity,
        "root_cause": root_cause,
        "fix_plan": fix_plan,
        "tags": tags,
        "failed_checkpoints": len([v for v in verdicts if not v.get("passed", True)]),
        "total_checkpoints": len(verdicts),
        "step_count": len(steps),
        "score": score,
    }


def should_auto_create_badcase(run_data: dict) -> bool:
    """判断是否应该自动创建 badcase（失败的运行）。"""
    status = run_data.get("status", "")
    metrics = run_data.get("metrics", {}) or {}
    score = float(metrics.get("score", 1.0))

    # 超时/报错 自动创建
    if status in ("timeout", "error"):
        return True
    # 得分低于0.6 自动创建
    if score < 0.6:
        return True
    return False


def compute_flywheel_stats(store, results_dir: Path) -> FlywheelStats:
    """计算数据飞轮统计。

    Args:
        store: RunStore 实例
        results_dir: 结果目录路径

    Returns:
        FlywheelStats 统计数据
    """
    stats = FlywheelStats()

    # badcase 统计
    try:
        bc_stats = store.badcase_stats()
        stats.pending_annotation = bc_stats.get("pending", 0)
        stats.annotated = bc_stats.get("analyzed", 0) + bc_stats.get("resolved", 0)
        total_bc = stats.pending_annotation + stats.annotated + bc_stats.get("other", 0)
        stats.converted_to_task = bc_stats.get("converted", 0)
        if total_bc > 0:
            stats.annotation_rate = stats.annotated / total_bc
        if stats.annotated > 0:
            stats.conversion_rate = stats.converted_to_task / stats.annotated
    except Exception:
        pass

    # 采样统计（从 daily_samples.json 读取）
    try:
        samples_path = results_dir.parent / "daily_samples.json"
        if samples_path.exists():
            data = json.loads(samples_path.read_text(encoding="utf-8"))
            stats.sampled_today = data.get("count", 0)
            # 总采样数需要历史累计，这里用 today 近似
            stats.sampled_total = stats.sampled_today
    except Exception:
        pass

    # 记忆统计
    try:
        memories, total = store.list_memories(limit=1)
        stats.memories_total = total
        stats.memories_active = sum(1 for m in memories if m.get("status") == "active")
        # 实际统计所有 active
        all_mem, _ = store.list_memories(limit=1000)
        stats.memories_active = sum(1 for m in all_mem if m.get("status") == "active")
    except Exception:
        pass

    # 整体效率
    if stats.sampled_total > 0:
        stats.flywheel_efficiency = stats.converted_to_task / stats.sampled_total

    return stats


def generate_flywheel_report(stats: FlywheelStats) -> list[str]:
    """生成数据飞轮健康度报告。"""
    report = []

    # 采样阶段
    report.append(f"📊 采样: 今日{stats.sampled_today}条，累计{stats.sampled_total}条")

    # 标注阶段
    if stats.annotation_rate < 0.5:
        report.append(f"⚠️ 标注率偏低: {stats.annotation_rate:.0%}（待标注{stats.pending_annotation}条），建议加快标注")
    else:
        report.append(f"✅ 标注率良好: {stats.annotation_rate:.0%}（已标注{stats.annotated}条）")

    # 回流阶段
    if stats.conversion_rate < 0.3:
        report.append(f"⚠️ 回流率偏低: {stats.conversion_rate:.0%}（已回流{stats.converted_to_task}条），建议将已标注badcase转化为回归任务")
    else:
        report.append(f"✅ 回流率良好: {stats.conversion_rate:.0%}（已回流{stats.converted_to_task}条）")

    # 记忆沉淀
    report.append(f"🧠 经验记忆: 共{stats.memories_total}条，活跃{stats.memories_active}条")

    # 整体效率
    if stats.flywheel_efficiency < 0.1:
        report.append(f"⚠️ 飞轮效率偏低: {stats.flywheel_efficiency:.0%}，数据闭环有待加强")
    elif stats.flywheel_efficiency < 0.3:
        report.append(f"📈 飞轮效率中等: {stats.flywheel_efficiency:.0%}，持续优化中")
    else:
        report.append(f"🚀 飞轮效率良好: {stats.flywheel_efficiency:.0%}，数据闭环运转顺畅")

    return report
