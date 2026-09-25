"""多 run 采样统计（V2.0-Day3）。

LLM Agent 具有非确定性，单次 run 不能当结论。
对同一 (agent, task) 的多次 run 得分计算：best / mean / std / pass_rate。
- pass_rate：得分 > 0（即有通过判定）的 run 占比（即 pass@k 语义：k 次中至少一次成功）
- pass_all：全部 run 都通过的比例（即 pass^k 语义：连续 k 次每次都成功）
  参考 Anthropic《Demystifying evals for AI agents》：退款/改配置/发布类任务必须看 pass^k，
  用户不接受"多试几次总能成功一次"。
"""

from __future__ import annotations


def summarize_scores(scores: list[float]) -> dict:
    n = len(scores)
    if n == 0:
        return {"n": 0, "best": 0.0, "mean": 0.0, "std": 0.0, "pass_rate": 0.0,
                "pass_all": 0.0, "pass_at_k": 0.0}
    best = max(scores)
    mean = sum(scores) / n
    var = sum((s - mean) ** 2 for s in scores) / n
    std = var**0.5
    passed = sum(1 for s in scores if s > 0)
    pass_rate = passed / n
    # pass^k：连续 k 次（本批次全部 run）每次都成功
    pass_all = 1.0 if n > 0 and passed == n else 0.0
    return {
        "n": n,
        "best": round(best, 3),
        "mean": round(mean, 3),
        "std": round(std, 3),
        "pass_rate": round(pass_rate, 3),
        "pass_at_k": round(pass_rate, 3),   # pass@k = 至少一次成功（与 pass_rate 同义）
        "pass_all": round(pass_all, 3),      # pass^k = 连续 k 次全部成功
    }
