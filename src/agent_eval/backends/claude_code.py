"""Claude Code 后端（V2.8）：调用 Anthropic Claude Code CLI 非交互模式执行评测任务。

Claude Code（https://docs.anthropic.com/en/docs/claude-code）是 Anthropic 官方
命令行 Agent。本后端通过其 headless 模式在评测工作目录内一次性执行任务：

    claude -p "<task.description>" --output-format json --model <model> --bare

关键设计：
- 工作目录 = 启动 claude 时所在目录（即评测 workspace）
- --bare：跳过 hooks/LSP/plugin/keychain/OAuth/CLAUDE.md，严格用 ANTHROPIC_API_KEY，
  评测隔离干净、不污染用户 ~/.claude
- --output-format json：单次结构化结果，含最终文本、token 用量、花费
- --permission-mode acceptEdits：非交互下自动接受文件编辑
- --allowedTools：工具白名单，默认禁 WebFetch 避免评测联网引入不确定性
- --max-budget-usd：单次花费上限，防烧钱
- 退出码 0 = 完成
- 轨迹：JSON 含 messages 时解析 tool_use/tool_result 为步骤级 steps 与回放
  traces；不含时降级为单步黑盒（同 aider 口径），不影响评测

依赖：npm install -g @anthropic-ai/claude-code（安装后命令为 claude）
API Key：环境变量 ANTHROPIC_API_KEY（--bare 模式严格只认这个，兼容 ANTHROPIC_AUTH_TOKEN）
模型定价：见 costing.DEFAULT_PRICING（claude-sonnet-4-5 / claude-opus-4-5）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.log import get_logger
from agent_eval.traces import tool_category

logger = get_logger(__name__)

_DEFAULT_CLAUDE_CANDIDATES = (
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
)
# 评测默认工具白名单：文件操作 + 命令 + 检索；禁 WebFetch/MCP 等外部依赖
_DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,MultiEdit,Bash,Glob,Grep,List"
# 单次评测花费上限（美元），防止异常循环烧钱；0 表示不限制
_DEFAULT_MAX_BUDGET_USD = 2.0


def _find_claude_cmd() -> str | None:
    """探测 claude 命令：环境变量 CLAUDE_CMD > PATH > 常见安装路径。"""
    env_cmd = os.environ.get("CLAUDE_CMD")
    if env_cmd:
        return env_cmd
    which = shutil.which("claude")
    if which:
        return which
    for cand in _DEFAULT_CLAUDE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _extract_json(stdout: str) -> dict:
    """从 claude --output-format json 输出中提取 JSON 对象（容忍前置非 JSON 文本）。"""
    idx = stdout.find("{")
    if idx < 0:
        return {}
    try:
        return json.loads(stdout[idx:])
    except (json.JSONDecodeError, ValueError):
        # 尝试找最后一个完整的 }（流式残留时）
        last = stdout.rfind("}")
        if last > idx:
            try:
                return json.loads(stdout[idx : last + 1])
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}


def _parse_usage(data: dict) -> dict | None:
    """从 JSON 结果提取 token 用量（兼容多种字段命名）。"""
    # 常见字段：tokensIn/tokensOut、input_tokens/output_tokens、usage.{...}、tokens.{...}
    candidates = [
        ("tokensIn", "tokensOut"),
        ("input_tokens", "output_tokens"),
        ("prompt_tokens", "completion_tokens"),
    ]
    usage = data.get("usage") or data.get("tokens") or {}
    if isinstance(usage, dict):
        for ik, ok in candidates:
            if usage.get(ik) is not None and usage.get(ok) is not None:
                return {
                    "prompt_tokens": int(usage[ik]),
                    "completion_tokens": int(usage[ok]),
                }
    for ik, ok in candidates:
        if data.get(ik) is not None and data.get(ok) is not None:
            return {"prompt_tokens": int(data[ik]), "completion_tokens": int(data[ok])}
    return None


def _parse_messages(data: dict) -> tuple[list[dict], list[dict]]:
    """若 JSON 含完整 messages，解析 tool_use/tool_result → (steps, traces)。

    兼容结构：data.messages / data.result.messages，消息类型 user/assistant/tool_result，
    assistant.content 含 text / thinking / tool_use 块。解析失败返回空。
    """
    messages = data.get("messages")
    if not messages and isinstance(data.get("result"), dict):
        messages = data["result"].get("messages")
    if not isinstance(messages, list) or not messages:
        return [], []

    steps: list[dict] = []
    traces: list[dict] = []
    model = data.get("model") or ""
    last_user = ""
    step_no = 0
    pending: dict[str, dict] = {}  # callId -> {name, input, step_idx}

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype == "user":
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    last_user = c.get("text", "")[:300]
        elif mtype == "assistant":
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text" and c.get("text", "").strip():
                    traces.append(
                        {"kind": "llm", "ts": 0, "model": model, "input": last_user,
                         "output": c["text"], "phase": "final"}
                    )
                elif ct == "thinking" and c.get("thinking", "").strip():
                    traces.append(
                        {"kind": "llm", "ts": 0, "model": model, "input": last_user,
                         "output": c["thinking"], "phase": "reasoning"}
                    )
                elif ct == "tool_use":
                    step_no += 1
                    name = c.get("name") or "tool"
                    inp = c.get("input") or {}
                    cid = c.get("id") or ""
                    pending[cid] = {"name": name, "input": inp, "step_idx": len(steps)}
                    steps.append(
                        {"step": step_no, "action": name, "args": inp,
                         "observation": "", "ts": 0}
                    )
                    traces.append(
                        {"kind": "llm", "ts": 0, "model": model, "input": last_user,
                         "output": json.dumps(inp, ensure_ascii=False)[:500],
                         "tool": name, "phase": "decision"}
                    )
        elif mtype == "tool_result":
            cid = msg.get("tool_use_id") or ""
            p = pending.get(cid, {})
            name = p.get("name") or "tool"
            obs = ""
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    obs += c.get("text", "")
            if p.get("step_idx") is not None:
                steps[p["step_idx"]]["observation"] = obs[:2000]
            traces.append(
                {"kind": "tool", "category": tool_category(name), "ts": 0,
                 "tool": name, "args": p.get("input") or {}, "observation": obs[:2000]}
            )
    return steps, traces


class ClaudeCodeBackend(Backend):
    name = "claude-code"
    version = "0.1.0"

    def __init__(
        self,
        model: str = "claude-sonnet-4-5",
        api_key: str | None = None,
        timeout_s: int = 300,
        cmd: str | None = None,
        allowed_tools: str | None = None,
        max_budget_usd: float = _DEFAULT_MAX_BUDGET_USD,
    ) -> None:
        self.model = model
        # --bare 模式严格只认 ANTHROPIC_API_KEY；兼容 ANTHROPIC_AUTH_TOKEN 兜底
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
            "ANTHROPIC_AUTH_TOKEN"
        )
        self.timeout_s = timeout_s
        self.cmd = cmd or _find_claude_cmd()
        self.allowed_tools = allowed_tools or _DEFAULT_ALLOWED_TOOLS
        self.max_budget_usd = max_budget_usd

    def run(self, task, workspace) -> BackendResult:
        if not self.cmd:
            return BackendResult(
                status="error",
                error='未找到 claude 命令，请先：npm install -g @anthropic-ai/claude-code（或设置 CLAUDE_CMD）',
            )
        if not self.api_key:
            return BackendResult(
                status="error",
                error="缺少 ANTHROPIC_API_KEY（--bare 模式严格只认环境变量，不读 keychain/OAuth）",
            )

        cmd = [
            self.cmd,
            "-p", task.description,
            "--output-format", "json",
            "--model", self.model,
            "--permission-mode", "acceptEdits",
            "--allowedTools", self.allowed_tools,
            "--bare",
        ]
        if self.max_budget_usd and self.max_budget_usd > 0:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]

        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = self.api_key
        env.setdefault("CLAUDE_CODE_TELEMETRY_DISABLED", "1")
        # 隔离配置目录，避免污染用户 ~/.claude（历史/skills/配置）
        claude_home = Path(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        ) / "agent-eval" / "claude-home"
        claude_home.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(claude_home)

        logger.info(
            "claude-code 执行开始 | model=%s timeout=%ds budget=$%s",
            self.model, self.timeout_s, self.max_budget_usd,
        )
        start = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            stdin=subprocess.DEVNULL,
        )
        try:
            out, err = proc.communicate(timeout=self.timeout_s)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.warning("claude-code 超时 (>%ds)，已 kill", self.timeout_s)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                out, err = "", ""
            partial = ((out or "") + (("\n" + err) if err else ""))[-2000:]
            return BackendResult(
                status="timeout",
                steps=[
                    {"step": 1, "action": "claude", "args": {"model": self.model},
                     "observation": partial, "ts": round(time.time(), 3)}
                ],
                duration_s=round(time.time() - start, 3),
                error=f"claude 超时（>{self.timeout_s}s）。输出尾部：{partial[:400]}",
            )

        full = (out or "") + (("\n" + err) if err else "")
        data = _extract_json(out or "")
        steps, traces = _parse_messages(data)
        usage = _parse_usage(data)
        final = (
            data.get("message")
            or data.get("text")
            or data.get("final_text")
            or (isinstance(data.get("result"), dict) and data["result"].get("message"))
            or ""
        )
        if not steps:
            # JSON 无完整对话时降级为单步黑盒（同 aider 口径）
            steps = [
                {"step": 1, "action": "claude", "args": {"model": self.model},
                 "observation": full[-3000:], "ts": round(time.time(), 3)}
            ]
        if not traces and final:
            traces = [
                {"kind": "llm", "ts": 0, "model": self.model, "input": task.description[:300],
                 "output": str(final)[:2000], "phase": "final"}
            ]

        ok = rc == 0 and bool(data or (out or "").strip())
        cost_usd = data.get("cost")
        logger.info(
            "claude-code 完成 | status=%s rc=%d dur=%.1fs steps=%d traces=%d usage=%s cost=$%s",
            "completed" if ok else "error", rc, time.time() - start,
            len(steps), len(traces), usage, cost_usd,
        )
        if not ok:
            logger.warning("claude-code 未正常完成: 退出码 %d，stderr 尾部: %s", rc, (err or "")[-300:])
        return BackendResult(
            status="completed" if ok else "error",
            steps=steps,
            traces=traces,
            usage=usage,
            duration_s=round(time.time() - start, 3),
            stdout=str(final) if final else (out or "")[-1000:],
            error="" if ok else f"claude 退出码 {rc}：{(err or out or '')[-400:]}",
        )
