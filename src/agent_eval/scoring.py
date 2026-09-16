"""评分器（Day 3）：由 verdicts 计算任务得分。

规则（MVP 版）：
- 任务得分 = 任务权重 × 校验点通过率
- 通过率 = 通过的校验点数 / 校验点总数
- V4.0 Hard Gate：P0 风险任务任一 checkpoint 失败即 hard_gate_blocked=True，
  不被其他高分项抵消，CI 门禁直接 BLOCK。
- V4.0 失败归因标准化：6类映射（data_logic/skill_routing/tool_param/
  output_contract/environment/model_semantic），参考《工具型 Agent 的分层评测方法》。
"""

from __future__ import annotations


# 6类失败归因定义（参考 ODAR 文章 4.5 节）
FAILURE_CATEGORIES = {
    "data_logic": {
        "label": "数据/指标逻辑",
        "fix_target": "SQL、数据路由、统计口径",
        "evidence": "查询成功但关键数值断言失败",
        "color": "#f59e0b",
    },
    "skill_routing": {
        "label": "Skill 路由/策略",
        "fix_target": "路由、系统指令、Skill 描述",
        "evidence": "选错 Skill，或违反任务约束",
        "color": "#8b5cf6",
    },
    "tool_param": {
        "label": "Tool 参数/顺序",
        "fix_target": "Tool Schema、计划、执行器",
        "evidence": "漏调用、参数错误、依赖顺序错误",
        "color": "#ef4444",
    },
    "output_contract": {
        "label": "输出契约",
        "fix_target": "Contract、确定性序列化",
        "evidence": "内容可读但 Schema/字段不满足接口",
        "color": "#3b82f6",
    },
    "environment": {
        "label": "环境/权限",
        "fix_target": "Runtime、白名单、部署配置",
        "evidence": "必需工具不存在或 permission denied",
        "color": "#6b7280",
    },
    "model_semantic": {
        "label": "模型语义",
        "fix_target": "模型、Prompt、上下文",
        "evidence": "证据完整，但解释或归纳仍然错误",
        "color": "#ec4899",
    },
}


def classify_failure(verdicts: list[dict], steps: list[dict] | None = None) -> dict:
    """将失败归因到6类标准化映射之一。

    判定优先级（从高到低）：
    1. environment：steps 中有 permission denied / error / 工具不存在
    2. tool_param：tool_call_assert 类型 checkpoint 失败
    3. data_logic：content_contains 数值类断言失败（含数字的断言）
    4. output_contract：file_exists 成功但 content_contains 失败
    5. skill_routing：required_skills 未触发或 Skill 顺序错误
    6. model_semantic：所有工具调用成功但最终结果错误（兜底）

    返回: {"category": "...", "label": "...", "fix_target": "...", "evidence": "...", "color": "...", "confidence": 0.0~1.0}
    """
    steps = steps or []
    failed_verdicts = [v for v in verdicts if not v.get("passed")]

    if not failed_verdicts:
        return {"category": None, "label": "无失败", "fix_target": "", "evidence": "全部通过", "color": "#16a34a", "confidence": 1.0}

    # 1. 检测环境/权限错误
    env_keywords = ["permission denied", "not found", "no such file", "command not found",
                     "error", "exception", "traceback", "unavailable", "timeout", "connection"]
    step_errors = []
    for s in steps:
        obs = (str(s.get("observation", "")) + " " + str(s.get("result", "")) + " " + str(s.get("error", ""))).lower()
        for kw in env_keywords:
            if kw in obs:
                step_errors.append(kw)
                break
    if step_errors:
        cat = FAILURE_CATEGORIES["environment"]
        return {"category": "environment", **cat, "confidence": 0.8,
                "detail": f"检测到环境错误: {', '.join(step_errors[:3])}"}

    # 2. 检测工具参数错误（tool_call_assert 失败）
    tool_param_failures = [v for v in failed_verdicts if v.get("type") == "tool_call_assert"]
    if tool_param_failures:
        cat = FAILURE_CATEGORIES["tool_param"]
        return {"category": "tool_param", **cat, "confidence": 0.9,
                "detail": f"工具参数断言失败: {', '.join(v.get('checkpoint_id') or v.get('id','?') for v in tool_param_failures)}"}

    # 3. 检测数据/指标逻辑错误（content_contains 数值类）
    data_failures = []
    for v in failed_verdicts:
        if v.get("type") == "content_contains":
            detail = str(v.get("detail", "")) + " " + str(v.get("expected", ""))
            # 检测是否包含数字（数值断言）
            if any(c.isdigit() for c in detail):
                data_failures.append(v)
    if data_failures:
        cat = FAILURE_CATEGORIES["data_logic"]
        return {"category": "data_logic", **cat, "confidence": 0.7,
                "detail": f"数值断言失败: {', '.join(v.get('checkpoint_id') or v.get('id','?') for v in data_failures)}"}

    # 4. 检测输出契约错误（file_exists 成功但 content_contains 失败）
    file_exists_passed = any(v.get("type") == "file_exists" and v.get("passed") for v in verdicts)
    content_failures = [v for v in failed_verdicts if v.get("type") == "content_contains"]
    if file_exists_passed and content_failures:
        cat = FAILURE_CATEGORIES["output_contract"]
        return {"category": "output_contract", **cat, "confidence": 0.75,
                "detail": f"文件存在但内容不匹配: {', '.join(v.get('checkpoint_id') or v.get('id','?') for v in content_failures)}"}

    # 5. 检测 Skill 路由错误（后续可扩展 required_skills 检查）
    # 暂时通过失败类型推断

    # 6. 兜底：模型语义错误
    cat = FAILURE_CATEGORIES["model_semantic"]
    return {"category": "model_semantic", **cat, "confidence": 0.5,
            "detail": f"未匹配到明确工程错误，可能为模型语义理解问题: {', '.join(v.get('checkpoint_id') or v.get('id','?') for v in failed_verdicts)}"}


def score_task(task, verdicts: list[dict], steps: list[dict] | None = None) -> dict:
    total = len(verdicts)
    passed = sum(1 for v in verdicts if v.get("passed"))
    rate = passed / total if total else 0.0

    # Hard Gate 独立判断：P0 风险任务任一 checkpoint 失败即 BLOCK
    risk_level = getattr(task, "risk_level", None) or "P2"
    hard_gate_blocked = False
    hard_gate_failed = []
    if risk_level == "P0" and passed < total:
        hard_gate_blocked = True
        hard_gate_failed = [
            v.get("checkpoint_id") or v.get("id", "unknown")
            for v in verdicts if not v.get("passed")
        ]

    # 失败归因标准化（6类映射）
    failure_attribution = classify_failure(verdicts, steps) if passed < total else None

    return {
        "task_id": task.id,
        "weight": task.weight,
        "score": round(task.weight * rate, 3),
        "passed": passed,
        "total": total,
        "pass_rate": round(rate, 3),
        "risk_level": risk_level,
        "hard_gate_blocked": hard_gate_blocked,
        "hard_gate_failed_checkpoints": hard_gate_failed,
        "failure_attribution": failure_attribution,
    }
