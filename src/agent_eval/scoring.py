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

    # 0. V4.2：检测 Decision 层失败（required_tools/required_skills 未调用）
    decision_failures = [v for v in failed_verdicts if v.get("type") == "decision_check" or v.get("id") == "decision_layer"]
    if decision_failures:
        cat = FAILURE_CATEGORIES["skill_routing"]
        return {"category": "skill_routing", **cat, "confidence": 0.95,
                "detail": f"Decision 层失败: {decision_failures[0].get('detail', '必需能力未调用')}"}

    # 0.5 V4.2：检测 Action 层失败（工具失败未修复/顺序错误/死循环）
    action_failures = [v for v in failed_verdicts if v.get("type") == "action_check" or v.get("id") == "action_layer"]
    if action_failures:
        cat = FAILURE_CATEGORIES["tool_param"]
        return {"category": "tool_param", **cat, "confidence": 0.9,
                "detail": f"Action 层失败: {action_failures[0].get('detail', '工具执行异常')}"}

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


def score_trajectory(steps: list[dict] | None, max_steps: int = None, max_errors: int = None) -> dict:
    """计算轨迹指标评分。
    
    参考文章中的轨迹指标：
    - step_count: 总步数
    - error_count: 错误步数
    - duplicate_calls: 重复调用次数
    - efficiency_score: 效率得分（0-1）
    
    效率计算逻辑：
    - 步数越少得分越高（最多 max_steps 步）
    - 错误越少得分越高（最多 max_errors 个错误）
    """
    steps = steps or []
    step_count = len(steps)
    
    # 计算错误步数
    error_count = sum(1 for s in steps if s.get("error") or s.get("status") == "error")
    
    # 计算重复调用次数
    seen_calls = set()
    duplicate_calls = 0
    for s in steps:
        tool_name = s.get("tool", s.get("name", ""))
        args = str(s.get("args", s.get("arguments", "")))
        call_key = f"{tool_name}:{args}"
        if call_key in seen_calls:
            duplicate_calls += 1
        else:
            seen_calls.add(call_key)
    
    # 计算效率得分
    max_steps = max_steps or 20  # 默认最多 20 步
    max_errors = max_errors or 3  # 默认最多 3 个错误
    
    step_score = max(0.0, 1.0 - (step_count / max_steps))
    error_score = max(0.0, 1.0 - (error_count / max_errors))
    duplicate_score = max(0.0, 1.0 - (duplicate_calls / 5))  # 重复 5 次以上扣完
    
    efficiency_score = round((step_score * 0.4 + error_score * 0.4 + duplicate_score * 0.2), 3)
    
    return {
        "step_count": step_count,
        "error_count": error_count,
        "duplicate_calls": duplicate_calls,
        "efficiency_score": efficiency_score,
        "violations": [
            f"步数 {step_count} 超过上限 {max_steps}" if step_count > max_steps else None,
            f"错误数 {error_count} 超过上限 {max_errors}" if error_count > max_errors else None,
            f"重复调用 {duplicate_calls} 次" if duplicate_calls > 3 else None,
        ]
    }


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

    # 轨迹指标评分
    trajectory_metrics = score_trajectory(steps)
    
    # 综合得分：规则评分 70% + 轨迹效率 30%
    final_score = round(task.weight * (rate * 0.7 + trajectory_metrics["efficiency_score"] * 0.3), 3)
    
    return {
        "task_id": task.id,
        "weight": task.weight,
        "score": final_score,
        "rule_score": round(task.weight * rate, 3),
        "trajectory_score": round(task.weight * trajectory_metrics["efficiency_score"], 3),
        "passed": passed,
        "total": total,
        "pass_rate": round(rate, 3),
        "risk_level": risk_level,
        "hard_gate_blocked": hard_gate_blocked,
        "hard_gate_failed_checkpoints": hard_gate_failed,
        "failure_attribution": failure_attribution,
        "trajectory_metrics": trajectory_metrics,
    }
