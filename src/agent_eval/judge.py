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

from agent_eval.observability import trace_llm_call

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
    """语义判分器。client 可注入（测试），默认走 OpenAI 兼容接口（DeepSeek）。"""

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str | None = None,
        base_url: str | None = None,
        client=None,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "LLM_API_KEY"
        )
        self.client = client
        if self.client is None and self.api_key:
            import openai  # 延迟导入

            self.client = openai.OpenAI(
                api_key=self.api_key,
                base_url=base_url
                or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            )

    def judge(self, task, workspace: Path) -> dict:
        """对任务产物执行语义判分，返回 verdict（与确定性校验点同构）。"""
        if self.client is None:
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": False,
                "detail": "缺少 LLM API Key（DEEPSEEK_API_KEY），无法执行语义判分",
                "score": 0.0,
                "reasoning": "",
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
            }

        rubric = (task.rubric or DEFAULT_RUBRIC).strip()
        user_msg = (
            f"任务：{task.description}\n\n"
            f"评分标准：\n{rubric}\n\n"
            f"Agent 产物：\n{artifacts}\n\n"
            '请仅输出一个 JSON 对象：{"score": 0-100, "passed": true/false, "reasoning": "判分理由"}'
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
            trace_llm_call(
                "judge",
                model=self.model,
                messages=user_msg[:2000],
                response=raw[:2000],
                usage=usage,
                duration_ms=duration_ms,
            )

            data = _parse_score(raw)
            score = max(0.0, min(1.0, float(data.get("score", 0)) / 100.0))
            passed = bool(data.get("passed", score >= 0.6))
            reasoning = str(data.get("reasoning", ""))[:300]
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": passed,
                "detail": f"语义判分 score={round(score, 3)}：{reasoning}"[:300],
                "score": score,
                "reasoning": reasoning,
            }
        except Exception as e:  # noqa: BLE001 - 判分失败不应中断评测
            return {
                "id": "judge",
                "type": "llm_judge",
                "passed": False,
                "detail": f"判分调用失败: {type(e).__name__}: {e}"[:300],
                "score": 0.0,
                "reasoning": "",
            }


def judge_llm(task, workspace: Path) -> dict:
    """便捷函数：按环境变量构造判分器并判分。"""
    return LLMJudge().judge(task, workspace)
