"""自建最小 ReAct Agent（Day 2，Day 3 加固）。

循环：system 提示 → LLM 输出 action → 执行工具 → 观察 → 重复，直到 finish 或步数耗尽。

健壮性（Day 3）：
- _extract_json：从模型输出中提取第一个合法 JSON 对象，容忍前导/尾随文字
- 单步解析失败自动重试（带纠正提示），重试耗尽才中断，状态记为 error

模型接入：OpenAI 兼容接口（默认 DeepSeek API），可用环境变量覆盖：
- DEEPSEEK_API_KEY / LLM_API_KEY：API Key
- LLM_BASE_URL：接口地址，默认 https://api.deepseek.com
"""

from __future__ import annotations

import json
import os
import time

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.tools import run_tool

SYSTEM_PROMPT = """你是一个在沙箱工作目录里执行任务的自主 Agent。
每执行一步，输出且只输出一个 JSON 对象，不要输出任何其他文字：
{"tool": "<工具名>", "args": {...}}

可用工具：
- list_dir:  args {"path": "相对路径"} —— 列出目录内容
- read_file: args {"path": "相对路径"} —— 读取文件内容
- write_file: args {"path": "相对路径", "content": "内容"} —— 写文件
- run_command: args {"command": "shell 命令", "timeout": 30} —— 运行命令
- finish: args {"summary": "任务完成说明"} —— 任务完成，结束循环

路径均为相对工作目录。完成后必须调用 finish。"""


def _extract_json(text: str) -> dict:
    """从模型输出中提取第一个 JSON 对象并解析（容忍前后文字）。"""
    if not text:
        raise ValueError("模型输出为空")
    start = text.find("{")
    if start == -1:
        raise ValueError("输出中未找到 JSON 对象")
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


class MinimalReactBackend(Backend):
    name = "minimal-react"
    version = "0.2.0"

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str | None = None,
        base_url: str | None = None,
        max_steps: int = 20,
        max_parse_retries: int = 2,
        temperature: float = 0.0,
    ) -> None:
        import openai  # 延迟导入，避免无 key 环境无法 import 本模块

        self.model = model
        self.max_steps = max_steps
        self.max_parse_retries = max_parse_retries
        self.temperature = temperature
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "LLM_API_KEY"
        )
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=base_url or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
        )

    def _ask(self, messages: list[dict]) -> tuple[dict | None, str]:
        """调用 LLM 并解析 action；单步失败自动重试。返回 (action, raw)；action 为 None 表示重试耗尽。"""
        for attempt in range(self.max_parse_retries + 1):
            raw = ""
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=600,
                )
                raw = resp.choices[0].message.content or ""
                action = _extract_json(raw)
                return action, raw
            except Exception as e:  # noqa: BLE001 - 需区分可恢复/不可恢复
                if attempt < self.max_parse_retries:
                    messages.append({"role": "assistant", "content": raw or ""})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"上一步输出无法解析为 JSON（{type(e).__name__}: {e}）。"
                                "请只输出一个 JSON 对象，不要包含任何其他文字。"
                            ),
                        }
                    )
                else:
                    return None, f"LLM 输出解析失败: {type(e).__name__}: {e}"
        return None, "LLM 输出解析失败（未知错误）"

    def run(self, task, workspace) -> BackendResult:
        if not self.api_key:
            return BackendResult(
                status="error",
                steps=[],
                error="缺少 LLM API Key，请设置环境变量 DEEPSEEK_API_KEY",
            )

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"任务：{task.description}\n"
                    "请在工作目录内完成任务，所有工具路径均使用相对路径。"
                ),
            },
        ]
        steps: list[dict] = []
        start = time.time()

        for i in range(1, self.max_steps + 1):
            action, raw = self._ask(messages)
            if action is None:
                steps.append(
                    {"step": i, "action": "llm_error", "args": {}, "observation": raw}
                )
                return BackendResult(
                    status="error",
                    steps=steps,
                    duration_s=round(time.time() - start, 3),
                    error=raw,
                )

            tool = action.get("tool")
            args = action.get("args") or {}
            ts = round(time.time(), 3)
            ok, observation = run_tool(tool, args, workspace)
            steps.append(
                {"step": i, "action": tool, "args": args, "observation": observation, "ts": ts}
            )

            if tool == "finish":
                return BackendResult(
                    status="completed", steps=steps, duration_s=round(time.time() - start, 3)
                )

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"观察：{observation}"})

        return BackendResult(
            status="max_steps",
            steps=steps,
            duration_s=round(time.time() - start, 3),
            error=f"达到最大步数 {self.max_steps}",
        )
