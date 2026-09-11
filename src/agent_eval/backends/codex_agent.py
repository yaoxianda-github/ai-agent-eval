"""Codex CLI 后端（黑盒 CLI 非交互模式）。

通过 `codex exec` 非交互模式执行任务，解析 session JSONL 提取步骤轨迹和 token 用量。

依赖：npm install -g @openai/codex（安装后命令为 codex）
API Key：环境变量 OPENAI_API_KEY（或 codex login 配置的认证）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.log import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_S = 300
_DEFAULT_MAX_BUDGET_USD = 5.0


def _find_codex_cmd() -> str | None:
    """定位 codex 可执行文件。"""
    if os.environ.get("CODEX_CMD"):
        return os.environ["CODEX_CMD"]
    # 常见安装路径
    candidates = [
        "codex",
        str(Path.home() / ".local" / "bin" / "codex"),
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
    ]
    for c in candidates:
        if shutil.which(c):
            return shutil.which(c)
    return None


def _parse_codex_session(session_path: Path) -> tuple[list[dict], list[dict], dict | None]:
    """解析 codex session JSONL → (steps, traces, usage)。

    事件类型：
    - response_item / payload.type=message, role=assistant：模型输出（thinking/text）
    - response_item / payload.type=function_call：工具调用
    - response_item / payload.type=function_call_output：工具返回
    - event_msg / payload.type=usage：token 用量
    """
    steps: list[dict] = []
    traces: list[dict] = []
    usage: dict | None = None
    step_idx = 0

    try:
        with open(session_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = evt.get("payload", {})
                evt_type = evt.get("type", "")

                # token 用量
                if evt_type == "event_msg" and payload.get("type") == "usage":
                    u = payload.get("usage", {})
                    if u:
                        usage = {
                            "prompt_tokens": int(u.get("input_tokens", 0) or 0),
                            "completion_tokens": int(u.get("output_tokens", 0) or 0),
                            "cache_read_tokens": int(u.get("cached_input_tokens", 0) or 0),
                            "reasoning_tokens": int(u.get("reasoning_tokens", 0) or 0),
                        }

                # 响应项
                if evt_type == "response_item":
                    item_type = payload.get("type", "")
                    role = payload.get("role", "")

                    # 工具调用
                    if item_type == "function_call":
                        name = payload.get("name", "unknown")
                        args = payload.get("arguments", "")
                        # 解析参数
                        try:
                            args_dict = json.loads(args) if isinstance(args, str) else args
                        except (json.JSONDecodeError, TypeError):
                            args_dict = {"raw": str(args)[:200]}
                        step_idx += 1
                        steps.append({
                            "step": step_idx,
                            "action": name,
                            "args": args_dict,
                            "observation": "",
                        })
                        traces.append({
                            "step": step_idx,
                            "tool": name,
                            "phase": "decision",
                            "args": args_dict,
                        })

                    # 工具返回
                    elif item_type == "function_call_output":
                        output = payload.get("output", "")
                        call_id = payload.get("call_id", "")
                        # 匹配最近的工具调用
                        for s in reversed(steps):
                            if s.get("observation") == "" and s.get("action"):
                                s["observation"] = str(output)[:500]
                                break
                        if steps:
                            traces.append({
                                "step": steps[-1]["step"],
                                "tool": steps[-1]["action"],
                                "phase": "observation",
                                "output": str(output)[:500],
                            })

                    # 模型文本输出
                    elif item_type == "message" and role == "assistant":
                        content = payload.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict):
                                    ctype = c.get("type", "")
                                    if ctype == "output_text" or ctype == "text":
                                        text = c.get("text", "")
                                        if text and not steps:
                                            # 第一步记录模型初始响应
                                            step_idx += 1
                                            steps.append({
                                                "step": step_idx,
                                                "action": "codex",
                                                "args": {"model": "codex"},
                                                "observation": text[:500],
                                            })
                                        traces.append({
                                            "step": max(step_idx, 1),
                                            "tool": "llm",
                                            "phase": "final",
                                            "text": text[:500],
                                        })

    except Exception as e:  # noqa: BLE001
        logger.warning("解析 codex session 失败: %s", e)

    return steps, traces, usage


class CodexAgentBackend(Backend):
    """Codex CLI 后端（黑盒 CLI 非交互模式）。"""

    name = "codex-agent"
    version = "0.1.0"
    default_model = "gpt-5.5"
    # 已验证可用的模型列表
    SUPPORTED_MODELS = ("gpt-5.5", "gpt-5", "gpt-4o", "o3", "o4-mini")
    # 快速模式默认模型
    FAST_MODEL = "gpt-4o"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        cmd: str | None = None,
        max_budget_usd: float = _DEFAULT_MAX_BUDGET_USD,
        max_steps: int | None = None,
    ) -> None:
        # 模型选择优先级：显式传入 > 环境变量 > 快速模式 > 默认
        env_model = os.environ.get("AGENT_EVAL_CODEX_MODEL")
        fast_mode = os.environ.get("AGENT_EVAL_CODEX_FAST", "").lower() in ("1", "true", "yes", "on")
        if model and model not in ("deepseek-chat", "claude-opus-4-8"):
            self.model = model
        elif env_model:
            self.model = env_model
        elif fast_mode:
            self.model = self.FAST_MODEL
        else:
            self.model = self.default_model
        if self.model not in self.SUPPORTED_MODELS:
            logger.warning(
                "codex-agent 模型 '%s' 不在已验证列表 %s 中，可能缺少定价配置或不兼容",
                self.model, self.SUPPORTED_MODELS,
            )
        # API Key：OPENAI_API_KEY（codex 默认使用）
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.timeout_s = timeout_s
        self.cmd = cmd or _find_codex_cmd()
        self.max_budget_usd = max_budget_usd
        # max_steps 保留接口：codex 黑盒后端由自身循环控制

    def run(self, task, workspace) -> BackendResult:
        if not self.cmd:
            return BackendResult(
                status="error",
                error='未找到 codex 命令，请先：npm install -g @openai/codex（或设置 CODEX_CMD）',
            )

        # 构造 codex exec 命令
        cmd = [
            self.cmd,
            "exec",
            task.description,
            "--json",
            "--model", self.model,
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--dangerously-bypass-approvals-and-sandbox",
        ]

        env = os.environ.copy()
        if self.api_key:
            env["OPENAI_API_KEY"] = self.api_key
        env.setdefault("CODEX_TELEMETRY_DISABLED", "1")

        # 隔离 CODEX_HOME，避免污染用户配置
        tmp_home = Path(tempfile.mkdtemp(prefix="agent-eval-codex-home-"))
        src = Path.home() / ".codex"
        if src.is_dir():
            dst = tmp_home / ".codex"
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                # 跳过 sessions/projects/backups 等大目录，保留配置和认证
                if item.name in ("sessions", "projects", "backups", "logs_2.sqlite-wal", "state_5.sqlite-wal"):
                    continue
                if item.is_dir():
                    shutil.copytree(item, dst / item.name, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst / item.name)
        env["HOME"] = str(tmp_home)
        env["CODEX_HOME"] = str(tmp_home / ".codex")

        logger.info(
            "codex-agent 执行开始 | model=%s timeout=%ds",
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
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                logger.warning("codex-agent 超时 (%ds)，已终止", self.timeout_s)
            rc = proc.returncode
        except Exception as e:  # noqa: BLE001
            logger.error("codex-agent 执行异常: %s", e)
            return BackendResult(status="error", error=f"执行异常: {e}")
        finally:
            # 清理临时 HOME
            try:
                shutil.rmtree(tmp_home, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass

        duration = time.time() - start
        full = (out or "") + (("\n" + err) if err else "")

        # 从 JSONL 输出提取最终消息
        final_text = ""
        for line in (out or "").splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
                if evt.get("type") == "turn.completed":
                    # 最终完成事件
                    msg = evt.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                                    final_text += c.get("text", "")
            except json.JSONDecodeError:
                continue

        # 从 session 文件提取步骤轨迹和 token 用量
        steps: list[dict] = []
        traces: list[dict] = []
        usage: dict | None = None
        session_dir = tmp_home / ".codex" / "sessions"
        if session_dir.is_dir():
            # 找最近的 session 文件
            session_files = sorted(session_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if session_files:
                steps, traces, usage = _parse_codex_session(session_files[0])

        # 如果没有从 session 提取到步骤，用默认步骤
        if not steps:
            steps = [{"step": 1, "action": "codex", "args": {"model": self.model}, "observation": final_text[:300]}]

        # 判定完成状态
        has_result = bool(final_text) or bool(steps) or rc == 0
        ok = rc == 0 or (rc != 0 and has_result)

        # 错误检测
        error_msg = None
        if not ok:
            error_msg = (err or out or "未知错误")[:500]
            # 检测常见错误模式
            if "401" in full or "unauthorized" in full.lower():
                error_msg = "API 认证失败（401），请检查 OPENAI_API_KEY"
            elif "429" in full or "rate limit" in full.lower():
                error_msg = "API 速率限制（429），请稍后重试"
            elif "insufficient_quota" in full or "quota" in full.lower():
                error_msg = "API 额度不足，请检查账户余额"

        logger.info(
            "codex-agent 完成 | status=%s rc=%d dur=%.1fs steps=%d traces=%d usage=%s",
            "completed" if ok else "error", rc, duration, len(steps), len(traces), usage,
        )

        return BackendResult(
            status="completed" if ok else "error",
            final=final_text or (err or "")[:300],
            steps=steps,
            traces=traces,
            usage=usage,
            error=error_msg,
            duration_s=round(duration, 3),
        )
