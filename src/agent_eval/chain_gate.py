"""链路六环节独立门禁（V5.3 P0-2）。

参考腾讯 ADP《Harness Engineering 如何驱动云端智能体》：
"答案对了不代表链路安全，用错工具但碰巧答对，线上仍然危险。"
评测要拆成六个环节分别度量，任何一环出问题都能单独报警，不必等到端到端失败。

六环节定义：
1. intent     意图识别     —— 正确理解任务目标，选对能力/Skill/工具（Decision 层）
2. tool       工具选择     —— 调用了正确的工具，没有用错工具"碰巧答对"
3. param      参数构造     —— 工具参数正确（值域/必填/依赖顺序）
4. evidence   证据使用     —— 检索/读取权威依据，不凭记忆编造（RAG 语义）
5. state      状态管理     —— 上下文不丢失、无死循环、无状态污染
6. expression 表达输出     —— 输出满足契约（格式/文件/字段）

数据来源（全部从评测既有产物推断，不额外请求模型）：
- verdicts：checkpoint 判定结果（tool_call_assert → param；content_contains/file_exists → expression）
- steps：执行步骤（error → state；检索类工具 → evidence）
- traces：工具调用序列（required_tools 命中 → tool/intent；循环检测 → state）
- task：spec 声明（required_tools/required_skills/output_contract → intent/tool/expression）

每环节输出 passed/rate/detail/blocking，由 gate.py 汇总为独立门禁指标。
"""

from __future__ import annotations

from typing import Any

# 检索/证据类工具（与 traces.tool_category 保持一致，此处独立维护避免循环依赖）
_EVIDENCE_TOOLS = frozenset(
    {"read_file", "list_dir", "search", "query", "search_kb", "read", "grep"}
)

# 每环节默认阈值（可被 gate 配置覆盖）
CHAIN_DEFAULTS = {
    "intent": 0.85,       # 意图识别：Decision 层正确率 ≥85%
    "tool": 0.85,         # 工具选择：必选工具触发率 ≥85%
    "param": 0.80,        # 参数构造：参数断言通过率 ≥80%
    "evidence": 0.80,     # 证据使用：证据类任务检索触发率 ≥80%
    "state": 0.90,        # 状态管理：无循环/上下文异常 ≥90%
    "expression": 0.85,   # 表达输出：输出契约通过率 ≥85%
}


def _extract_tool_seq(traces: list[dict]) -> list[str]:
    """从 traces 提取工具调用序列（排除 intent/llm/finish 节点）。"""
    seq = []
    for t in traces or []:
        name = t.get("tool") or t.get("action") or ""
        if name and name != "finish" and t.get("kind") not in ("intent", "llm"):
            seq.append(name)
    return seq


def _step_has_error(s: dict) -> bool:
    obs = " ".join([
        str(s.get("observation", "")),
        str(s.get("result", "")),
        str(s.get("error", "")),
        str(s.get("status", "")),
    ]).lower()
    for kw in ("error", "exception", "traceback", "permission denied",
               "timeout", "connection", "failed"):
        if kw in obs:
            return True
    return False


def evaluate_chain(task, verdicts: list[dict], steps: list[dict] | None = None,
                   traces: list[dict] | None = None) -> dict:
    """评估链路六环节，返回每环节的指标字典。

    Args:
        task: TaskSpec（需有 required_tools/required_skills/output_contract 等字段）
        verdicts: checkpoint 判定结果列表
        steps: 执行步骤（含 observation/result/error/tool 等）
        traces: 轨迹节点列表（含 tool/action/kind/args）

    Returns:
        {"intent": {...}, "tool": {...}, ...}，每项含
        passed/rate/detail/weight 供 gate 汇总。
    """
    steps = steps or []
    traces = traces or []
    verdicts = verdicts or []
    tool_seq = _extract_tool_seq(traces)
    tool_set = set(tool_seq)

    required_tools = list(getattr(task, "required_tools", None) or [])
    required_skills = list(getattr(task, "required_skills", None) or [])
    output_contract = str(getattr(task, "output_contract", "") or "")
    expected_traj = list(getattr(task, "expected_trajectory", None) or [])

    result: dict[str, dict[str, Any]] = {}

    # ---------- 1. intent 意图识别 ----------
    # 信号：Decision 层 verdict（required_tools/required_skills 触发）+ 离题场景
    intent_signals = []
    decision_v = [v for v in verdicts if v.get("type") == "decision_check"]
    if decision_v:
        intent_signals.extend(decision_v)
    # 期望路径的工具被调用 = 意图正确；期望路径全漏 = 意图偏离
    if expected_traj and tool_set:
        hit = sum(1 for t in expected_traj if t in tool_set)
        intent_signals.append({
            "passed": (hit / len(expected_traj)) >= 0.5,
            "detail": f"期望路径 {expected_traj} 命中 {hit}/{len(expected_traj)}",
        })
    if intent_signals:
        intent_passed = sum(1 for s in intent_signals if s.get("passed"))
        intent_rate = intent_passed / len(intent_signals)
        intent_detail = "; ".join(
            str(s.get("detail", s.get("id", ""))) for s in intent_signals[:3]
        )
    else:
        # 无意图信号（无 required_tools/skills/期望路径）→ 不阻断，给中性分
        intent_rate, intent_detail = 1.0, "无意图约束（未声明 required_tools/期望路径）"
    result["intent"] = {
        "label": "意图识别", "rate": round(intent_rate, 3),
        "passed": intent_rate >= CHAIN_DEFAULTS["intent"],
        "detail": intent_detail, "threshold": CHAIN_DEFAULTS["intent"],
    }

    # ---------- 2. tool 工具选择 ----------
    # 信号：required_tools 触发率 + action_layer + tool_order_assert checkpoint
    tool_signals = []
    if required_tools:
        hit = sum(1 for t in required_tools if t in tool_set)
        tool_signals.append({
            "passed": hit == len(required_tools),
            "detail": f"必选工具 {required_tools} 触发 {hit}/{len(required_tools)}",
        })
    action_v = [v for v in verdicts if v.get("type") == "action_check"]
    if action_v:
        tool_signals.extend(action_v)
    order_v = [v for v in verdicts if v.get("type") == "tool_order_assert"]
    if order_v:
        tool_signals.extend(order_v)
    if tool_signals:
        tool_passed = sum(1 for s in tool_signals if s.get("passed"))
        tool_rate = tool_passed / len(tool_signals)
        tool_detail = "; ".join(
            str(s.get("detail", s.get("id", ""))) for s in tool_signals[:3]
        )
    else:
        tool_rate, tool_detail = 1.0, "无工具约束（未声明 required_tools）"
    result["tool"] = {
        "label": "工具选择", "rate": round(tool_rate, 3),
        "passed": tool_rate >= CHAIN_DEFAULTS["tool"],
        "detail": tool_detail, "threshold": CHAIN_DEFAULTS["tool"],
    }

    # ---------- 3. param 参数构造 ----------
    # 信号：tool_call_assert checkpoint（V3.8 参数级断言）
    param_v = [v for v in verdicts if v.get("type") == "tool_call_assert"]
    if param_v:
        param_passed = sum(1 for v in param_v if v.get("passed"))
        param_rate = param_passed / len(param_v)
        failed = [v.get("checkpoint_id") or v.get("id", "?") for v in param_v if not v.get("passed")]
        param_detail = f"参数断言 {param_passed}/{len(param_v)}" + (f"，失败: {failed[:3]}" if failed else "")
    else:
        param_rate, param_detail = 1.0, "无参数断言（未声明 tool_call_assert）"
    result["param"] = {
        "label": "参数构造", "rate": round(param_rate, 3),
        "passed": param_rate >= CHAIN_DEFAULTS["param"],
        "detail": param_detail, "threshold": CHAIN_DEFAULTS["param"],
    }

    # ---------- 4. evidence 证据使用 ----------
    # 信号：任务需要证据（RAG/检索/内容生成类）时，是否调用了检索类工具
    # 通过任务描述/tags/期望路径判断是否需要证据
    need_evidence = (
        len(expected_traj) > 0 and any(t in _EVIDENCE_TOOLS for t in expected_traj)
    ) or any(kw in str(getattr(task, "description", "")).lower()
              for kw in ("检索", "查询", "搜索", "依据", "资料", "文档", "search", "read"))
    if need_evidence:
        ev_hit = [t for t in _EVIDENCE_TOOLS if t in tool_set]
        # 检索工具覆盖：期望证据工具命中率（只统计期望路径内证据工具，避免额外工具撑高）
        if expected_traj:
            ev_expected = [t for t in expected_traj if t in _EVIDENCE_TOOLS]
            ev_hit_expected = [t for t in ev_hit if t in ev_expected]
            ev_rate = (len(ev_hit_expected) / len(ev_expected)) if ev_expected else 1.0
        else:
            ev_rate = 1.0 if ev_hit else 0.0
        # 幻觉检测：no_hallucinated_tool 失败 → 证据环节扣分
        hallu_v = [v for v in verdicts if v.get("type") == "no_hallucinated_tool" and not v.get("passed")]
        if hallu_v:
            ev_rate = min(ev_rate, 0.0)
            ev_detail = "检测到幻觉工具调用"
        else:
            ev_detail = f"证据工具命中 {ev_hit or '无'}" if ev_hit else "未调用任何检索/证据工具"
        result["evidence"] = {
            "label": "证据使用", "rate": round(ev_rate, 3),
            "passed": ev_rate >= CHAIN_DEFAULTS["evidence"],
            "detail": ev_detail, "threshold": CHAIN_DEFAULTS["evidence"],
        }
    else:
        result["evidence"] = {
            "label": "证据使用", "rate": 1.0, "passed": True,
            "detail": "非证据依赖任务", "threshold": CHAIN_DEFAULTS["evidence"],
        }

    # ---------- 5. state 状态管理 ----------
    # 信号：no_loop_assert + 步骤错误率 + context_state 归因
    state_signals = []
    loop_v = [v for v in verdicts if v.get("type") == "no_loop_assert"]
    if loop_v:
        state_signals.extend(loop_v)
    # 上下文异常：归因为 context_state 或步骤中上下文关键词
    ctx_v = [v for v in verdicts if v.get("type") == "context_check" and not v.get("passed")]
    if ctx_v:
        state_signals.extend(ctx_v)
    # 错误步占比（容忍 10% 内）
    if steps:
        err_count = sum(1 for s in steps if _step_has_error(s))
        err_rate = err_count / len(steps)
        if err_rate > 0.1:
            state_signals.append({
                "passed": False,
                "detail": f"错误步占比 {err_rate:.0%}（>{10}%）",
            })
    if state_signals:
        st_passed = sum(1 for s in state_signals if s.get("passed"))
        st_rate = st_passed / len(state_signals)
        st_detail = "; ".join(str(s.get("detail", s.get("id", ""))) for s in state_signals[:3])
    else:
        st_rate, st_detail = 1.0, "无状态异常信号"
    result["state"] = {
        "label": "状态管理", "rate": round(st_rate, 3),
        "passed": st_rate >= CHAIN_DEFAULTS["state"],
        "detail": st_detail, "threshold": CHAIN_DEFAULTS["state"],
    }

    # ---------- 6. expression 表达输出 ----------
    # 信号：输出契约类 checkpoint（file_exists/content_contains/content_not_contains/cmd_exit_zero）
    expr_types = {"file_exists", "file_not_exists", "content_contains",
                  "content_not_contains", "cmd_exit_zero", "browser_url_contains",
                  "ui_element_exists", "http_status"}
    expr_v = [v for v in verdicts if v.get("type") in expr_types]
    if expr_v:
        expr_passed = sum(1 for v in expr_v if v.get("passed"))
        expr_rate = expr_passed / len(expr_v)
        failed = [v.get("checkpoint_id") or v.get("id", "?") for v in expr_v if not v.get("passed")]
        expr_detail = f"输出契约 {expr_passed}/{len(expr_v)}" + (f"，失败: {failed[:3]}" if failed else "")
    elif output_contract:
        expr_rate, expr_detail = 0.0, f"声明输出契约 '{output_contract}' 但无契约类校验点"
    else:
        expr_rate, expr_detail = 1.0, "无输出契约校验点"
    result["expression"] = {
        "label": "表达输出", "rate": round(expr_rate, 3),
        "passed": expr_rate >= CHAIN_DEFAULTS["expression"],
        "detail": expr_detail, "threshold": CHAIN_DEFAULTS["expression"],
    }

    return result


def chain_summary(chain: dict) -> dict:
    """汇总六环节结果：passed 环节数、总通过率、失败环节列表。"""
    items = list(chain.values())
    total = len(items)
    passed = sum(1 for it in items if it.get("passed"))
    failed = [it["label"] for it in items if not it.get("passed")]
    overall = passed / total if total else 1.0
    return {
        "passed": passed,
        "total": total,
        "overall_rate": round(overall, 3),
        "failed_links": failed,
        "links": items,
    }
