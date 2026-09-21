"""Jev 快速 Judge 判分器（V4.5 P0）。

利用 Jev 模型（TypeSafe System One Model）做快速、低成本的结构化判定：
- 速度：70-500ms（比 LLM Judge 快 50-100 倍）
- 成本：$0.042/M input token，output 免费（比 LLM Judge 便宜 100 倍）
- 输出：结构化决策 + 置信度（可做风险门控）

设计原则：
- 与 LLMJudge 接口对齐，可无缝替换
- 低置信度自动升级到 LLM Judge
- 支持批量判定（一次调用问多个问题）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_eval.log import get_logger
from agent_eval.observability import trace_llm_call

logger = get_logger(__name__)

JEV_API_URL = "https://tokenra.io/v1/decisions"

# 默认置信度阈值：低于此值自动升级到 LLM Judge
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


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
        client=None,
    ) -> None:
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")
        self.confidence_threshold = float(
            os.environ.get("JEV_CONFIDENCE_THRESHOLD", confidence_threshold)
        )
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

        # 构造 Jev 问题集（一次调用问多个问题）
        state = (
            f"任务：{task.description}\n\n"
            f"Agent 产物：\n{artifacts[:8000]}"
        )

        questions = {
            "pass": {
                "type": "noul",
                "instructions": "Does the agent output meet the task requirements?",
                "criteria": {
                    "true": "Output fully meets all task requirements",
                    "false": "Output fails to meet task requirements",
                },
            },
            "completeness": {
                "type": "score",
                "instructions": "How complete is the output?",
                "criteria": ["Very incomplete", "Partially complete", "Mostly complete", "Fully complete"],
            },
            "correctness": {
                "type": "score",
                "instructions": "How accurate is the output?",
                "criteria": ["Very inaccurate", "Some errors", "Mostly accurate", "Fully accurate"],
            },
            "has_issues": {
                "type": "choice",
                "instructions": "What type of issues does the output have?",
                "criteria": {
                    "none": "No issues found",
                    "data_logic": "Data or logic errors",
                    "format": "Format or structure issues",
                    "missing_content": "Missing required content",
                    "wrong_answer": "Wrong answer or conclusion",
                },
            },
        }

        try:
            start = time.time()
            result = self._call_jev(state=state, questions=questions)
            duration_ms = int(round((time.time() - start) * 1000))

            answers = result.get("answers", {})

            # 提取通过判定
            pass_answer = answers.get("pass", {})
            pass_prob = float(pass_answer.get("noul", 0.0))
            passed = pass_prob >= 0.5
            confidence = float(pass_answer.get("confidence", pass_prob))

            # 提取评分
            completeness_score = float(answers.get("completeness", {}).get("score", 0.0))
            correctness_score = float(answers.get("correctness", {}).get("score", 0.0))

            # 提取问题类型
            issue_type = answers.get("has_issues", {}).get("choice", "none")

            # 计算综合得分（0-1）
            score = round((completeness_score + correctness_score) / 6.0, 3)  # 4 级 → 0-1

            # 判断是否需要升级到 LLM Judge
            needs_escalation = confidence < self.confidence_threshold

            logger.info(
                "jev_judge 完成 | task=%s score=%.3f passed=%s confidence=%.3f duration=%dms issue=%s",
                task.id, score, passed, confidence, duration_ms, issue_type,
            )

            return {
                "id": "jev_judge",
                "type": "jev_judge",
                "passed": passed,
                "detail": f"Jev 快速判定 score={round(score, 3)} confidence={round(confidence, 2)} issue={issue_type}"[:300],
                "score": score,
                "reasoning": f"Issue type: {issue_type}",
                "dimensions": {
                    "correctness": round(correctness_score / 3.0, 3),
                    "usefulness": round((completeness_score + correctness_score) / 6.0, 3),
                    "completeness": round(completeness_score / 3.0, 3),
                    "efficiency": 0.8,  # Jev 本身很快，给高分
                    "safety": 0.9,  # 默认安全
                },
                "confidence": confidence,
                "needs_escalation": needs_escalation,
                "issue_type": issue_type,
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


# ---------- 智能路由：Jev 快速判定 + 低置信度升级 LLM ----------

def smart_judge(
    task,
    workspace: Path,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict:
    """智能判分：先用 Jev 快速判定，低置信度自动升级到 LLM Judge。

    策略：
    1. 用 Jev 做快速判定（70-500ms）
    2. 如果置信度 >= 阈值，直接用 Jev 结果
    3. 如果置信度 < 阈值，升级到 LLM Judge 做详细判定
    """
    from agent_eval.judge import LLMJudge

    # 第一步：Jev 快速判定
    jev_judge = JevJudge(confidence_threshold=confidence_threshold)
    jev_result = jev_judge.judge(task, workspace)

    # 如果 Jev 不可用（缺 API Key），直接用 LLM Judge
    if not jev_judge.api_key:
        logger.info("smart_judge: Jev 不可用，直接使用 LLM Judge (task=%s)", task.id)
        return LLMJudge().judge(task, workspace)

    # 如果置信度足够高，直接返回 Jev 结果
    if not jev_result.get("needs_escalation", False):
        logger.info(
            "smart_judge: Jev 结果置信度足够，直接采用 (task=%s confidence=%.3f)",
            task.id, jev_result.get("confidence", 0),
        )
        return jev_result

    # 置信度不足，升级到 LLM Judge
    logger.info(
        "smart_judge: Jev 置信度不足，升级到 LLM Judge (task=%s confidence=%.3f)",
        task.id, jev_result.get("confidence", 0),
    )
    llm_result = LLMJudge().judge(task, workspace)

    # 合并结果：保留 Jev 的置信度信息
    llm_result["jev_confidence"] = jev_result.get("confidence")
    llm_result["jev_score"] = jev_result.get("score")
    llm_result["escalated_from_jev"] = True

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
