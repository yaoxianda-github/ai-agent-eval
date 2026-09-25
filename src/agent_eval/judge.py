"""LLM-as-a-Judge 语义判分器（V2.2）。

用于 verifier=llm_judge 的开放任务（如 T502 周报总结）：确定性校验点只验证
"结构在不在"，LLM 判分负责"质量好不好"——完整性、准确性、结构、语言。

设计原则：
- 无 API Key / 调用失败 / 无产物 → 降级为 failed verdict，绝不中断 run
- client 可注入（测试用 Fake），不依赖真实网络
- 输出 verdict 与确定性校验点同构（id/type/passed/detail），并附带 score/reasoning，
  供 Web 工作台与 CLI 展示
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agent_eval.log import get_logger
from agent_eval.observability import trace_llm_call

logger = get_logger(__name__)

# 默认评分标准；任务作者可在 spec.yaml 的 rubric 字段自定义
DEFAULT_RUBRIC = """请从以下四个方面判分（每项 0-25 分，共 100 分）：
1. 内容完整性：是否覆盖任务要求的所有关键信息；
2. 准确性：关键数据、事实是否准确，无明显编造；
3. 结构与格式：章节/条目组织清晰，符合任务要求的输出形态；
4. 语言质量：表达清楚、无错别字、无冗余。
评分参考：>=85 优秀，70-84 良好，60-69 及格，<60 不达标。"""


def _collect_artifacts(workspace: Path) -> str:
    """收集工作目录 output/ 下的全部文本产物（用于判分输入）。"""
    out_dir = workspace / "output"
    if not out_dir.is_dir():
        return ""
    parts: list[str] = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"--- {p.relative_to(workspace).as_posix()} ---\n{text[:8000]}")
    return "\n\n".join(parts)


def _parse_score(text: str) -> dict:
    """从 LLM 输出中提取第一个 JSON 对象（容忍前后文字）。"""
    if not text:
        raise ValueError("判分输出为空")
    start = text.find("{")
    if start == -1:
        raise ValueError("判分输出中未找到 JSON 对象")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("未找到闭合的 JSON 对象")


class LLMJudge:
    """语义判分器。client 可注入（测试），默认走 OpenAI 兼容接口。

    V4.3 P1-2：支持独立 judge 配置（JUDGE_MODEL / JUDGE_API_KEY / JUDGE_BASE_URL），
    避免与被测模型相同导致"自恋偏差"。judge 模型可以比被测模型便宜 10-20 倍。
    优先级：显式参数 > JUDGE_* 环境变量 > DEEPSEEK_* / LLM_* 环境变量。
    """

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str | None = None,
        base_url: str | None = None,
        client=None,
    ) -> None:
        # V4.3 P1-2：独立 judge 配置优先
        judge_model = os.environ.get("JUDGE_MODEL")
        judge_api_key = os.environ.get("JUDGE_API_KEY")
        judge_base_url = os.environ.get("JUDGE_BASE_URL")

        self.model = model if model != "deepseek-chat" else (judge_model or model)
        self.api_key = api_key or judge_api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "LLM_API_KEY"
        )
        self.client = client
        self._usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}
        if self.client is None and self.api_key:
            import openai  # 延迟导入

            self.client = openai.OpenAI(
                api_key=self.api_key,
                base_url=base_url
                or judge_base_url
                or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            )

    def judge(self, task, workspace: Path) -> dict:
        """对任务产物执行语义判分，返回 verdict（与确定性校验点同构）。"""
        if self.client is None:
            logger.warning("llm_judge 跳过: 缺少 LLM API Key (task=%s)", task.id)
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": False,
                "detail": "缺少 LLM API Key（DEEPSEEK_API_KEY），无法执行语义判分",
                "score": 0.0,
                "reasoning": "",
                "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
            }

        artifacts = _collect_artifacts(workspace)
        if not artifacts:
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": False,
                "detail": "未找到可判分的产物（output/ 目录为空）",
                "score": 0.0,
                "reasoning": "",
                "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
            }

        rubric = (task.rubric or DEFAULT_RUBRIC).strip()
        # V4.7 P1：参考解法注入（若有），作为判分的标准答案参照
        ref_block = ""
        ref = getattr(task, "reference_solution", "") or ""
        if ref.strip():
            ref_block = f"\n参考解法（标准答案，用于对比 Agent 产物是否达标）：\n{ref.strip()}\n"
        user_msg = (
            f"任务：{task.description}\n\n"
            f"评分标准：\n{rubric}\n\n"
            f"{ref_block}"
            f"Agent 产物：\n{artifacts}\n\n"
            '请仅输出一个 JSON 对象，包含以下字段：\n'
            '{"score": 0-100 总分, "passed": true/false, '
            '"dimensions": {"correctness": 0-100 事实正确性, "usefulness": 0-100 有用性, '
            '"completeness": 0-100 完整性, "efficiency": 0-100 效率, "safety": 0-100 安全性}, '
            '"reasoning": "判分理由"}\n'
            '重要（V4.7 P1）：如果产物证据不足、信息不完整，无法做出可信判断，'
            '必须返回 {"abstain": true, "reasoning": "无法判断的原因"}，'
            '禁止强迫猜测一个分数。只有证据充分时才给分。'
        )
        try:
            start = time.time()
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是 AI Agent 评测框架的语义判分员，严格按评分标准打分。",
                    },
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=500,
            )
            duration_ms = int(round((time.time() - start) * 1000))
            raw = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self._usage["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
                self._usage["completion_tokens"] += (
                    getattr(usage, "completion_tokens", 0) or 0
                )
            trace_llm_call(
                "judge",
                model=self.model,
                messages=user_msg[:2000],
                response=raw[:2000],
                usage=usage,
                duration_ms=duration_ms,
            )

            data = _parse_score(raw)
            # V4.7 P1：abstain（无法判断）——证据不足时不允许猜分，交由人工复核
            abstain = bool(data.get("abstain", False))
            score = max(0.0, min(1.0, float(data.get("score", 0)) / 100.0))
            passed = bool(data.get("passed", score >= 0.6))
            reasoning = str(data.get("reasoning", ""))[:300]
            # V3.7：多维度评分（correctness/usefulness/completeness/efficiency/safety）
            raw_dims = data.get("dimensions", {}) or {}
            dimensions = {}
            for dim_key in ("correctness", "usefulness", "completeness", "efficiency", "safety"):
                try:
                    dimensions[dim_key] = max(0.0, min(1.0, float(raw_dims.get(dim_key, 0)) / 100.0))
                except (ValueError, TypeError):
                    dimensions[dim_key] = 0.0
            logger.info(
                "llm_judge 完成 | task=%s score=%.3f passed=%s duration=%dms dims=%s",
                task.id, score, passed, duration_ms,
                {k: round(v, 2) for k, v in dimensions.items()},
            )
            if abstain:
                return {
                    "id": "judge",
                    "type": "llm_judge",
                    "passed": False,
                    "abstain": True,   # V4.7 P1：无法判断，需人工复核
                    "detail": f"无法判断（abstain）：{reasoning or '产物证据不足'}"[:300],
                    "score": 0.0,
                    "reasoning": reasoning,
                    "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
                    "usage": dict(self._usage) or None,
                }
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": passed,
                "detail": f"语义判分 score={round(score, 3)}：{reasoning}"[:300],
                "score": score,
                "reasoning": reasoning,
                "dimensions": dimensions,
                "usage": dict(self._usage) or None,
            }
        except Exception as e:  # noqa: BLE001 - 判分失败不应中断评测
            logger.error("llm_judge 调用失败 | task=%s | %s: %s", task.id, type(e).__name__, e)
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": False,
                "detail": f"判分调用失败: {type(e).__name__}: {e}"[:300],
                "score": 0.0,
                "reasoning": "",
                "dimensions": {"correctness": 0, "usefulness": 0, "completeness": 0, "efficiency": 0, "safety": 0},
            }


def judge_llm(task, workspace: Path) -> dict:
    """便捷函数：按环境变量构造判分器并判分。"""
    return LLMJudge().judge(task, workspace)


# ---------- V4.3 P1-1：LLM Judge 校准闭环 ----------

def calibrate_judge(
    labeled_path: str | Path,
    judge: LLMJudge | None = None,
    rubric: str | None = None,
) -> dict:
    """LLM Judge 校准闭环：用人工标注集计算一致率。

    文章核心：judge 不是写完 prompt 就能用的。50 条人工标注，一致率 <80% 改 rubric 重跑，
    >85% 才有资格参与自动化决策。

    人工标注集格式（JSON 文件）：
    [
      {"id": "case-001", "input": "用户问题", "output": "Agent 产物", "human_pass": true, "human_score": 85},
      ...
    ]

    返回：{"agreement_rate": float, "passed": bool, "threshold": 0.85,
           "total": int, "agree": int, "conflicts": [...], "recommendation": str}
    """
    labeled_path = Path(labeled_path)
    if not labeled_path.exists():
        raise FileNotFoundError(f"人工标注集不存在: {labeled_path}")

    with open(labeled_path, encoding="utf-8") as f:
        cases = json.load(f)

    if not isinstance(cases, list) or not cases:
        raise ValueError("人工标注集格式错误：应为非空 JSON 数组")

    judge = judge or LLMJudge()
    if judge.client is None:
        return {
            "agreement_rate": 0.0,
            "passed": False,
            "threshold": 0.85,
            "total": len(cases),
            "agree": 0,
            "conflicts": [],
            "recommendation": "缺少 LLM API Key（JUDGE_API_KEY 或 DEEPSEEK_API_KEY），无法执行校准",
        }

    agree = 0
    conflicts = []
    for i, case in enumerate(cases):
        cid = case.get("id", f"case-{i+1}")
        human_pass = bool(case.get("human_pass", case.get("pass", False)))
        human_score = case.get("human_score")

        # 构造临时 task 对象用于 judge
        import types
        tmp_task = types.SimpleNamespace(
            id=cid,
            description=case.get("input", case.get("description", "")),
            rubric=rubric or DEFAULT_RUBRIC,
        )

        # 构造临时 workspace（把 output 写入 output/ 目录）
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "output"
            out_dir.mkdir(exist_ok=True)
            (out_dir / "result.txt").write_text(
                str(case.get("output", case.get("agent_output", ""))), encoding="utf-8"
            )
            verdict = judge.judge(tmp_task, Path(tmpdir))

        judge_pass = verdict.get("passed", False)
        judge_score = verdict.get("score", 0.0)

        if judge_pass == human_pass:
            agree += 1
        else:
            conflicts.append({
                "id": cid,
                "human_pass": human_pass,
                "judge_pass": judge_pass,
                "human_score": human_score,
                "judge_score": round(judge_score, 3),
                "judge_reasoning": verdict.get("reasoning", "")[:200],
                "input_preview": str(case.get("input", ""))[:100],
            })

    agreement_rate = round(agree / len(cases), 3)
    passed = agreement_rate >= 0.85

    if agreement_rate < 0.80:
        recommendation = (
            f"一致率 {agreement_rate:.1%} < 80%，建议修改 rubric 后重跑。"
            f"重点检查 {len(conflicts)} 个冲突样本的判分理由。"
        )
    elif agreement_rate < 0.85:
        recommendation = (
            f"一致率 {agreement_rate:.1%} 在 80%-85% 之间，接近达标但未上岗。"
            f"建议优化 rubric 中模糊项后再校准。"
        )
    else:
        recommendation = (
            f"一致率 {agreement_rate:.1%} >= 85%，judge 可以上岗参与自动化决策。"
            f"建议定期（每月）用新增标注集复校。"
        )

    return {
        "agreement_rate": agreement_rate,
        "passed": passed,
        "threshold": 0.85,
        "total": len(cases),
        "agree": agree,
        "conflicts": conflicts,
        "recommendation": recommendation,
    }


# ---------- V4.3 P2-1：A/B Pairwise Judge + 换位测试 ----------

def pairwise_compare(
    task_description: str,
    output_a: str,
    output_b: str,
    label_a: str = "Agent A",
    label_b: str = "Agent B",
    judge: LLMJudge | None = None,
    rubric: str | None = None,
) -> dict:
    """A/B Pairwise 对比 + 换位测试。

    文章核心：分别打分有偏差，pairwise 对比更稳定；换位测试防止位置偏差。
    流程：A 在前 B 在后评一次 → B 在前 A 在后再评一次 → 取两次一致结果，
    不一致时标注为"存疑"需要人工复核。

    返回：{"winner": "A"/"B"/"tie"/"inconclusive", "agreement": bool,
           "round1": {"winner":..., "reasoning":...}, "round2": {...},
           "position_bias_risk": bool}
    """
    judge = judge or LLMJudge()
    if judge.client is None:
        return {"winner": "inconclusive", "agreement": False, "error": "缺少 LLM API Key"}

    rubric_text = (rubric or DEFAULT_RUBRIC).strip()

    def _one_round(first_label: str, first_output: str, second_label: str, second_output: str) -> dict:
        user_msg = (
            f"任务：{task_description}\n\n"
            f"评分标准：\n{rubric_text}\n\n"
            f"【{first_label} 的输出】\n{first_output[:4000]}\n\n"
            f"【{second_label} 的输出】\n{second_output[:4000]}\n\n"
            "请对比两个输出，仅输出一个 JSON 对象：\n"
            '{"winner": "first"/"second"/"tie", '
            '"reasoning": "选择理由（50字以内）", '
            '"score_first": 0-100, "score_second": 0-100}'
        )
        try:
            resp = judge.client.chat.completions.create(
                model=judge.model,
                messages=[
                    {"role": "system", "content": "你是严格的 AI Agent 输出对比评审员，只看输出质量不看标签。"},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=300,
            )
            raw = resp.choices[0].message.content or ""
            data = _parse_score(raw)
            return {
                "winner": data.get("winner", "tie"),
                "reasoning": str(data.get("reasoning", ""))[:200],
                "score_first": float(data.get("score_first", 0)),
                "score_second": float(data.get("score_second", 0)),
            }
        except Exception as e:  # noqa: BLE001
            return {"winner": "tie", "reasoning": f"judge 调用失败: {e}", "score_first": 0, "score_second": 0}

    # 第一轮：A 在前，B 在后
    r1 = _one_round(label_a, output_a, label_b, output_b)
    # 第二轮：B 在前，A 在后（换位）
    r2 = _one_round(label_b, output_b, label_a, output_a)

    # 映射回原始标签
    r1_winner = "A" if r1["winner"] == "first" else ("B" if r1["winner"] == "second" else "tie")
    r2_winner = "B" if r2["winner"] == "first" else ("A" if r2["winner"] == "second" else "tie")

    agreement = r1_winner == r2_winner
    position_bias_risk = not agreement and r1_winner != "tie" and r2_winner != "tie"

    if agreement:
        winner = r1_winner
    elif r1_winner == "tie" or r2_winner == "tie":
        winner = r1_winner if r1_winner != "tie" else r2_winner
    else:
        winner = "inconclusive"  # 两次结果矛盾，需人工复核

    return {
        "winner": winner,
        "agreement": agreement,
        "position_bias_risk": position_bias_risk,
        "round1": {"winner": r1_winner, "reasoning": r1["reasoning"], "score_a": r1["score_first"], "score_b": r1["score_second"]},
        "round2": {"winner": r2_winner, "reasoning": r2["reasoning"], "score_a": r2["score_second"], "score_b": r2["score_first"]},
        "avg_score_a": round((r1["score_first"] + r2["score_second"]) / 2, 1),
        "avg_score_b": round((r1["score_second"] + r2["score_first"]) / 2, 1),
    }
