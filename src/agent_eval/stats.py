"""多 run 采样统计（V2.0-Day3）。

LLM Agent 具有非确定性，单次 run 不能当结论。
对同一 (agent, task) 的多次 run 得分计算：best / mean / std / pass_rate。
- pass_rate：得分 > 0（即有通过判定）的 run 占比（即 pass@k 语义：k 次中至少一次成功）
- pass_all：全部 run 都通过的比例（即 pass^k 语义：连续 k 次每次都成功）
  参考 Anthropic《Demystifying evals for AI agents》：退款/改配置/发布类任务必须看 pass^k，
  用户不接受"多试几次总能成功一次"。

V5.1 P0（评测标准可靠性）：unknown（abstain）独立统计
参考《Agent评测：评测体系、工程实践与持续演进》：Rubric 判断应收敛为 1/0/unknown，
unknown 表示证据不足、规则不清或不适用，必须单独统计，不能按通过或失败处理。
- 传入 abstain_flags 时，pass_rate 的分母排除 unknown 样本（避免证据不足被计为失败）
- 同时输出 unknown_count / unknown_rate，供门禁与报告独立呈现
"""

from __future__ import annotations


def run_abstained(run_data: dict) -> bool:
    """判断一次运行是否为 unknown（abstain 无法判断）。

    verdicts 中任一 verdict 标记 abstain=True，则该 run 判定为 unknown：
    证据不足、规则不清或不适用，不能按通过或失败处理。
    """
    verdicts = run_data.get("verdicts") or []
    return any(bool(v.get("abstain")) for v in verdicts)


def summarize_scores(
    scores: list[float],
    abstain_flags: list[bool] | None = None,
) -> dict:
    """汇总多次 run 得分。

    Args:
        scores: 各 run 的得分（0-1）
        abstain_flags: 与 scores 对齐的"无法判断(abstain)"标记列表，
            为 None 时按无 unknown 处理（保持旧行为）。

    Returns:
        {
            "n", "best", "mean", "std", "pass_rate", "pass_at_k", "pass_all",
            "unknown_count", "unknown_rate"   # V5.1 P0：unknown 独立统计
        }
    """
    n = len(scores)
    if n == 0:
        return {"n": 0, "best": 0.0, "mean": 0.0, "std": 0.0, "pass_rate": 0.0,
                "pass_all": 0.0, "pass_at_k": 0.0,
                "unknown_count": 0, "unknown_rate": 0.0}

    # V5.1 P0：unknown（无法判断）样本独立统计，不并入失败
    unknown_count = 0
    if abstain_flags:
        unknown_count = sum(1 for f in abstain_flags[:n] if f)
    evaluated_n = n - unknown_count  # 参与通过率计算的有效样本数

    best = max(scores) if scores else 0.0
    mean = sum(scores) / n
    var = sum((s - mean) ** 2 for s in scores) / n
    std = var**0.5
    passed = sum(1 for s in scores if s > 0)
    # V5.1 P0：分母排除 unknown（证据不足不算失败）；无有效样本时按 0 处理
    pass_rate = passed / evaluated_n if evaluated_n else 0.0
    # pass^k：连续 k 次（本批次全部有效 run）每次都成功
    pass_all = 1.0 if evaluated_n > 0 and passed == evaluated_n else 0.0
    return {
        "n": n,
        "best": round(best, 3),
        "mean": round(mean, 3),
        "std": round(std, 3),
        "pass_rate": round(pass_rate, 3),
        "pass_at_k": round(pass_rate, 3),   # pass@k = 至少一次成功（与 pass_rate 同义）
        "pass_all": round(pass_all, 3),      # pass^k = 连续 k 次全部成功
        "unknown_count": unknown_count,       # V5.1 P0
        "unknown_rate": round(unknown_count / n, 3) if n else 0.0,  # V5.1 P0
    }
