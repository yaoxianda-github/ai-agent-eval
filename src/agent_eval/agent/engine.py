"""评测 Agent ReAct 循环引擎。

实现标准 ReAct 循环：
Thinking → Action → Action Input → Observation → (循环) → Final Answer

使用 LLM 做 reasoning，调用 tools.py 中的工具。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from agent_eval.log import get_logger
from agent_eval.agent.tools import TOOLS_SCHEMA, call_tool

logger = get_logger(__name__)


SYSTEM_PROMPT = """你是一个 AI Agent 评测平台的智能助手。你可以通过调用工具来帮用户完成评测任务。

## 你的能力（工具）
你有以下工具可以调用：
{tools_desc}

## 工作流程
1. 理解用户的评测需求（要测什么 Agent？哪些任务？对比？）
2. 如果不确定有哪些任务/Agent，先调用 list_tasks / list_backends 查询
3. 决定执行策略（选哪些任务、哪些 Agent、跑几次）
4. 调用 run_batch 发起评测
5. 用 get_batch_status 轮询直到完成
6. 分析结果，给出结构化结论

## 输出格式
每次回复必须是一个 JSON 对象：
{{
  "thought": "你现在的思考（为什么要调这个工具）",
  "action": "要调用的工具名",
  "action_input": {{...}},
  "observation": null,
  "final_answer": null
}}

当任务完成时，把 action 设为 "final"，final_answer 写你的结论：
{{
  "thought": "任务已完成，总结结果",
  "action": "final",
  "action_input": null,
  "observation": null,
  "final_answer": "## 评测结论\\n- 通过率：...\\n- 关键发现：..."
}}

## 重要规则
- 不要编造数据，所有结论必须基于工具返回的 observation
- 批量评测时，runs 默认 1，用户要求"多次/消除波动"时 runs=3
- 对比多个 Agent 时，统一任务集和 runs 数
"""


def _build_tools_desc() -> str:
    """把工具 schema 转成自然语言描述。"""
    lines = []
    for t in TOOLS_SCHEMA:
        lines.append(f"- {t['name']}: {t['description']}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象。"""
    # 尝试找 ```json ... ``` 块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试找裸 JSON
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return None


class EvalAgent:
    """评测 Agent ReAct 引擎。"""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_iterations: int = 10,
        poll_interval_s: float = 2.0,
    ) -> None:
        self.model = model or os.environ.get("EVAL_AGENT_MODEL", "deepseek-chat")
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self.base_url = base_url or os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
        self.max_iterations = max_iterations
        self.poll_interval_s = poll_interval_s
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    def _reason(self, messages: list[dict]) -> dict:
        """调用 LLM 做一次 reasoning，返回结构化决策。"""
        client = self._get_client()
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            max_tokens=800,
        )
        raw = resp.choices[0].message.content or ""
        logger.info("EvalAgent reasoning: %s", raw[:200])

        decision = _extract_json(raw)
        if decision is None:
            # 如果解析失败，把原始文本当 final answer
            return {
                "thought": raw,
                "action": "final",
                "action_input": None,
                "observation": None,
                "final_answer": raw,
            }
        return decision

    def run(self, user_query: str) -> dict:
        """执行完整的 ReAct 循环，返回最终结果和执行轨迹。"""
        system = SYSTEM_PROMPT.format(tools_desc=_build_tools_desc())
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_query},
        ]

        trajectory: list[dict] = []
        final_answer = ""

        for i in range(self.max_iterations):
            logger.info("EvalAgent 第 %d 轮", i + 1)

            decision = self._reason(messages)
            thought = decision.get("thought", "")
            action = decision.get("action", "")
            action_input = decision.get("action_input") or {}

            step = {
                "iteration": i + 1,
                "thought": thought,
                "action": action,
                "action_input": action_input,
            }

            # 终止条件：final
            if action == "final":
                final_answer = decision.get("final_answer", thought)
                step["final"] = True
                trajectory.append(step)
                break

            # 执行工具（做小写和去空格处理，兼容 LLM 输出的细微差异）
            normalized_action = action.lower().replace(" ", "_") if action else ""
            tool_names = [t["name"] for t in TOOLS_SCHEMA]
            if normalized_action and normalized_action in tool_names:
                result = call_tool(normalized_action, action_input)
                step["observation"] = result

                # 如果是查批次状态，等待轮询
                if action == "get_batch_status" and result.get("status") == "running":
                    logger.info("批次运行中，等待 %.1fs...", self.poll_interval_s)
                    time.sleep(self.poll_interval_s)
                elif action == "run_batch" and result.get("batch_id"):
                    # 刚创建批次，立即查一次状态
                    time.sleep(0.5)

                # 把 observation 加回 messages
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })
                messages.append({
                    "role": "user",
                    "content": f"工具 {action} 返回结果：{json.dumps(result, ensure_ascii=False)}",
                })
            else:
                # 未知 action，提示 LLM 修正
                step["observation"] = {"error": f"未知工具: {action}"}
                messages.append({
                    "role": "assistant",
                    "content": json.dumps(decision, ensure_ascii=False),
                })
                messages.append({
                    "role": "user",
                    "content": f"工具 {action} 不存在。可用工具：{tool_names}",
                })

            trajectory.append(step)

        return {
            "query": user_query,
            "final_answer": final_answer,
            "iterations": len(trajectory),
            "trajectory": trajectory,
        }
