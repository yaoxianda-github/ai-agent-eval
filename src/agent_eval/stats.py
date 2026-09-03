"""多 run 采样统计（V2.0-Day3）。

LLM Agent 具有非确定性，单次 run 不能当结论。
对同一 (agent, task) 的多次 run 得分计算：best / mean / std / pass_rate。
- pass_rate：得分 > 0（即有通过判定）的 run 占比
"""

from __future__ import annotations


def summarize_scores(scores: list[float]) -> dict:
    n = len(scores)
    if n == 0:
        return {"n": 0, "best": 0.0, "mean": 0.0, "std": 0.0, "pass_rate": 0.0}
    best = max(scores)
    mean = sum(scores) / n
    var = sum((s - mean) ** 2 for s in scores) / n
    std = var**0.5
    pass_rate = sum(1 for s in scores if s > 0) / n
    return {
        "n": n,
        "best": round(best, 3),
        "mean": round(mean, 3),
        "std": round(std, 3),
        "pass_rate": round(pass_rate, 3),
    }
