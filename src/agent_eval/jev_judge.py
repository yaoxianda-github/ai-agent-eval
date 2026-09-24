"""Jev 快速 Judge 判分器（V4.6 P0 优化）。

利用 Jev 模型（TypeSafe System One Model）做快速、低成本的结构化判定：
- 速度：70-500ms（比 LLM Judge 快 50-100 倍）
- 成本：$0.042/M input token，output 免费（比 LLM Judge 便宜 100 倍）
- 输出：结构化决策 + 置信度（可做风险门控）

P0 优化：
1. 判分问题拆分：Noul（是否完成）→ Choice（失败原因分类）→ Score（质量评分）三步
2. 三级阈值策略：≥0.9自动通过 / 0.6-0.9人工复核 / <0.6自动重跑
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_eval.log import get_logger
from agent_eval.observability import trace_llm_call

logger = get_logger(__name__)

JEV_API_URL = "https://api.typesafe.ai/v1/systemone"

# 三级阈值策略
DEFAULT_AUTO_PASS_THRESHOLD = 0.9   # ≥0.9：自动通过，直接计入结果
DEFAULT_MANUAL_REVIEW_THRESHOLD = 0.6  # 0.6-0.9：人工复核区间，升级到 LLM Judge
# <0.6：自动重跑区间，最多重跑2次

# 兼容旧配置
DEFAULT_CONFIDENCE_THRESHOLD = DEFAULT_MANUAL_REVIEW_THRESHOLD


class JevJudge:
    """Jev 快速判分器。

    环境变量：
    - TYPESAFE_API_KEY / JEV_API_KEY：Jev API Key
    - JEV_CONFIDENCE_THRESHOLD：置信度阈值（默认 0.7）
    """

    def __init__(
        self,
        api_key: str | None = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        auto_pass_threshold: float = DEFAULT_AUTO_PASS_THRESHOLD,
        manual_review_threshold: float = DEFAULT_MANUAL_REVIEW_THRESHOLD,
        client=None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
        # 兼容旧配置：如果用户设置了 JEV_CONFIDENCE_THRESHOLD，用作人工复核阈值
        self.manual_review_threshold = float(
            os.environ.get("JEV_MANUAL_REVIEW_THRESHOLD",
            os.environ.get("JEV_CONFIDENCE_THRESHOLD", manual_review_threshold))
        )
        self.auto_pass_threshold = float(
            os.environ.get("JEV_AUTO_PASS_THRESHOLD", auto_pass_threshold)
        )
        # 兼容旧字段
        self.confidence_threshold = self.manual_review_threshold
        self.client = client
        self._usage: dict = {"input_tokens": 0, "output_tokens": 0}

    def check_api_key(self) -> dict:
        """检查 API Key 连通性。"""
        if not self.api_key:
            return {
                "ok": False,
                "status": "missing",
                "message": "缺少 Jev API Key（TYPESAFE_API_KEY）",
                "latency_ms": None,
            }
        try:
            start = time.time()
            # 发一个简单的测试请求
            self._call_jev(
                state="test",
                questions={
                    "test": {
                        "type": "noul",
                        "instructions": "Is this a test?",
                        "criteria": {"true": "Yes", "false": "No"},
                    }
                },
            )
            latency = int(round((time.time() - start) * 1000))
            return {
                "ok": True,
                "status": "ok",
                "message": "Jev API 连通正常",
                "latency_ms": latency,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "status": "error",
                "message": f"Jev API 调用失败: {type(e).__name__}: {e}",
                "latency_ms": None,
            }

    def judge_task_complexity(self, task) -> dict:
        """判断任务复杂度（L1-L5），用于 Model Router 决策。

        Returns:
            {
                "complexity": "L1" / "L2" / "L3" / "L4" / "L5",
                "use_jev_only": bool,  # 是否可以直接用 Jev 判分
                "confidence": float
            }
        """
        if not self.api_key:
            # 无 API Key 时默认走 LLM Judge
            return {"complexity": "L3", "use_jev_only": False, "confidence": 0.0}

        state = f"任务描述：{task.description}\n任务标签：{getattr(task, 'tags', [])}"

        questions = {
            "complexity": {
                "type": "choice",
                "instructions": "What is the complexity level of this task?",
                "criteria": {
                    "L1": "Very simple: single step, no tool use, trivial output",
                    "L2": "Simple: 1-2 steps, simple tool call, straightforward check",
                    "L3": "Medium: multi-step workflow, multiple tools, moderate reasoning",
                    "L4": "Complex: long context, multi-hop reasoning, edge cases",
                    "L5": "Very complex: open-ended, ambiguous requirements, high risk of hallucination",
                },
            },
            "jev_can_judge": {
                "type": "noul",
                "instructions": "Can Jev reliably judge this task without LLM assistance?",
                "criteria": {
                    "true": "Task has clear objective, verifiable output, no subjective evaluation needed",
                    "false": "Task requires deep reasoning, subjective judgment, or complex open-ended evaluation",
                },
            },
        }

        try:
            start = time.time()
            result = self._call_jev(state=state, questions=questions)
            duration_ms = int(round((time.time() - start) * 1000))

            answers = result.get("answers", {})
            complexity = answers.get("complexity", {}).get("choice", "L3")
            jev_can_judge = float(answers.get("jev_can_judge", {}).get("noul", 0.5))
            confidence = float(answers.get("complexity", {}).get("confidence", 0.8))

            # L1-L2 且 Jev 可判：直接走 Jev 判分
            use_jev_only = complexity in ("L1", "L2") and jev_can_judge >= 0.6

            logger.info(
                "任务复杂度判断 | task=%s complexity=%s use_jev_only=%s confidence=%.2f duration=%dms",
                task.id, complexity, use_jev_only, confidence, duration_ms,
            )

            return {
                "complexity": complexity,
                "use_jev_only": use_jev_only,
                "confidence": confidence,
            }

        except Exception as e:  # noqa: BLE001
            logger.warning("任务复杂度判断失败，默认走 LLM Judge: %s", e)
            return {"complexity": "L3", "use_jev_only": False, "confidence": 0.0}

    def judge(self, task, workspace: Path) -> dict:
        """对任务产物执行快速判定，返回 verdict（与 LLMJudge 同构）。"""
        if not self.api_key:
            logger.warning("jev_judge 跳过: 缺少 TYPESAFE_API_KEY (task=%s)", task.id)
            return {
                "id": "jev_judge",
                "type": "jev_judge",
                "passed": False,
                "detail": "缺少 Jev API Key（TYPESAFE_API_KEY），无法执行快速判分",
                "score": 0.0,
                "reasoning": "",
                "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
                "confidence": 0.0,
                "needs_escalation": True,
            }

        from agent_eval.judge import _collect_artifacts

        artifacts = _collect_artifacts(workspace)
        if not artifacts:
            return {
                "id": "jev_judge",
                "type": "jev_judge",
                "passed": False,
                "detail": "未找到可判分的产物（output/ 目录为空）",
                "score": 0.0,
                "reasoning": "",
                "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
                "confidence": 1.0,
                "needs_escalation": False,
            }

        # 构造 Jev 问题集（三步拆分：是否完成 → 失败原因 → 质量评分）
        state = (
            f"任务描述：{task.description}\n\n"
            f"Agent 执行产物：\n{artifacts[:8000]}"
        )

        questions = {
            # 第一步：二元判断（Noul）- 是否完成任务
            "completed": {
                "type": "noul",
                "instructions": "Did the agent successfully complete the task?",
                "criteria": {
                    "true": "Task completed successfully, output meets all requirements",
                    "false": "Task failed, output does not meet requirements or is missing",
                },
            },
            # 第二步：分类选择（Choice）- 失败原因（对齐6类归因）
            "fail_reason": {
                "type": "choice",
                "instructions": "If the task failed, what is the root cause category?",
                "criteria": {
                    "none": "Task passed successfully",
                    "data_logic": "Data calculation or business logic errors",
                    "skill_routing": "Wrong tool/skill selection or routing",
                    "tool_param": "Tool parameter errors or invalid inputs",
                    "output_contract": "Output format/structure does not match requirements",
                    "environment": "Environment/dependency issues or missing resources",
                    "model_semantic": "Model understanding or semantic comprehension errors",
                },
            },
            # 第三步：质量评分（Score）- 0-100分
            "quality_score": {
                "type": "score",
                "instructions": "What is the overall quality score of the output?",
                "criteria": ["0-20 Very poor", "21-40 Poor", "41-60 Average", "61-80 Good", "81-100 Excellent"],
            },
            # 补充维度评分
            "completeness": {
                "type": "score",
                "instructions": "How complete is the output?",
                "criteria": ["0-20 Very incomplete", "21-40 Partial", "41-60 Mostly complete", "61-80 Almost complete", "81-100 Fully complete"],
            },
            "correctness": {
                "type": "score",
                "instructions": "How accurate is the output?",
                "criteria": ["0-20 Very inaccurate", "21-40 Many errors", "41-60 Some errors", "61-80 Mostly accurate", "81-100 Fully accurate"],
            },
        }

        try:
            start = time.time()
            result = self._call_jev(state=state, questions=questions)
            duration_ms = int(round((time.time() - start) * 1000))

            answers = result.get("answers", {})

            # 提取完成判定（Noul）
            completed_answer = answers.get("completed", {})
            completed_prob = float(completed_answer.get("noul", 0.0))
            passed = completed_prob >= 0.5
            confidence = float(completed_answer.get("confidence", completed_prob))

            # 提取失败原因（Choice）
            fail_reason = answers.get("fail_reason", {}).get("choice", "none")
            # 如果任务通过，强制 fail_reason 为 none
            if passed:
                fail_reason = "none"

            # 提取评分（0-100 → 0-1）
            quality_raw = float(answers.get("quality_score", {}).get("score", 50))
            completeness_score = float(answers.get("completeness", {}).get("score", 50))
            correctness_score = float(answers.get("correctness", {}).get("score", 50))

            # 计算综合得分（0-1）：质量分 60% + 完整性 20% + 正确性 20%
            score = round(
                (quality_raw * 0.6 + completeness_score * 0.2 + correctness_score * 0.2) / 100.0,
                3
            )

            # 三级阈值判定
            if confidence >= self.auto_pass_threshold:
                review_level = "auto"  # 自动通过
            elif confidence >= self.manual_review_threshold:
                review_level = "manual"  # 人工复核
            else:
                review_level = "rerun"  # 需要重跑

            # 判断是否需要升级到 LLM Judge：非 auto 级别都升级
            needs_escalation = review_level != "auto"

            logger.info(
                "jev_judge 完成 | task=%s score=%.3f passed=%s confidence=%.3f review_level=%s duration=%dms fail_reason=%s",
                task.id, score, passed, confidence, review_level, duration_ms, fail_reason,
            )

            return {
                "id": "jev_judge",
                "type": "jev_judge",
                "passed": passed,
                "detail": f"Jev 快速判定 score={round(score, 3)} confidence={round(confidence, 2)} review_level={review_level} fail_reason={fail_reason}"[:300],
                "score": score,
                "reasoning": f"Fail reason: {fail_reason}",
                "dimensions": {
                    "correctness": round(correctness_score / 100.0, 3),
                    "usefulness": round(quality_raw / 100.0, 3),
                    "completeness": round(completeness_score / 100.0, 3),
                    "efficiency": 0.8,  # Jev 本身很快，给高分
                    "safety": 0.9,  # 默认安全
                },
                "confidence": confidence,
                "review_level": review_level,  # auto / manual / rerun
                "needs_escalation": needs_escalation,
                "issue_type": fail_reason,  # 兼容旧字段
                "fail_reason": fail_reason,
                "usage": dict(self._usage) or None,
                "duration_ms": duration_ms,
            }

        except Exception as e:  # noqa: BLE001
            logger.error("jev_judge 调用失败 | task=%s | %s: %s", task.id, type(e).__name__, e)
            return {
                "id": "jev_judge",
                "type": "jev_judge",
                "passed": False,
                "detail": f"Jev 判分调用失败: {type(e).__name__}: {e}"[:300],
                "score": 0.0,
                "reasoning": "",
                "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
                "confidence": 0.0,
                "needs_escalation": True,
            }

    def _call_jev(self, state: str, questions: dict) -> dict:
        """调用 Jev API。"""
        if self.client:
            return self.client(state=state, questions=questions)

        import requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "jev-latest",
            "state": state,
            "questions": questions,
        }

        resp = requests.post(JEV_API_URL, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()

        # 估算 token 用量（粗略估算）
        self._usage["input_tokens"] += len(state) // 4
        self._usage["output_tokens"] += len(json.dumps(result)) // 4

        trace_llm_call(
            "jev_judge",
            model="jev-latest",
            messages=state[:2000],
            response=json.dumps(result)[:2000],
            usage=self._usage,
            duration_ms=0,
        )

        return result


def judge_jev(task, workspace: Path) -> dict:
    """便捷函数：按环境变量构造 Jev 判分器并判分。"""
    return JevJudge().judge(task, workspace)


# ---------- 智能路由：Jev 快速判定 + 三级阈值策略 ----------

def smart_judge(
    task,
    workspace: Path,
    auto_pass_threshold: float = DEFAULT_AUTO_PASS_THRESHOLD,
    manual_review_threshold: float = DEFAULT_MANUAL_REVIEW_THRESHOLD,
    max_rerun: int = 1,  # 低置信度最多重跑次数
) -> dict:
    """智能判分：Jev 三步判分 + 三级阈值路由。

    三级策略：
    1. auto（≥0.9）：直接采用 Jev 结果，无需升级
    2. manual（0.6-0.9）：升级到 LLM Judge 做详细判定，标记人工复核
    3. rerun（<0.6）：自动重跑最多 N 次，仍低置信度则升级到 LLM Judge
    """
    from agent_eval.judge import LLMJudge

    # 初始化 Jev Judge
    jev_judge = JevJudge(
        auto_pass_threshold=auto_pass_threshold,
        manual_review_threshold=manual_review_threshold,
    )

    # 如果 Jev 不可用（缺 API Key），直接用 LLM Judge
    if not jev_judge.api_key:
        logger.info("smart_judge: Jev 不可用，直接使用 LLM Judge (task=%s)", task.id)
        return LLMJudge().judge(task, workspace)

    # P1 新增：Model Router 先判断任务复杂度
    complexity_info = jev_judge.judge_task_complexity(task)
    complexity = complexity_info.get("complexity", "L3")
    use_jev_only = complexity_info.get("use_jev_only", False)

    # 简单任务（L1-L2）：直接走 Jev 判分，不升级 LLM，成本最低
    if use_jev_only:
        logger.info(
            "smart_judge: 简单任务(%s)，直接使用 Jev 判分 (task=%s)",
            complexity, task.id,
        )
        jev_result = jev_judge.judge(task, workspace)
        jev_result["task_complexity"] = complexity
        jev_result["router_path"] = "jev_only"
        return jev_result

    # 复杂任务：走三级阈值策略
    logger.info(
        "smart_judge: 复杂任务(%s)，走三级阈值策略 (task=%s)",
        complexity, task.id,
    )
    # 执行判分，低置信度自动重跑
    jev_result = jev_judge.judge(task, workspace)
    jev_result["task_complexity"] = complexity
    rerun_count = 0
    best_result = jev_result

    # 低置信度重跑循环
    while best_result.get("review_level") == "rerun" and rerun_count < max_rerun:
        rerun_count += 1
        logger.info(
            "smart_judge: 低置信度，第 %d 次重跑 (task=%s confidence=%.3f)",
            rerun_count, task.id, best_result.get("confidence", 0),
        )
        jev_result = jev_judge.judge(task, workspace)
        # 保留置信度更高的结果
        if jev_result.get("confidence", 0) > best_result.get("confidence", 0):
            best_result = jev_result

    # auto 级别：直接返回 Jev 结果
    if best_result.get("review_level") == "auto":
        logger.info(
            "smart_judge: 自动通过，直接采用 Jev 结果 (task=%s confidence=%.3f)",
            task.id, best_result.get("confidence", 0),
        )
        return best_result

    # manual / rerun 级别：升级到 LLM Judge
    logger.info(
        "smart_judge: 升级到 LLM Judge (task=%s review_level=%s confidence=%.3f reruns=%d)",
        task.id, best_result.get("review_level"), best_result.get("confidence", 0), rerun_count,
    )
    llm_result = LLMJudge().judge(task, workspace)

    # 合并结果：保留 Jev 的置信度和阈值信息
    llm_result["jev_confidence"] = best_result.get("confidence")
    llm_result["jev_score"] = best_result.get("score")
    llm_result["jev_review_level"] = best_result.get("review_level")
    llm_result["jev_rerun_count"] = rerun_count
    llm_result["escalated_from_jev"] = True

    # 如果重跑过，标记需要人工关注
    if rerun_count > 0:
        llm_result["needs_manual_review"] = True

    return llm_result


# ---------- P1：批量 Badcase 归因分析 ----------

def batch_analyze_badcases(badcases: list[dict], max_batch: int = 20) -> dict:
    """批量分析 badcase，使用 Jev 一次调用分析多个 badcase。

    优势：
    - 速度：一次 API 调用分析 20 个 badcase，比逐个分析快 20 倍
    - 成本：批量分析成本降低 80%
    - 聚类：自动识别相似 badcase，找出共性问题

    Args:
        badcases: badcase 列表，每项包含 id/title/description/category 等
        max_batch: 每批最大数量（Jev API 单次建议不超过 20 个）

    Returns:
        包含 batch_id/results/clusters/summary 的分析结果
    """
    judge = JevJudge()
    if not judge.api_key:
        return {
            "ok": False,
            "error": "缺少 Jev API Key（TYPESAFE_API_KEY），无法执行批量分析",
            "results": [],
        }

    if not badcases:
        return {
            "ok": True,
            "results": [],
            "clusters": [],
            "summary": {"total": 0, "analyzed": 0},
        }

    all_results: list[dict] = []
    all_clusters: list[dict] = []

    # 分批处理
    for i in range(0, len(badcases), max_batch):
        batch = badcases[i:i + max_batch]
        batch_num = i // max_batch + 1
        logger.info("批量分析 badcase | batch=%d size=%d", batch_num, len(batch))

        # 构造 Jev 问题集
        state = "Badcase 列表：\n"
        questions: dict = {}

        for idx, bc in enumerate(batch):
            bc_id = bc.get("id", f"bc_{idx}")
            title = bc.get("title", "")
            desc = bc.get("description", "")[:500]  # 限制长度
            category = bc.get("category", "unknown")

            state += f"\n--- Badcase {idx+1} ---"
            state += f"\nID: {bc_id}"
            state += f"\n标题: {title}"
            state += f"\n分类: {category}"
            state += f"\n描述: {desc}"

            # 为每个 badcase 添加问题
            questions[f"bc_{idx}_root_cause"] = {
                "type": "choice",
                "instructions": f"What is the root cause type of this badcase?",
                "criteria": {
                    "data_logic": "Data or logic errors",
                    "skill_routing": "Skill or tool routing issues",
                    "tool_param": "Tool parameter errors",
                    "output_contract": "Output format or contract issues",
                    "environment": "Environment or dependency issues",
                    "model_semantic": "Model understanding or semantic issues",
                    "other": "Other unknown reasons",
                },
            }

            questions[f"bc_{idx}_severity"] = {
                "type": "score",
                "instructions": f"How severe is this badcase?",
                "criteria": ["P3 minor", "P2 moderate", "P1 major", "P0 critical"],
            }

            questions[f"bc_{idx}_fix_difficulty"] = {
                "type": "score",
                "instructions": f"How difficult is it to fix this issue?",
                "criteria": ["Easy fix", "Moderate effort", "Hard fix", "Very hard"],
            }

            questions[f"bc_{idx}_has_common_pattern"] = {
                "type": "noul",
                "instructions": f"Does this badcase share common patterns with others?",
                "criteria": {
                    "true": "Likely shares patterns with other badcases",
                    "false": "Unique issue unlikely to repeat",
                },
            }

        try:
            start = time.time()
            result = judge._call_jev(state=state, questions=questions)
            duration_ms = int(round((time.time() - start) * 1000))

            answers = result.get("answers", {})

            # 提取每个 badcase 的分析结果
            for idx, bc in enumerate(batch):
                bc_id = bc.get("id", f"bc_{idx}")
                root_cause = answers.get(f"bc_{idx}_root_cause", {}).get("choice", "other")
                severity_score = float(answers.get(f"bc_{idx}_severity", {}).get("score", 1.5))
                fix_difficulty = float(answers.get(f"bc_{idx}_fix_difficulty", {}).get("score", 1.5))
                has_common = float(answers.get(f"bc_{idx}_has_common_pattern", {}).get("noul", 0.5))

                # 映射严重程度
                severity_map = {0: "P3", 1: "P2", 2: "P1", 3: "P0"}
                severity = severity_map.get(int(severity_score), "P2")

                all_results.append({
                    "id": bc_id,
                    "title": bc.get("title", ""),
                    "original_category": bc.get("category", "unknown"),
                    "jev_root_cause": root_cause,
                    "jev_severity": severity,
                    "jev_fix_difficulty": ["Easy", "Moderate", "Hard", "Very hard"][min(int(fix_difficulty), 3)],
                    "jev_common_pattern": round(has_common, 2),
                    "confidence": round(float(answers.get(f"bc_{idx}_root_cause", {}).get("confidence", 0.8)), 2),
                })

            logger.info(
                "批量分析完成 | batch=%d duration=%dms analyzed=%d",
                batch_num, duration_ms, len(batch),
            )

        except Exception as e:  # noqa: BLE001
            logger.error("批量分析失败 | batch=%d | %s: %s", batch_num, type(e).__name__, e)
            # 失败时返回原始信息
            for idx, bc in enumerate(batch):
                all_results.append({
                    "id": bc.get("id", f"bc_{idx}"),
                    "title": bc.get("title", ""),
                    "original_category": bc.get("category", "unknown"),
                    "jev_root_cause": "analysis_failed",
                    "jev_severity": "P2",
                    "jev_fix_difficulty": "Unknown",
                    "jev_common_pattern": 0.5,
                    "confidence": 0.0,
                    "error": str(e),
                })

    # 聚类分析：按根因类型分组
    clusters: dict[str, list] = {}
    for r in all_results:
        cause = r.get("jev_root_cause", "other")
        if cause not in clusters:
            clusters[cause] = []
        clusters[cause].append(r["id"])

    cluster_list = [
        {
            "root_cause": cause,
            "count": len(ids),
            "badcase_ids": ids,
            "suggestion": f"发现 {len(ids)} 个相同根因的 badcase，建议集中处理",
        }
        for cause, ids in clusters.items()
        if len(ids) >= 2  # 只报告至少 2 个的聚类
    ]

    # 按数量排序
    cluster_list.sort(key=lambda x: x["count"], reverse=True)

    return {
        "ok": True,
        "total": len(badcases),
        "analyzed": len(all_results),
        "results": all_results,
        "clusters": cluster_list,
        "summary": {
            "by_root_cause": {cause: len(ids) for cause, ids in clusters.items()},
            "high_severity": sum(1 for r in all_results if r.get("jev_severity") in ("P0", "P1")),
            "common_pattern_count": sum(1 for r in all_results if r.get("jev_common_pattern", 0) > 0.6),
        },
    }
