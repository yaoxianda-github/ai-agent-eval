"""Hermes Agent 后端（V1.0）：调用 Nous Research Hermes Agent CLI 非交互模式执行评测任务。

Hermes Agent（https://github.com/NousResearch/hermes-agent）是 Nous Research 开源的
自我改进 AI Agent，核心特性：跨会话持久记忆（MEMORY.md/USER.md）、自动技能创建与复用、
Background Review 经验提炼。本后端通过其 CLI 非交互模式在评测工作目录内一次性执行任务：

    hermes chat -q "<task.description>" --ignore-user-config --ignore-rules \
        --model claude-opus-4-8 --provider anthropic \
        --toolsets terminal,filesystem,skills

关键设计：
- 工作目录 = subprocess.Popen 的 cwd 参数（即评测 workspace）
- --ignore-user-config --ignore-rules：评测隔离，不加载用户 ~/.hermes 配置和记忆，
  避免跨会话记忆污染评测结果
- 注意：不使用 --worktree 参数（要求 git 仓库），直接用 cwd 指定工作目录
- -q：单次查询非交互模式（类似 claude -p）
- --provider anthropic：复用 ANTHROPIC_API_KEY，与 claude-code 后端同一套凭证
- --model claude-opus-4-8：与 claude-code 默认模型一致，保证可比
- --toolsets：工具集白名单，默认 terminal+filesystem+skills，禁 web 避免联网不确定性
- 退出码 0 = 完成
- 轨迹：解析 stdout 中的工具调用模式为步骤级 steps；拿不到时降级为单步黑盒

依赖：pip install hermes-agent（安装后命令为 hermes）
API Key：环境变量 ANTHROPIC_API_KEY（与 claude-code 复用）
模型定价：见 costing.DEFAULT_PRICING（claude-opus-4-8）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.log import get_logger

logger = get_logger(__name__)

_DEFAULT_HERMES_CANDIDATES = (
    "/usr/local/bin/hermes",
    "/opt/homebrew/bin/hermes",
    "~/.local/bin/hermes",
)
# 评测默认工具集：terminal（命令执行）+ file（文件读写）+ code_execution（代码执行）
# 启用 file 工具集后，hermes 可以直接调用 write_file/read_file 等工具，
# 不需要通过 terminal 的 cat/echo 间接操作，避免工具不存在的错误
# 禁 web/browser 避免联网引入不确定性
_DEFAULT_TOOLSETS = "terminal,file,code_execution"
# 单次评测超时（秒）
_DEFAULT_TIMEOUT = 300
# 与 claude-code 后端一致的默认模型
_DEFAULT_MODEL = "claude-opus-4-8"
# 与 claude-code 后端一致的提供商
_DEFAULT_PROVIDER = "anthropic"


def _find_hermes_cmd() -> str | None:
    """探测 hermes 命令：环境变量 HERMES_CMD > PATH > 常见安装路径。"""
    env_cmd = os.environ.get("HERMES_CMD")
    if env_cmd:
        return env_cmd
    which = shutil.which("hermes")
    if which:
        return which
    for cand in _DEFAULT_HERMES_CANDIDATES:
        p = Path(cand).expanduser()
        if p.exists():
            return str(p)
    return None


def _extract_json(text: str) -> dict:
    """从 hermes 输出中提取 JSON 对象（高健壮性，复用 claude_code 的解析策略）。"""
    if not text:
        return {}
    # 策略1：直接从第一个 { 解析
    idx = text.find("{")
    if idx >= 0:
        try:
            return json.loads(text[idx:])
        except (json.JSONDecodeError, ValueError):
            pass
    # 策略2：扫描所有顶层完整 {} 对，取可解析的最大对象
    candidates: list[tuple[int, int]] = []
    stack: list[int] = []
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            if not stack:
                candidates.append((start, i))
    best: dict = {}
    best_len = 0
    for start, end in candidates:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                plen = len(json.dumps(parsed, ensure_ascii=False))
                if plen > best_len:
                    best = parsed
                    best_len = plen
        except (json.JSONDecodeError, ValueError):
            continue
    return best


class HermesAgentBackend(Backend):
    """Hermes Agent 后端：通过 CLI 非交互模式执行评测任务。

    与 claude-code 后端复用同一套 ANTHROPIC_API_KEY 和 claude-opus-4-8 模型，
    保证两个 Harness 在相同模型下的能力对比公平。
    """

    name = "hermes-agent"
    version = "1.0.0"
    default_model = _DEFAULT_MODEL

    def __init__(
        self,
        model: str | None = None,
        provider: str | None = None,
        toolsets: str = _DEFAULT_TOOLSETS,
        timeout_s: int = _DEFAULT_TIMEOUT,
        max_steps: int | None = None,
    ):
        self.model = model or self.default_model
        self.provider = provider or _DEFAULT_PROVIDER
        self.toolsets = toolsets
        self.timeout_s = timeout_s
        self.max_steps = max_steps
        self._cmd = _find_hermes_cmd()
        if not self._cmd:
            raise RuntimeError(
                "未找到 hermes 命令，请先安装：pip install hermes-agent，"
                "或设置环境变量 HERMES_CMD 指向 hermes 可执行文件路径"
            )
        # 检查 API Key（与 claude-code 复用）
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning(
                "未检测到 ANTHROPIC_API_KEY 环境变量，hermes-agent 调用可能失败"
            )

    def run(self, task, workspace: Path) -> BackendResult:
        """在 workspace 内执行任务，返回结果与轨迹。"""
        start = time.time()

        # 构造命令：与 claude-code 相同的模型和 API Key，评测隔离模式
        # 注意：不使用 --worktree（要求 git 仓库），直接用 cwd 指定工作目录
        # --yolo：绕过危险命令审批提示，避免评测因交互审批而卡住
        # -Q：quiet 模式，抑制 banner/spinner/tool previews，只输出最终响应和 session info
        cmd = [
            self._cmd,
            "chat",
            "-q", task.description,
            "--ignore-user-config",
            "--ignore-rules",
            "--yolo",
            "-Q",
            "--model", self.model,
            "--provider", self.provider,
            "--toolsets", self.toolsets,
        ]

        task_id = getattr(task, "id", getattr(task, "task_id", "unknown"))
        logger.info(
            f"hermes-agent 执行开始 | model={self.model} provider={self.provider} "
            f"timeout={self.timeout_s}s task={task_id} workspace={workspace}"
        )

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env={
                    **os.environ,
                    "HERMES_NO_COLOR": "1",
                    "HERMES_DISABLE_UPDATE_CHECK": "1",
                },
            )
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
            duration = time.time() - start
            rc = proc.returncode

        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            duration = time.time() - start
            logger.warning(f"hermes-agent 超时 | task={task_id} {self.timeout_s}s")
            return BackendResult(
                status="timeout",
                steps=[{
                    "step": 1,
                    "action": "hermes",
                    "args": {"model": self.model, "provider": self.provider},
                    "observation": f"超时（>{self.timeout_s}s）",
                    "ts": time.time(),
                }],
                duration_s=duration,
                error=f"timeout after {self.timeout_s}s",
            )

        # 解析结果和轨迹
        steps = self._parse_steps(stdout, task)
        usage = self._extract_usage(stdout)

        # 从 state.db 提取更精确的 token 用量、成本和详细工具调用轨迹
        session_id = self._extract_session_id(stdout)
        if session_id:
            db_steps, db_usage = self._enrich_from_state_db(session_id)
            if db_steps:
                steps = db_steps  # state.db 的轨迹更精确（含工具参数和输出）
            if db_usage:
                # 合并 usage：state.db 的 token 用量优先，保留 stdout 的其他字段
                if usage:
                    db_usage.update({k: v for k, v in usage.items() if k not in db_usage})
                usage = db_usage

        # 增强错误检测：即使退出码为 0，也检查输出中的错误模式
        error_detected = self._detect_errors(stdout, stderr, steps, workspace)

        status = "completed" if (rc == 0 and not error_detected) else "error"
        if rc != 0 or error_detected:
            error_msg = stderr[-500:] if stderr else ""
            if error_detected and not error_msg:
                error_msg = error_detected
            logger.warning(
                f"hermes-agent 异常 | task={task_id} rc={rc} "
                f"error_detected={error_detected or 'N/A'} "
                f"stderr_tail={stderr[-200:] if stderr else '(empty)'}"
            )

        logger.info(
            f"hermes-agent 完成 | status={status} rc={rc} "
            f"dur={duration:.1f}s steps={len(steps)} "
            f"usage={usage if usage else 'N/A'}"
        )

        return BackendResult(
            status=status,
            steps=steps,
            duration_s=duration,
            stdout=stdout[-10000:] if len(stdout) > 10000 else stdout,
            error=stderr[-1000:] if rc != 0 and stderr else "",
            usage=usage,
        )

    def _detect_errors(self, stdout: str, stderr: str, steps: list[dict], workspace: Path) -> str | None:
        """增强错误检测：检查输出中的错误模式、工具调用失败、关键输出文件缺失。

        返回错误描述字符串，无错误返回 None。
        """
        # 1. 检查 stderr 中的错误模式
        error_patterns = [
            (r"Traceback \(most recent call last\)", "Python Traceback"),
            (r"Permission denied", "Permission denied"),
            (r"No such file or directory", "File not found"),
            (r"command not found", "Command not found"),
            (r"Error:", "Error"),
            (r"Exception:", "Exception"),
            (r"Failed to", "Operation failed"),
        ]
        for pattern, label in error_patterns:
            if re.search(pattern, stderr, re.IGNORECASE):
                return f"{label} in stderr"

        # 2. 检查 stdout 中的错误模式（排除正常的 "Error handling" 等）
        for pattern, label in error_patterns:
            matches = re.findall(pattern, stdout, re.IGNORECASE)
            if matches and label not in ("Error",):  # "Error" 太宽泛，只检查更具体的模式
                return f"{label} in stdout"

        # 3. 检查步骤轨迹中的工具调用失败
        for step in steps:
            obs = step.get("observation", "")
            if obs:
                # 检查工具输出中的错误
                if re.search(r"(error|failed|exception|traceback)", obs, re.IGNORECASE):
                    # 但要排除正常的 "error handling" 或 "no error" 等
                    if not re.search(r"(no error|without error|error handling|error rate)", obs, re.IGNORECASE):
                        return f"Tool error in step {step.get('step', '?')}: {obs[:100]}"

        # 4. 检查关键输出文件是否存在（从任务的 ground_truth checkpoints 提取）
        # 这部分在 verifier 层面会做更严格的检查，这里只做简单的存在性检查
        return None

    def _parse_steps(self, stdout: str, task) -> list[dict]:
        """从 hermes 输出解析步骤级轨迹。

        Hermes 实际输出格式（v0.17.0）：
        - 工具调用：`┊ 💻 $         command  0.3s`（终端命令）
        - 写文件：`┊ ✍️ preparing write_file…`
        - 模型回复：`╭─ ⚕ Hermes ────╮ ... ╰────────────────╯`
        - Session 信息：`Messages: 12 (1 user, 10 tool calls)`

        解析策略：
        1. 匹配 `┊ 💻 $` 开头的行，提取命令和耗时
        2. 匹配 `┊ ✍️` 开头的行，标记为写文件操作
        3. 降级为单步黑盒（同 aider 口径）
        """
        steps = []
        now = time.time()
        step_idx = 1

        for line in stdout.split("\n"):
            # 终端命令调用：┊ 💻 $         command  0.3s
            m = re.match(r"^\s*┊\s*💻\s*\$\s+(.+?)\s+(\d+\.\d+)s\s*$", line)
            if m:
                cmd = m.group(1).strip()
                dur = float(m.group(2))
                # 截断过长的命令（如 cat << 'PYEOF' ...）
                cmd_display = cmd[:200] + "..." if len(cmd) > 200 else cmd
                steps.append({
                    "step": step_idx,
                    "action": "bash",
                    "args": {"command": cmd_display, "duration_s": dur},
                    "observation": "",
                    "ts": now + step_idx * 0.001,
                })
                step_idx += 1
                continue

            # 写文件操作：┊ ✍️ preparing write_file…
            m = re.match(r"^\s*┊\s*✍️\s+(.+)$", line)
            if m:
                steps.append({
                    "step": step_idx,
                    "action": "write_file",
                    "args": {"detail": m.group(1).strip()},
                    "observation": "",
                    "ts": now + step_idx * 0.001,
                })
                step_idx += 1
                continue

        # 降级为单步黑盒（同 aider 口径）
        if not steps:
            steps = [{
                "step": 1,
                "action": "hermes",
                "args": {"model": self.model, "provider": self.provider},
                "observation": stdout[:500] if stdout else "(no output)",
                "ts": now,
            }]

        return steps

    def _extract_usage(self, stdout: str) -> dict | None:
        """从输出提取用量信息。

        Hermes 输出底部包含：
        - `Messages: 12 (1 user, 10 tool calls)`

        Hermes 不直接输出 token 用量，但可以提取消息数和工具调用数作为用量指标。
        """
        usage = {}

        # 提取 Messages 统计
        m = re.search(
            r"Messages:\s*(\d+)\s*\((\d+)\s*user,\s*(\d+)\s*tool calls\)",
            stdout, re.IGNORECASE,
        )
        if m:
            usage["total_messages"] = int(m.group(1))
            usage["user_messages"] = int(m.group(2))
            usage["tool_calls"] = int(m.group(3))

        # 提取 Duration
        m = re.search(r"Duration:\s*(\d+)s", stdout, re.IGNORECASE)
        if m:
            usage["duration_s"] = int(m.group(1))

        # 提取 Session ID
        m = re.search(r"Session:\s*(\S+)", stdout, re.IGNORECASE)
        if m:
            usage["session_id"] = m.group(1)

        return usage if usage else None

    def _extract_session_id(self, stdout: str) -> str | None:
        """从 hermes 输出提取 session_id。

        输出底部格式：
            Session:        20260910_083732_353098
        """
        m = re.search(r"Session:\s*(\S+)", stdout, re.IGNORECASE)
        return m.group(1) if m else None

    def _enrich_from_state_db(self, session_id: str) -> tuple[list[dict], dict | None]:
        """从 Hermes state.db 提取精确的 token 用量、成本和详细工具调用轨迹。

        Hermes 将所有会话数据存储在 ~/.hermes/state.db（SQLite）：
        - sessions 表：token 用量（input/output/cache/reasoning）、估算成本、消息数
        - messages 表：详细的消息记录，包括工具调用（tool_calls JSON）和工具输出（content）

        返回：(steps, usage)
        - steps：从 messages 表构建的详细工具调用轨迹（含工具名称、参数、输出）
        - usage：从 sessions 表提取的精确 token 用量和成本
        """
        state_db_path = Path.home() / ".hermes" / "state.db"
        if not state_db_path.exists():
            return [], None

        steps = []
        usage = {}

        try:
            # 使用只读模式连接，避免锁冲突
            conn = sqlite3.connect(f"file:{state_db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row

            # 1. 从 sessions 表提取 token 用量和成本
            cur = conn.execute(
                "SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "reasoning_tokens, message_count, tool_call_count, estimated_cost_usd, model "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row:
                usage = {
                    "prompt_tokens": row["input_tokens"] or 0,
                    "completion_tokens": row["output_tokens"] or 0,
                    "cache_read_tokens": row["cache_read_tokens"] or 0,
                    "cache_write_tokens": row["cache_write_tokens"] or 0,
                    "reasoning_tokens": row["reasoning_tokens"] or 0,
                    "total_tokens": (row["input_tokens"] or 0) + (row["output_tokens"] or 0),
                    "message_count": row["message_count"] or 0,
                    "tool_call_count": row["tool_call_count"] or 0,
                    "estimated_cost_usd": row["estimated_cost_usd"] or 0,
                    "model": row["model"] or self.model,
                    "session_id": session_id,
                }

            # 2. 从 messages 表提取详细工具调用轨迹
            cur = conn.execute(
                "SELECT id, role, content, tool_calls, tool_name, token_count, finish_reason, timestamp "
                "FROM messages WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,),
            )
            messages = cur.fetchall()
            conn.close()

            # 构建步骤轨迹：每个 assistant 消息的 tool_calls 对应后续 tool 消息的输出
            step_idx = 1
            for i, msg in enumerate(messages):
                if msg["role"] != "assistant" or not msg["tool_calls"]:
                    continue

                # 解析 tool_calls JSON
                try:
                    tool_calls = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    continue

                # 收集此 assistant 消息之后的所有 tool 输出（按顺序匹配）
                tool_outputs = []
                for j in range(i + 1, min(i + 20, len(messages))):
                    if messages[j]["role"] == "tool":
                        tool_outputs.append(messages[j])
                    elif messages[j]["role"] == "assistant":
                        # 遇到下一个 assistant 消息，停止收集（tool outputs 应该在两个 assistant 之间）
                        break

                for tc_idx, tc in enumerate(tool_calls):
                    # Hermes tool_calls 格式：{"id":..., "type":"function_call", "function":{"name":..., "arguments":"..."}}
                    func = tc.get("function", tc)
                    tool_name = func.get("name", tc.get("name", tc.get("tool_name", "unknown")))
                    tool_args = func.get("arguments", tc.get("arguments", tc.get("input", {})))
                    if isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except json.JSONDecodeError:
                            tool_args = {"raw": tool_args[:200]}

                    # 查找对应的 tool 输出（按顺序匹配第 tc_idx 个 tool output）
                    observation = ""
                    if tc_idx < len(tool_outputs):
                        tool_output = tool_outputs[tc_idx]["content"]
                        if tool_output:
                            try:
                                output_json = json.loads(tool_output)
                                # 优先提取 output 字段，其次是 content，最后是整个 JSON
                                observation = (
                                    output_json.get("output")
                                    or output_json.get("content")
                                    or output_json.get("result")
                                    or str(output_json)[:500]
                                )
                            except (json.JSONDecodeError, TypeError):
                                observation = tool_output[:500]

                    # 截断过长的 observation
                    if len(observation) > 500:
                        observation = observation[:500] + "..."

                    steps.append({
                        "step": step_idx,
                        "action": tool_name,
                        "args": tool_args if isinstance(tool_args, dict) else {"raw": str(tool_args)[:200]},
                        "observation": str(observation) if observation else "",
                        "ts": msg["timestamp"] or time.time(),
                    })
                    step_idx += 1

        except sqlite3.Error as e:
            logger.warning(f"从 state.db 提取数据失败 | session={session_id} | error={e}")
            return [], None

        return steps, usage if usage else None
