"""Qoder Agent 后端（V1.0）：调用阿里巴巴 Qoder CLI 非交互模式执行评测任务。

Qoder（https://qoder.com）是阿里巴巴推出的 AI 编程助手，原通义灵码。
本后端通过其 headless 模式在评测工作目录内一次性执行任务：

    qodercli -p "<task.description>" -o json --permission-mode auto --allowed-tools Bash,Edit,Write,Read,Glob,Grep

关键设计：
- 工作目录 = 启动 qodercli 时所在目录（即评测 workspace）
- -p/--print：非交互模式，打印响应并退出
- -o/--output-format：输出格式（text/json/stream-json）
- --permission-mode auto：自动权限模式
- --dangerously-skip-permissions：绕过所有权限检查（评测隔离环境）
- --allowed-tools：允许的工具列表
- --add-dir：允许 agent 访问的额外目录
- 退出码 0 = 完成
- 轨迹：JSON/stream-json 输出含 tool_call/tool_result 事件，解析为步骤级 steps 与回放 traces
- session 目录：~/.qoder/sessions/，可提取更完整的多轮轨迹

依赖：Qoder CLI 已安装（curl -fsSL https://qoder.com/install | bash）
认证：qodercli login（阿里云账号登录）
模型：见 SUPPORTED_MODELS（qwen-max, qwen-plus, qwen-turbo 等，需登录后查看）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.log import get_logger
from agent_eval.traces import tool_category

logger = get_logger(__name__)

# Qoder CLI 常见安装路径
_DEFAULT_QODER_CANDIDATES = (
    str(Path.home() / ".local" / "bin" / "qodercli"),
    str(Path.home() / ".qoder" / "bin" / "qodercli"),
    "/usr/local/bin/qodercli",
    "/opt/homebrew/bin/qodercli",
)
# 评测默认工具白名单：文件操作 + 命令 + 检索
_DEFAULT_ALLOWED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,List,Replace,MultiEdit"


def _find_qoder_cmd() -> str | None:
    """探测 qodercli 命令：环境变量 QODER_CMD > PATH > 常见安装路径。"""
    env_cmd = os.environ.get("QODER_CMD")
    if env_cmd:
        return env_cmd
    which = shutil.which("qodercli")
    if which:
        return which
    which2 = shutil.which("qoder")
    if which2:
        return which2
    for cand in _DEFAULT_QODER_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _parse_json_output(text: str) -> list[dict]:
    """从 qodercli -o json 输出中提取 JSON 事件。

    Qoder CLI 的 JSON 输出可能是：
    - 单个 JSON 对象
    - JSON 数组
    - NDJSON（每行一个 JSON 对象）
    - 带前缀日志文本的 JSON
    """
    if not text:
        return []

    events: list[dict] = []

    # 尝试解析为单个 JSON 对象或数组
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    events.append(item)
            return events
        elif isinstance(parsed, dict):
            events.append(parsed)
            return events
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试 NDJSON 解析（每行一个 JSON 对象）
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except (json.JSONDecodeError, ValueError):
            continue

    return events


def _parse_qoder_events(events: list[dict]) -> tuple[list[dict], list[dict], dict | None, str]:
    """解析 Qoder CLI JSON/stream-json 事件 → (steps, traces, usage, final_text)。

    Qoder CLI 的事件类型（基于 Claude Code 兼容设计）：
    - message (role=user/assistant)：用户/助手消息
    - reasoning / thinking：推理过程
    - tool_use / function_call：工具调用
    - tool_result / function_call_result：工具调用结果
    - usage：token 用量
    - result / final：最终结果
    - stream_event：流式事件（delta）
    """
    steps: list[dict] = []
    traces: list[dict] = []
    usage: dict | None = None
    final_text = ""
    model = ""
    last_user = ""
    step_no = 0
    pending: dict[str, dict] = {}  # tool_use_id -> {name, input, step_idx}
    total_input = 0
    total_output = 0
    found_usage = False

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type", event.get("event", event.get("stream_event", "")))
        ts = event.get("timestamp", event.get("ts", 0))
        if isinstance(ts, str):
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0

        if event.get("model"):
            model = event["model"]

        # 统一处理 message 事件
        if etype == "message":
            msg = event.get("message") or event
            role = msg.get("role", "")
            content = msg.get("content") or []
            if role == "user":
                if isinstance(content, str):
                    last_user = content[:300]
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                            last_user = c.get("text", "")[:300]
                        elif isinstance(c, dict) and c.get("type") == "tool_result":
                            cid = c.get("tool_use_id", c.get("callId", ""))
                            obs = c.get("content", "")
                            if isinstance(obs, list):
                                obs = "".join(
                                    x.get("text", "") for x in obs if isinstance(x, dict) and x.get("type") == "text"
                                )
                            if cid in pending:
                                steps[pending[cid]]["observation"] = str(obs)[:2000]
                                steps[pending[cid]]["ts"] = ts or steps[pending[cid]]["ts"]
                            traces.append({
                                "kind": "tool", "category": tool_category("bash"),
                                "ts": ts, "tool": "tool_result", "args": {},
                                "observation": str(obs)[:2000],
                            })
            elif role == "assistant":
                msg_usage = msg.get("usage") or {}
                inp = msg_usage.get("input_tokens") or msg_usage.get("prompt_tokens") or 0
                out = msg_usage.get("output_tokens") or msg_usage.get("completion_tokens") or 0
                if inp or out:
                    total_input += int(inp)
                    total_output += int(out)
                    found_usage = True
                for c in (content if isinstance(content, list) else []):
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct in ("text", "output_text") and c.get("text", "").strip():
                        traces.append({
                            "kind": "llm", "ts": ts, "model": model, "input": last_user,
                            "output": c["text"][:2000], "phase": "final",
                        })
                        final_text = c["text"]
                    elif ct in ("thinking", "reasoning") and c.get("thinking", c.get("reasoning", "")).strip():
                        traces.append({
                            "kind": "llm", "ts": ts, "model": model, "input": last_user,
                            "output": c.get("thinking", c.get("reasoning", ""))[:2000],
                            "phase": "reasoning",
                        })
                    elif ct in ("tool_use", "function_call"):
                        step_no += 1
                        name = c.get("name") or c.get("tool_name") or "tool"
                        inp_args = c.get("input") or c.get("arguments") or {}
                        if isinstance(inp_args, str):
                            try:
                                inp_args = json.loads(inp_args)
                            except (json.JSONDecodeError, ValueError):
                                inp_args = {"raw": inp_args[:500]}
                        cid = c.get("id") or c.get("tool_use_id") or c.get("callId") or ""
                        pending[cid] = {"name": name, "input": inp_args, "step_idx": len(steps)}
                        steps.append({
                            "step": step_no, "action": name, "args": inp_args,
                            "observation": "", "ts": ts,
                        })
                        traces.append({
                            "kind": "llm", "ts": ts, "model": model, "input": last_user,
                            "output": json.dumps(inp_args, ensure_ascii=False)[:500],
                            "tool": name, "phase": "decision",
                        })

        # 独立的 tool_use 事件
        elif etype in ("tool_use", "function_call"):
            step_no += 1
            name = event.get("name") or event.get("tool_name") or "tool"
            inp_args = event.get("input") or event.get("arguments") or {}
            if isinstance(inp_args, str):
                try:
                    inp_args = json.loads(inp_args)
                except (json.JSONDecodeError, ValueError):
                    inp_args = {"raw": inp_args[:500]}
            cid = event.get("id") or event.get("tool_use_id") or event.get("callId") or ""
            pending[cid] = {"name": name, "input": inp_args, "step_idx": len(steps)}
            steps.append({
                "step": step_no, "action": name, "args": inp_args,
                "observation": "", "ts": ts,
            })
            traces.append({
                "kind": "llm", "ts": ts, "model": model, "input": last_user,
                "output": json.dumps(inp_args, ensure_ascii=False)[:500],
                "tool": name, "phase": "decision",
            })

        # 独立的 tool_result 事件
        elif etype in ("tool_result", "function_call_result"):
            cid = event.get("tool_use_id") or event.get("callId") or ""
            p = pending.get(cid, {})
            name = p.get("name") or event.get("name") or "tool"
            obs = event.get("content", event.get("output", ""))
            if isinstance(obs, list):
                obs = "".join(
                    x.get("text", "") for x in obs if isinstance(x, dict) and x.get("type") == "text"
                )
            elif isinstance(obs, dict):
                obs = obs.get("text", json.dumps(obs, ensure_ascii=False))
            if p.get("step_idx") is not None:
                steps[p["step_idx"]]["observation"] = str(obs)[:2000]
                steps[p["step_idx"]]["ts"] = ts or steps[p["step_idx"]]["ts"]
            traces.append({
                "kind": "tool", "category": tool_category(name),
                "ts": ts, "tool": name, "args": p.get("input") or {},
                "observation": str(obs)[:2000],
            })

        # usage 事件
        elif etype == "usage":
            u = event.get("usage") or event
            inp = u.get("input_tokens") or u.get("prompt_tokens") or 0
            out = u.get("output_tokens") or u.get("completion_tokens") or 0
            if inp or out:
                total_input += int(inp)
                total_output += int(out)
                found_usage = True

        # result / final 事件
        elif etype in ("result", "final", "complete"):
            result_text = event.get("result") or event.get("text") or event.get("final_text", "")
            if result_text and not final_text:
                final_text = str(result_text)
            result_usage = event.get("usage") or {}
            if isinstance(result_usage, dict):
                inp = result_usage.get("input_tokens") or result_usage.get("prompt_tokens") or 0
                out = result_usage.get("output_tokens") or result_usage.get("completion_tokens") or 0
                if inp or out:
                    total_input += int(inp)
                    total_output += int(out)
                    found_usage = True

        # stream_event delta（流式文本增量）
        elif etype in ("stream_event", "delta"):
            delta = event.get("delta") or event.get("text") or ""
            if delta and isinstance(delta, str):
                final_text += delta

    if found_usage:
        usage = {"prompt_tokens": total_input, "completion_tokens": total_output}

    return steps, traces, usage, final_text


class QoderAgentBackend(Backend):
    """Qoder CLI 后端（阿里巴巴，原通义灵码）。"""

    name = "qoder-agent"
    version = "1.0.0"
    default_model = "claude-opus-4-8"
    # Qoder 支持的模型（需登录后查看实际可用模型）
    SUPPORTED_MODELS = (
        "claude-opus-4-8",
        "claude-3-5-sonnet",
        "qwen-max",
        "qwen-plus",
        "qwen-turbo",
        "qwen3-max",
        "qwen3-plus",
        "qwen3-coder",
        "qwen2.5-coder-32b",
        "qwen2.5-coder-7b",
        "deepseek-v3",
        "gpt-4o",
    )
    # 快速模式默认模型（性价比高，适合大规模回归测试）
    FAST_MODEL = "claude-opus-4-8"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: int = 300,
        cmd: str | None = None,
        allowed_tools: str | None = None,
    ) -> None:
        # 模型选择优先级（从高到低）：
        # 1. 显式传入的模型
        # 2. AGENT_EVAL_QODER_MODEL 环境变量
        # 3. AGENT_EVAL_QODER_FAST=true 时使用 FAST_MODEL
        # 4. 后端 default_model
        env_model = os.environ.get("AGENT_EVAL_QODER_MODEL")
        fast_mode = os.environ.get("AGENT_EVAL_QODER_FAST", "").lower() in ("1", "true", "yes", "on")
        if model and model in self.SUPPORTED_MODELS:
            self.model = model
        elif env_model:
            self.model = env_model
        elif fast_mode:
            self.model = self.FAST_MODEL
        else:
            self.model = self.default_model
        if model and model not in self.SUPPORTED_MODELS and model:
            logger.info("qoder-agent 模型 '%s' 不在已验证列表中，使用默认 %s", model, self.model)
        # Qoder CLI 认证：qodercli login（阿里云账号登录）
        # 也支持通过环境变量传递 API Key（如 DASHSCOPE_API_KEY）
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")
        self.timeout_s = timeout_s
        self.cmd = cmd or _find_qoder_cmd()
        self.allowed_tools = allowed_tools or _DEFAULT_ALLOWED_TOOLS

    def run(self, task, workspace) -> BackendResult:
        if not self.cmd:
            return BackendResult(
                status="error",
                error=(
                    "未找到 qodercli 命令，请先安装："
                    "curl -fsSL https://qoder.com/install | bash"
                    "（或设置 QODER_CMD 环境变量）"
                ),
            )

        cmd = [
            self.cmd,
            "-p", task.description,
            "-o", "json",
            "-m", self.model,
            "--permission-mode", "auto",
            "--allowed-tools", self.allowed_tools,
            "--add-dir", str(workspace),
            "--no-session-persistence",
        ]

        env = os.environ.copy()
        if self.api_key:
            env["DASHSCOPE_API_KEY"] = self.api_key
        # 禁用遥测
        env.setdefault("QODER_TELEMETRY_DISABLED", "1")
        # Qoder CLI 配置目录隔离
        tmp_home = Path(tempfile.mkdtemp(prefix="agent-eval-qoder-home-"))
        src = Path.home() / ".qoder"
        if src.is_dir():
            dst = tmp_home / ".qoder"
            dst.mkdir(parents=True, exist_ok=True)
            skip_dirs = {"sessions", "logs", "cache", "tmp", ".cache", ".bin", "bin", "entry", "external-commands"}
            for item in src.iterdir():
                if item.name in skip_dirs:
                    continue
                if item.is_dir():
                    try:
                        shutil.copytree(item, dst / item.name, dirs_exist_ok=True)
                    except Exception:
                        pass
                else:
                    try:
                        shutil.copy2(item, dst / item.name)
                    except Exception:
                        pass
        env["HOME"] = str(tmp_home)

        logger.info(
            "qoder-agent 执行开始 | model=%s timeout=%ds",
            self.model, self.timeout_s,
        )
        start = time.time()
        try:
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
                logger.warning("qoder-agent 超时 (>%ds)，已 kill", self.timeout_s)
                try:
                    out, err = proc.communicate(timeout=10)
                except Exception:
                    out, err = "", ""
                partial = ((out or "") + (("\n" + err) if err else ""))[-2000:]
                return BackendResult(
                    status="timeout",
                    steps=[
                        {"step": 1, "action": "qoder-agent", "args": {"model": self.model},
                         "observation": partial, "ts": round(time.time(), 3)}
                    ],
                    duration_s=round(time.time() - start, 3),
                    error=f"qoder-agent 超时（>{self.timeout_s}s）。输出尾部：{partial[:400]}",
                )

            full = (out or "") + (("\n" + err) if err else "")
            events = _parse_json_output(full)
            steps, traces, usage, final_text = _parse_qoder_events(events)

            if not steps:
                steps = [
                    {"step": 1, "action": "qoder-agent", "args": {"model": self.model},
                     "observation": full[-3000:], "ts": round(time.time(), 3)}
                ]
            if not traces and final_text:
                traces = [
                    {"kind": "llm", "ts": 0, "model": self.model, "input": task.description[:300],
                     "output": str(final_text)[:2000], "phase": "final"}
                ]

            # 判定完成：退出码 0，或提取到有效结果
            has_result = bool(final_text) or bool(usage) or bool(events)
            ok = rc == 0 or (rc != 0 and has_result)
            logger.info(
                "qoder-agent 完成 | status=%s rc=%d dur=%.1fs steps=%d traces=%d usage=%s events=%d",
                "completed" if ok else "error", rc, time.time() - start,
                len(steps), len(traces), usage, len(events),
            )
            if not ok:
                logger.warning("qoder-agent 未正常完成: 退出码 %d，stderr 尾部: %s", rc, (err or "")[-300:])
            return BackendResult(
                status="completed" if ok else "error",
                steps=steps,
                traces=traces,
                usage=usage,
                duration_s=round(time.time() - start, 3),
                stdout=str(final_text) if final_text else (out or "")[-1000:],
                error="" if ok else f"qoder-agent 退出码 {rc}：{(err or out or '')[-400:]}",
            )
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)
