"""评测标准可靠性四验证（V5.1 P0-2）。

参考《Agent评测：评测体系、工程实践与持续演进》：
一套评测标准（Rubric + Grader）在投入使用前，需要验证四个维度：

1. 人人一致（inter-rater）：两位以上人类专家对同一批 Case 的判定是否一致
   → Cohen's Kappa，κ ≥ 0.75 视为"几乎一致"，κ < 0.40 说明标准本身有歧义
2. 人机一致（human-LLM）：LLM Judge 与人类专家判定的一致性
   → 简单一致率 + Cohen's Kappa + 偏差方向（LLM 偏严/偏松）
3. 业务有效性（business validity）：评测得分是否与真实业务结果相关
   → Spearman 秩相关；得分高的 Agent 上线后业务表现是否同样好
4. 覆盖度（coverage）：任务集是否覆盖目标能力维度
   → 按 tier / pack / L 级 / 能力标签统计覆盖矩阵，暴露评测盲区
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, Sequence

# ============ 1. 人人一致：Cohen's Kappa ============


def cohens_kappa(a: Sequence[int], b: Sequence[int]) -> float:
    """Cohen's Kappa：两位评判者分类一致性（已修正偶然一致）。

    公式：κ = (p_o - p_e) / (1 - p_e)
    - p_o：观察到的一致率
    - p_e：偶然一致概率（两评判者各类别边缘概率乘积之和）

    κ 解释（Landis & Koch）：
    - < 0.00 差（poor）
    - 0.00-0.20 轻微（slight）
    - 0.21-0.40 一般（fair）
    - 0.41-0.60 中等（moderate）
    - 0.61-0.80 高度一致（substantial）
    - 0.81-1.00 几乎完全一致（almost perfect）
    """
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    n = len(a)
    # 混淆矩阵
    classes = sorted(set(a) | set(b))
    table = {x: {y: 0 for y in classes} for x in classes}
    for x, y in zip(a, b):
        table[x][y] += 1
    # 观察到的一致率
    p_o = sum(table[x][x] for x in classes) / n
    # 偶然一致概率
    p_a = {x: sum(table[x].values()) / n for x in classes}
    p_b = {y: sum(table[x][y] for x in classes) / n for y in classes}
    p_e = sum(p_a[x] * p_b[x] for x in classes)
    if p_e >= 1.0:
        return 0.0
    return round((p_o - p_e) / (1 - p_e), 3)


KAPPA_LABELS = [
    (0.81, "几乎完全一致"),
    (0.61, "高度一致"),
    (0.41, "中等一致"),
    (0.21, "一般"),
    (0.00, "轻微/差"),
]


def kappa_label(kappa: float) -> str:
    for threshold, label in KAPPA_LABELS:
        if kappa >= threshold:
            return label
    return "差"


def inter_rater_agreement(ratings_list: list[list[int]]) -> dict:
    """多人（≥2）对同一批 Case 的判定（0=失败 1=通过 2=unknown），两两计算 Kappa。

    返回：两两 Kappa 矩阵 + 平均 Kappa + 一致性结论。
    平均 κ < 0.40 → 判定标准本身存在歧义，需先修订 Rubric 再投入使用。
    """
    n_raters = len(ratings_list)
    if n_raters < 2:
        return {"error": "需要至少 2 位评判者"}
    length = len(ratings_list[0])
    pair_kappas: list[float] = []
    for i in range(n_raters):
        for j in range(i + 1, n_raters):
            pair_kappas.append(cohens_kappa(ratings_list[i], ratings_list[j]))
    avg_kappa = sum(pair_kappas) / len(pair_kappas) if pair_kappas else 0.0
    # 结论
    if avg_kappa >= 0.75:
        conclusion = "人人一致达标（κ≥0.75）：判定标准清晰，可用于正式评测"
    elif avg_kappa >= 0.40:
        conclusion = "人人一致基本可用（0.40≤κ<0.75）：存在少量歧义，建议仲裁争议 Case"
    else:
        conclusion = "人人一致不足（κ<0.40）：Rubric 存在明显歧义，必须修订后再评测"
    return {
        "n_raters": n_raters,
        "n_cases": length,
        "pairwise_kappa": pair_kappas,
        "avg_kappa": round(avg_kappa, 3),
        "kappa_label": kappa_label(avg_kappa),
        "conclusion": conclusion,
    }


# ============ 2. 人机一致：LLM Judge vs 人类专家 ============


def human_llm_agreement(human: Sequence[int], llm: Sequence[int]) -> dict:
    """人类专家 vs LLM Judge 对同一批 Case 的判定一致性。

    判定取值约定：0=失败 1=通过 2=unknown。
    除一致率与 Kappa 外，额外输出偏差方向：
    - llm_harsher：LLM 判为失败而人类判为通过的 Case 数（LLM 偏严）
    - llm_looser：LLM 判为通过而人类判为失败的 Case 数（LLM 偏松）
    """
    if len(human) != len(llm) or len(human) == 0:
        return {"error": "判定序列长度不一致或为空"}
    n = len(human)
    agree = sum(1 for h, l in zip(human, llm) if h == l)
    llm_harsher = sum(1 for h, l in zip(human, llm) if h != 2 and l == 0 and h == 1)
    llm_looser = sum(1 for h, l in zip(human, llm) if h == 0 and l == 1)
    agreement_rate = agree / n
    kappa = cohens_kappa(human, llm)

    if agreement_rate >= 0.90 and kappa >= 0.61:
        conclusion = "人机一致达标：LLM Judge 可替代人工判分用于大规模评测"
    elif agreement_rate >= 0.80:
        conclusion = "人机一致基本可用：LLM Judge 可用于初筛，但关键 Case 需人工复核"
    else:
        conclusion = "人机一致不足：LLM Judge 与人类专家分歧大，需检查 Prompt/Rubric 或改用确定性判分"

    if llm_harsher > llm_looser:
        bias = f"LLM 偏严（多判失败 {llm_harsher - llm_looser} 例），可能低估 Agent 能力"
    elif llm_looser > llm_harsher:
        bias = f"LLM 偏松（多判通过 {llm_looser - llm_harsher} 例），可能高估 Agent 能力"
    else:
        bias = "无系统性偏差"

    return {
        "n": n,
        "agreement_rate": round(agreement_rate, 3),
        "kappa": kappa,
        "kappa_label": kappa_label(kappa),
        "llm_harsher": llm_harsher,
        "llm_looser": llm_looser,
        "bias": bias,
        "conclusion": conclusion,
    }


# ============ 3. 业务有效性：评测得分 vs 真实业务结果 ============


def _spearman_rank(x: Sequence[float]) -> list[float]:
    """为序列赋秩（并列取平均秩）。"""
    order = sorted(range(len(x)), key=lambda i: x[i])
    ranks = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-based 平均秩
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(x: Sequence[float], y: Sequence[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy) ** 0.5


def business_validity(eval_scores: Sequence[float], business_outcomes: Sequence[float]) -> dict:
    """评测得分与真实业务结果的相关性（Spearman 秩相关）。

    例如：3 个 Agent 的评测得分 [0.9, 0.6, 0.4] 对应上线后业务指标 [98, 71, 55]。
    ρ ≥ 0.7：评测有业务预测力；0.3-0.7：中等；<0.3：评测指标与业务脱节。
    """
    if len(eval_scores) != len(business_outcomes) or len(eval_scores) < 2:
        return {"error": "需要至少 2 组 (评测得分, 业务结果) 对"}
    rx, ry = _spearman_rank(eval_scores), _spearman_rank(business_outcomes)
    rho = _pearson(rx, ry)
    if rho >= 0.7:
        conclusion = "业务有效性高：评测得分能预测真实业务表现"
    elif rho >= 0.3:
        conclusion = "业务有效性中等：评测与业务部分相关，建议补充真实场景 Case"
    else:
        conclusion = "业务有效性不足：评测得分与业务结果脱节，需检查评测指标与业务目标的映射"
    return {
        "n": len(eval_scores),
        "spearman_rho": round(rho, 3),
        "pearson_r": round(_pearson(list(eval_scores), list(business_outcomes)), 3),
        "conclusion": conclusion,
    }


# ============ 4. 覆盖度：任务集 × 能力维度 ============


def coverage_report(tasks_dir: Path, task_ids: Iterable[str] | None = None) -> dict:
    """统计任务集在 tier / pack / L 级 / 能力标签上的覆盖。

    输入：
    - tasks_dir：tasks 根目录（含 manifest.yaml + <id>/spec.yaml）
    - task_ids：可选，限定统计范围（如某次批量评测选中的任务）

    输出覆盖矩阵与盲区提示：若某 tier 或能力维度无任务覆盖，说明评测存在盲区。
    """
    from agent_eval.spec import load_manifest

    manifest = load_manifest(tasks_dir)
    tier_by_task: dict[str, str] = {}
    for entry in manifest.get("tasks", []):
        tier_by_task[str(entry.get("id", ""))] = entry.get("tier", "?")

    scope_ids = set(task_ids) if task_ids else set(tier_by_task.keys())
    # 任务 id -> 能力标签（spec.yaml 的 tags / level）
    level_counter: Counter[str] = Counter()
    tag_counter: Counter[str] = Counter()
    tier_counter: Counter[str] = Counter()
    per_task: dict[str, dict] = {}
    for tid in sorted(scope_ids):
        spec_path = tasks_dir / tid / "spec.yaml"
        meta: dict = {"tier": tier_by_task.get(tid, "?"), "level": "?", "tags": []}
        if spec_path.exists():
            try:
                import yaml

                spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
                meta["level"] = str(spec.get("level", spec.get("task_level", "?")))
                meta["tags"] = list(spec.get("tags", spec.get("capabilities", [])) or [])
            except Exception:
                pass
        tier_counter[meta["tier"]] += 1
        level_counter[meta["level"]] += 1
        for t in meta["tags"]:
            tag_counter[str(t)] += 1
        per_task[tid] = meta

    # tier 名称映射
    tier_labels = {k: v.get("label", k) for k, v in manifest.get("tiers", {}).items()}
    tier_coverage = {
        k: {"count": v, "label": tier_labels.get(k, k)}
        for k, v in sorted(tier_counter.items(), key=lambda x: -x[1])
    }
    total = len(scope_ids)
    # 盲区：manifest 声明了但本次未覆盖的 tier
    declared_tiers = set(tier_by_task.values())
    covered_tiers = set(tier_counter.keys())
    blind_tiers = sorted(declared_tiers - covered_tiers)

    return {
        "total_tasks": total,
        "tier_coverage": tier_coverage,
        "level_distribution": dict(sorted(level_counter.items(), key=lambda x: -x[1])),
        "tag_distribution": dict(sorted(tag_counter.items(), key=lambda x: -x[1])),
        "blind_tiers": blind_tiers,
        "per_task": per_task,
        "conclusion": (
            f"任务集覆盖 {len(covered_tiers)}/{len(declared_tiers)} 个 tier"
            + (f"；盲区 tier: {blind_tiers}" if blind_tiers else "")
            + f"；能力标签 {len(tag_counter)} 类"
        ),
    }


# ============ 便捷入口 ============


def full_reliability_report(
    tasks_dir: Path,
    human_ratings: list[list[int]] | None = None,
    llm_ratings: Sequence[int] | None = None,
    eval_scores: Sequence[float] | None = None,
    business_outcomes: Sequence[float] | None = None,
    task_ids: Iterable[str] | None = None,
) -> dict:
    """四验证一站式报告。缺数据的维度标记为 not_available，不阻塞其余维度。"""
    report: dict = {
        "version": "V5.1 P0-2",
        "reference": "《Agent评测：评测体系、工程实践与持续演进》",
    }
    report["inter_rater"] = (
        inter_rater_agreement(human_ratings) if human_ratings else {"not_available": True}
    )
    report["human_llm"] = (
        human_llm_agreement(
            human_ratings[0] if human_ratings else [], list(llm_ratings or [])
        )
        if llm_ratings is not None
        else {"not_available": True}
    )
    report["business_validity"] = (
        business_validity(eval_scores, business_outcomes)
        if eval_scores and business_outcomes
        else {"not_available": True}
    )
    report["coverage"] = coverage_report(tasks_dir, task_ids)
    return report


# ============ CLI 入口（agent-eval reliability） ============


def run_reliability_report(
    tasks_dir: Path,
    human_ratings_file: str | None = None,
    llm_ratings_file: str | None = None,
    business_file: str | None = None,
    task_ids: Iterable[str] | None = None,
) -> dict:
    """从 JSON 文件读取评分数据生成四验证报告（供 CLI 调用）。

    - human_ratings_file: {"raters": [[0,1,1,...], [0,1,0,...]]}
    - llm_ratings_file: {"ratings": [0,1,1,...]}  (与第一位 human rater 对齐比较)
    - business_file: {"eval_scores": [..], "business_outcomes": [..]}
    """
    import json as _json

    def _load(path: str | None) -> dict | None:
        if not path:
            return None
        return _json.loads(Path(path).read_text(encoding="utf-8"))

    hr = _load(human_ratings_file)
    lr = _load(llm_ratings_file)
    bf = _load(business_file)
    return full_reliability_report(
        tasks_dir=tasks_dir,
        human_ratings=hr.get("raters") if hr else None,
        llm_ratings=lr.get("ratings") if lr else None,
        eval_scores=bf.get("eval_scores") if bf else None,
        business_outcomes=bf.get("business_outcomes") if bf else None,
        task_ids=task_ids,
    )
