"""评测置信度评估模块。

基于 5 个维度加权计算每次评测结果的置信度分数（0-100），
帮助用户判断"这个评测结果有多可信"。

维度与权重：
1. 运行次数（25%）：runs 越多，置信度越高
2. 校验点类型（25%）：确定性校验 > 混合 > LLM Judge
3. 任务覆盖度（20%）：全量 > core包 > 单任务
4. 历史稳定性（15%）：多次运行得分标准差越小越稳定
5. 任务级别（15%）：L1/L2 基础任务 > L3/L4 复杂任务

置信度分级：
- 高（≥80）：结果可信，可用于决策
- 中（60-79）：结果有一定参考价值，建议补充验证
- 低（<60）：结果不可靠，需要增加 runs 或扩大任务集
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


# 权重配置
WEIGHTS = {
    "runs": 0.25,
    "verifier_type": 0.25,
    "task_coverage": 0.20,
    "stability": 0.15,
    "task_level": 0.15,
}


def calculate_run_confidence(
    run_data: dict[str, Any],
    all_runs_dir: Path | None = None,
    task_count_total: int = 36,
    core_task_ids: set[str] | None = None,
) -> dict[str, Any]:
    """计算单次运行的置信度。

    Args:
        run_data: run.json 的内容（dict）
        all_runs_dir: results/runs/ 目录，用于查询历史稳定性
        task_count_total: 任务总数，用于计算任务覆盖度
        core_task_ids: core 包任务 ID 集合

    Returns:
        {
            "score": 0-100,
            "level": "high" | "medium" | "low",
            "dimensions": {每个维度的得分和说明},
            "suggestions": [改进建议列表]
        }
    """
    if core_task_ids is None:
        core_task_ids = set()

    dimensions: dict[str, dict[str, Any]] = {}

    # 1. 运行次数（从 batch 或历史中推断）
    runs_score, runs_detail = _score_runs(run_data, all_runs_dir)
    dimensions["runs"] = {"score": runs_score, "detail": runs_detail}

    # 2. 校验点类型
    verifier_score, verifier_detail = _score_verifier_type(run_data)
    dimensions["verifier_type"] = {"score": verifier_score, "detail": verifier_detail}

    # 3. 任务覆盖度（单任务运行时为单任务）
    coverage_score, coverage_detail = _score_task_coverage(run_data, task_count_total, core_task_ids)
    dimensions["task_coverage"] = {"score": coverage_score, "detail": coverage_detail}

    # 4. 历史稳定性
    stability_score, stability_detail = _score_stability(run_data, all_runs_dir)
    dimensions["stability"] = {"score": stability_score, "detail": stability_detail}

    # 5. 任务级别
    level_score, level_detail = _score_task_level(run_data)
    dimensions["task_level"] = {"score": level_score, "detail": level_detail}

    # 加权总分
    total_score = sum(
        dimensions[k]["score"] * WEIGHTS[k]
        for k in WEIGHTS
    )
    total_score = round(total_score, 1)

    # 分级
    if total_score >= 80:
        level = "high"
    elif total_score >= 60:
        level = "medium"
    else:
        level = "low"

    # 改进建议
    suggestions = _generate_suggestions(dimensions, level)

    return {
        "score": total_score,
        "level": level,
        "dimensions": dimensions,
        "suggestions": suggestions,
    }


def calculate_batch_confidence(
    batch_runs: list[dict[str, Any]],
    task_count_total: int = 36,
    core_task_ids: set[str] | None = None,
) -> dict[str, Any]:
    """计算批次（多任务多Agent对比）的置信度。

    Args:
        batch_runs: 批次中所有 run 的 run.json 列表
        task_count_total: 任务总数
        core_task_ids: core 包任务 ID 集合

    Returns:
        同 calculate_run_confidence 的结构
    """
    if core_task_ids is None:
        core_task_ids = set()

    if not batch_runs:
        return {
            "score": 0,
            "level": "low",
            "dimensions": {},
            "suggestions": ["无运行数据"],
        }

    dimensions: dict[str, dict[str, Any]] = {}

    # 1. 运行次数（批次中的平均 runs）
    task_run_counts: dict[str, int] = {}
    for r in batch_runs:
        tid = r.get("task_id", "unknown")
        task_run_counts[tid] = task_run_counts.get(tid, 0) + 1
    avg_runs = sum(task_run_counts.values()) / len(task_run_counts) if task_run_counts else 1
    runs_score = min(100, avg_runs * 25)  # 1次=25, 2次=50, 3次=75, 4次+=100
    runs_score = round(runs_score, 1)
    dimensions["runs"] = {
        "score": runs_score,
        "detail": f"批次平均每任务 {avg_runs:.1f} 次运行",
    }

    # 2. 校验点类型（批次中确定性校验的比例）
    deterministic_count = 0
    llm_judge_count = 0
    for r in batch_runs:
        verifier = r.get("verifier", "deterministic")
        if verifier == "llm_judge":
            llm_judge_count += 1
        else:
            deterministic_count += 1
    total = deterministic_count + llm_judge_count
    if total == 0:
        verifier_score = 70.0
    else:
        det_ratio = deterministic_count / total
        verifier_score = 50 + det_ratio * 50  # 全LLM=50, 全确定=100
    verifier_score = round(verifier_score, 1)
    dimensions["verifier_type"] = {
        "score": verifier_score,
        "detail": f"确定性校验 {deterministic_count} 个，LLM Judge {llm_judge_count} 个",
    }

    # 3. 任务覆盖度
    unique_tasks = len(set(r.get("task_id") for r in batch_runs))
    if unique_tasks >= task_count_total * 0.8:
        coverage_score = 100.0
        coverage_detail = f"覆盖 {unique_tasks}/{task_count_total} 个任务（全量）"
    elif core_task_ids and all(tid in core_task_ids for tid in set(r.get("task_id") for r in batch_runs)):
        coverage_score = 70.0
        coverage_detail = f"覆盖 {unique_tasks} 个任务（core 包）"
    elif unique_tasks >= 5:
        coverage_score = 55.0
        coverage_detail = f"覆盖 {unique_tasks} 个任务（部分）"
    else:
        coverage_score = 40.0
        coverage_detail = f"仅覆盖 {unique_tasks} 个任务"
    dimensions["task_coverage"] = {
        "score": coverage_score,
        "detail": coverage_detail,
    }

    # 4. 历史稳定性（批次内同任务得分的标准差）
    task_scores: dict[str, list[float]] = {}
    for r in batch_runs:
        tid = r.get("task_id", "unknown")
        score = r.get("metrics", {}).get("score", 0)
        task_scores.setdefault(tid, []).append(score)
    stds = []
    for tid, scores in task_scores.items():
        if len(scores) >= 2:
            mean = sum(scores) / len(scores)
            variance = sum((s - mean) ** 2 for s in scores) / len(scores)
            stds.append(math.sqrt(variance))
    if stds:
        avg_std = sum(stds) / len(stds)
        # std=0 → 100分, std=0.5 → 50分, std>=1 → 0分
        stability_score = max(0, 100 - avg_std * 100)
    else:
        stability_score = 50.0  # 无重复运行，无法评估稳定性
    stability_score = round(stability_score, 1)
    dimensions["stability"] = {
        "score": stability_score,
        "detail": f"{'同任务多次运行，平均标准差 ' + f'{avg_std:.3f}' if stds else '无重复运行，无法评估稳定性'}",
    }

    # 5. 任务级别（批次中 L1/L2 的比例）
    level_counts = {"L1": 0, "L2": 0, "L3": 0, "L4": 0, "unknown": 0}
    for r in batch_runs:
        lvl = r.get("task_level", "unknown")
        level_counts[lvl] = level_counts.get(lvl, 0) + 1
    basic_count = level_counts.get("L1", 0) + level_counts.get("L2", 0)
    total_tasks = sum(level_counts.values())
    if total_tasks == 0:
        level_score = 70.0
    else:
        basic_ratio = basic_count / total_tasks
        level_score = 60 + basic_ratio * 40  # 全复杂=60, 全基础=100
    level_score = round(level_score, 1)
    dimensions["task_level"] = {
        "score": level_score,
        "detail": f"L1/L2 基础任务 {basic_count}/{total_tasks}",
    }

    # 加权总分
    total_score = sum(
        dimensions[k]["score"] * WEIGHTS[k]
        for k in WEIGHTS
    )
    total_score = round(total_score, 1)

    if total_score >= 80:
        level = "high"
    elif total_score >= 60:
        level = "medium"
    else:
        level = "low"

    suggestions = _generate_suggestions(dimensions, level)

    return {
        "score": total_score,
        "level": level,
        "dimensions": dimensions,
        "suggestions": suggestions,
    }


def _score_runs(run_data: dict, all_runs_dir: Path | None) -> tuple[float, str]:
    """运行次数得分。"""
    # 检查是否有 batch 信息
    task_id = run_data.get("task_id", "")
    agent_id = run_data.get("agent_id", "")

    # 统计同任务同 agent 的历史运行次数
    historical_runs = 1
    if all_runs_dir and all_runs_dir.exists():
        for run_dir in all_runs_dir.iterdir():
            run_json = run_dir / "run.json"
            if run_json.exists():
                try:
                    data = json.loads(run_json.read_text())
                    if data.get("task_id") == task_id and data.get("agent_id") == agent_id:
                        historical_runs += 1
                except (json.JSONDecodeError, OSError):
                    continue

    if historical_runs >= 5:
        score = 100.0
        detail = f"历史累计 {historical_runs} 次运行，样本充足"
    elif historical_runs >= 3:
        score = 80.0
        detail = f"历史累计 {historical_runs} 次运行，样本较充足"
    elif historical_runs >= 2:
        score = 60.0
        detail = f"历史累计 {historical_runs} 次运行，样本一般"
    else:
        score = 40.0
        detail = "首次运行，样本不足"

    return score, detail


def _score_verifier_type(run_data: dict) -> tuple[float, str]:
    """校验点类型得分。"""
    verifier = run_data.get("verifier", "deterministic")
    verdicts = run_data.get("verdicts", [])

    # 统计 checkpoint 类型
    checkpoint_types = set()
    for v in verdicts:
        cp = v.get("checkpoint", {})
        ctype = cp.get("type", "")
        if ctype:
            checkpoint_types.add(ctype)

    has_llm_judge = verifier == "llm_judge"
    has_deterministic = any(
        t in checkpoint_types
        for t in ["file_exists", "content_contains", "content_not_contains", "cmd_exit_zero"]
    )

    if has_llm_judge and not has_deterministic:
        score = 50.0
        detail = "全部使用 LLM Judge，存在主观判分风险"
    elif has_llm_judge and has_deterministic:
        score = 70.0
        detail = "混合校验（确定性 + LLM Judge）"
    else:
        score = 100.0
        detail = "全部使用确定性校验，结果可复现"

    return score, detail


def _score_task_coverage(
    run_data: dict,
    task_count_total: int,
    core_task_ids: set[str],
) -> tuple[float, str]:
    """任务覆盖度得分（单任务运行时）。"""
    task_id = run_data.get("task_id", "")

    if task_id in core_task_ids:
        score = 55.0
        detail = "单任务运行（core 包任务），建议跑完整 core 包"
    else:
        score = 40.0
        detail = "单任务运行，覆盖度有限，建议扩大任务集"

    return score, detail


def _score_stability(run_data: dict, all_runs_dir: Path | None) -> tuple[float, str]:
    """历史稳定性得分。"""
    task_id = run_data.get("task_id", "")
    agent_id = run_data.get("agent_id", "")
    current_score = run_data.get("metrics", {}).get("score", 0)

    if not all_runs_dir or not all_runs_dir.exists():
        return 50.0, "无历史数据，无法评估稳定性"

    # 收集同任务同 agent 的历史得分
    scores = []
    for run_dir in all_runs_dir.iterdir():
        run_json = run_dir / "run.json"
        if run_json.exists():
            try:
                data = json.loads(run_json.read_text())
                if data.get("task_id") == task_id and data.get("agent_id") == agent_id:
                    s = data.get("metrics", {}).get("score", 0)
                    scores.append(s)
            except (json.JSONDecodeError, OSError):
                continue

    if len(scores) < 2:
        return 50.0, "历史运行不足 2 次，无法评估稳定性"

    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = math.sqrt(variance)

    if std == 0:
        score = 100.0
        detail = f"历史 {len(scores)} 次运行得分完全一致，极其稳定"
    elif std < 0.1:
        score = 90.0
        detail = f"历史 {len(scores)} 次运行标准差 {std:.3f}，非常稳定"
    elif std < 0.3:
        score = 70.0
        detail = f"历史 {len(scores)} 次运行标准差 {std:.3f}，较稳定"
    elif std < 0.5:
        score = 50.0
        detail = f"历史 {len(scores)} 次运行标准差 {std:.3f}，有一定波动"
    else:
        score = 30.0
        detail = f"历史 {len(scores)} 次运行标准差 {std:.3f}，波动较大"

    return score, detail


def _score_task_level(run_data: dict) -> tuple[float, str]:
    """任务级别得分。"""
    level = run_data.get("task_level", "L2")

    if level == "L1":
        score = 100.0
        detail = "L1 基础任务，判定简单明确"
    elif level == "L2":
        score = 85.0
        detail = "L2 中等任务，判定较明确"
    elif level == "L3":
        score = 65.0
        detail = "L3 复杂任务，判定有一定难度"
    elif level == "L4":
        score = 50.0
        detail = "L4 高难任务，判定难度大"
    else:
        score = 70.0
        detail = f"{level} 任务"

    return score, detail


def _generate_suggestions(dimensions: dict, level: str) -> list[str]:
    """根据各维度得分生成改进建议。"""
    suggestions = []

    if dimensions.get("runs", {}).get("score", 100) < 70:
        suggestions.append("增加运行次数（--runs 3 或更多），提高统计显著性")

    if dimensions.get("verifier_type", {}).get("score", 100) < 70:
        suggestions.append("考虑增加确定性校验点（file_exists/content_contains），减少对 LLM Judge 的依赖")

    if dimensions.get("task_coverage", {}).get("score", 100) < 60:
        suggestions.append("扩大任务覆盖范围，建议至少跑 core 包或全量任务")

    if dimensions.get("stability", {}).get("score", 100) < 60:
        suggestions.append("历史得分波动较大，建议检查任务是否存在非确定性因素")

    if dimensions.get("task_level", {}).get("score", 100) < 70:
        suggestions.append("复杂任务（L3/L4）建议配合 L1/L2 基础任务一起跑，综合评估能力")

    if level == "low":
        suggestions.insert(0, "⚠️ 当前置信度较低，不建议直接用于决策")
    elif level == "medium":
        suggestions.insert(0, "当前置信度中等，建议补充验证后再用于重要决策")

    if not suggestions:
        suggestions.append("置信度良好，结果可用于决策")

    return suggestions


def confidence_level_label(level: str) -> str:
    """置信度级别中文标签。"""
    return {
        "high": "高置信",
        "medium": "中置信",
        "low": "低置信",
    }.get(level, "未知")


def confidence_level_color(level: str) -> str:
    """置信度级别颜色（用于前端展示）。"""
    return {
        "high": "#22c55e",  # 绿色
        "medium": "#f59e0b",  # 橙色
        "low": "#ef4444",  # 红色
    }.get(level, "#6b7280")
