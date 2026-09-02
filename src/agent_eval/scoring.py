"""评分器（Day 3）：由 verdicts 计算任务得分。

规则（MVP 版）：
- 任务得分 = 任务权重 × 校验点通过率
- 通过率 = 通过的校验点数 / 校验点总数
后续可扩展：hard-required 校验点、按级别加权、成本/时长惩罚项。
"""

from __future__ import annotations


def score_task(task, verdicts: list[dict]) -> dict:
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.get("passed"))
    rate = passed / total if total else 0.0
    return {
        "task_id": task.id,
        "weight": task.weight,
        "score": round(task.weight * rate, 3),
        "passed": passed,
        "total": total,
        "pass_rate": round(rate, 3),
    }
