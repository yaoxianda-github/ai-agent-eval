"""WorkBuddy 后端（V1.0）：调用腾讯 WorkBuddy（CodeBuddy Code）CLI 非交互模式执行评测任务。

WorkBuddy（https://www.workbuddy.ai）是腾讯推出的 AI Agent 桌面应用，
内置 CodeBuddy Code CLI。本后端通过其 headless 模式在评测工作目录内一次性执行任务：

    codebuddy -p "<task.description>" --output-format json --model <model> --permission-mode auto -y

关键设计：
- 工作目录 = 启动 codebuddy 时所在目录（即评测 workspace）
- -p/--print：非交互模式，打印响应后退出
- --output-format json：单次结构化结果，含最终文本、token 用量、花费
- --permission-mode auto：非交互下自动处理权限提示
- -y/--dangerously-skip-permissions：跳过权限确认（评测隔离环境）
- 退出码 0 = 完成
- 轨迹：JSON 含 function_call/function_call_result 事件，解析为步骤级 steps 与回放 traces
- result 事件含最终文本、duration_ms、num_turns、usage（input_tokens/output_tokens/cache tokens）

依赖：WorkBuddy 桌面应用已安装（内置 CodeBuddy Code CLI）
CLI 路径：/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy
API Key：WorkBuddy 应用内已登录（CLI 复用应用认证状态）
模型：见 SUPPORTED_MODELS（glm-5.x / deepseek-v4 / kimi-k3 / minimax-m3 / hy3 等）
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

# WorkBuddy CLI 常见安装路径（macOS）
_DEFAULT_CODEBUDDY_CANDIDATES = (
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
    "/Applications/CodeBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
    "/usr/local/bin/codebuddy",
    "/opt/homebrew/bin/codebuddy",
)
# 评测默认工具白名单：文件操作 + 命令 + 检索
_DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,List"
# 单次评测最大轮次（防止无限循环）
_DEFAULT_MAX_TURNS = 20


def _find_codebuddy_cmd() -> str | None:
    """探测 codebuddy 命令：环境变量 CODEBUDDY_CMD > PATH > 常见安装路径。"""
    env_cmd = os.environ.get("CODEBUDDY_CMD")
    if env_cmd:
        return env_cmd
    which = shutil.which("codebuddy")
    if which:
        return which
    which_cbc = shutil.which("cbc")
    if which_cbc:
        return which_cbc
    for cand in _DEFAULT_CODEBUDDY_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _parse_codebuddy_json(text: str) -> list[dict]:
    """从 codebuddy --output-format json 输出中提取 JSON 数组。

    codebuddy 的 JSON 输出是一个事件数组（[...]），但可能有前缀日志文本。
    处理场景：
    - 前缀非 JSON 文本（日志/警告）
    - 完整 JSON 数组
    - 输出截断（找最外层 [...]）
    """
    if not text:
        return []

    # 策略1：直接从第一个 [ 解析（最常见）
    idx = text.find("[")
    if idx >= 0:
        try:
            data = json.loads(text[idx:])
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # 策略2：找最外层完整 [...] 对
    stack: list[int] = []
    in_string = False
    escape = False
    candidates: list[tuple[int, int]] = []
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
        if ch == "[":
            stack.append(i)
        elif ch == "]" and stack:
            start = stack.pop()
            if not stack:
                candidates.append((start, i))

    for start, end in reversed(candidates):
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            continue

    return []


def _parse_codebuddy_events(events: list[dict]) -> tuple[list[dict], list[dict], dict | None, str]:
    """解析 codebuddy JSON 事件数组 → (steps, traces, usage, final_text)。

    事件类型：
    - message (role=user/assistant)：用户/助手消息
    - reasoning：推理过程
    - function_call：工具调用（name, callId, arguments）
    - function_call_result：工具调用结果（name, callId, status, output）
    - result：最终结果（subtype, result, duration_ms, num_turns, usage）
    - file-history-snapshot：文件历史快照（忽略）
    """
    steps: list[dict] = []
    traces: list[dict] = []
    usage: dict | None = None
    final_text = ""
    model = ""
    last_user = ""
    step_no = 0
    pending: dict[str, dict] = {}  # callId -> {name, args, step_idx}

    for event in events:
        if not isinstance(event, dict):
            continue
        etype = event.get("type", "")
        ts = event.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = 0

        provider = event.get("providerData") or {}
        if provider.get("model"):
            model = provider["model"]

        if etype == "message":
            role = event.get("role", "")
            content = event.get("content") or []
            if role == "user":
                for c in content:
                    if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                        text = c.get("text", "")
                        if "<user_query>" in text:
                            # 提取 <user_query>...</user_query> 之间的内容
                            start = text.find("<user_query>") + len("<user_query>")
                            end = text.find("</user_query>")
                            if end > start:
                                last_user = text[start:end][:300]
                        elif not text.startswith("<system-reminder"):
                            last_user = text[:300]
            elif role == "assistant":
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        traces.append({
                            "kind": "llm", "ts": ts, "model": model, "input": last_user,
                            "output": c.get("text", "")[:2000], "phase": "final",
                        })

        elif etype == "reasoning":
            raw = event.get("rawContent") or []
            reasoning_text = " ".join(
                r.get("text", "") for r in raw if isinstance(r, dict) and r.get("text")
            )
            if reasoning_text.strip():
                traces.append({
                    "kind": "llm", "ts": ts, "model": model, "input": last_user,
                    "output": reasoning_text[:2000], "phase": "reasoning",
                })

        elif etype == "function_call":
            name = event.get("name") or "tool"
            call_id = event.get("callId") or ""
            args = event.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    args = {"raw": args[:500]}
            step_no += 1
            pending[call_id] = {"name": name, "args": args, "step_idx": len(steps)}
            steps.append({
                "step": step_no, "action": name, "args": args,
                "observation": "", "ts": ts,
            })
            traces.append({
                "kind": "llm", "ts": ts, "model": model, "input": last_user,
                "output": json.dumps(args, ensure_ascii=False)[:500],
                "tool": name, "phase": "decision",
            })

        elif etype == "function_call_result":
            name = event.get("name") or "tool"
            call_id = event.get("callId") or ""
            status = event.get("status", "")
            output = event.get("output") or {}
            obs = ""
            if isinstance(output, dict):
                obs = output.get("text", "") or json.dumps(output, ensure_ascii=False)
            elif isinstance(output, str):
                obs = output
            if status == "error":
                obs = f"[ERROR] {obs}"
            p = pending.get(call_id, {})
            if p.get("step_idx") is not None:
                steps[p["step_idx"]]["observation"] = str(obs)[:2000]
                steps[p["step_idx"]]["ts"] = ts or steps[p["step_idx"]]["ts"]
            traces.append({
                "kind": "tool", "category": tool_category(name),
                "ts": ts, "tool": name, "args": p.get("args") or {},
                "observation": str(obs)[:2000],
            })

        elif etype == "result":
            final_text = event.get("result", "") or ""
            result_usage = event.get("usage") or {}
            if isinstance(result_usage, dict):
                input_tokens = result_usage.get("input_tokens") or 0
                output_tokens = result_usage.get("output_tokens") or 0
                if input_tokens or output_tokens:
                    usage = {
                        "prompt_tokens": int(input_tokens),
                        "completion_tokens": int(output_tokens),
                    }
                    # 额外缓存信息
                    cache_creation = result_usage.get("cache_creation_input_tokens")
                    cache_read = result_usage.get("cache_read_input_tokens")
                    if cache_creation or cache_read:
                        usage["cache_creation_tokens"] = int(cache_creation or 0)
                        usage["cache_read_tokens"] = int(cache_read or 0)

    return steps, traces, usage, final_text


class WorkBuddyBackend(Backend):
    """WorkBuddy（CodeBuddy Code）CLI 后端。"""

    name = "workbuddy"
    version = "1.0.0"
    default_model = "glm-5.1"
    # 已验证可用的模型列表（从 codebuddy --help 输出获取）
    SUPPORTED_MODELS = (
        "auto",
        "glm-5.3",
        "glm-5.3-flash",
        "glm-5.2",
        "glm-5.1",
        "glm-5v-turbo",
        "deepseek-v4-pro",
        "deepseek-v4.1-flash",
        "kimi-k3-1",
        "kimi-k2.7",
        "kimi-k2.6",
        "minimax-m3",
        "hy3",
        "hy3-x",
        "hy4-preview-f",
    )
    # 快速模式默认模型（性价比高，适合大规模回归测试）
    FAST_MODEL = "glm-5.1"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: int = 300,
        cmd: str | None = None,
        allowed_tools: str | None = None,
        max_turns: int = _DEFAULT_MAX_TURNS,
    ) -> None:
        # 模型选择优先级（从高到低）：
        # 1. 显式传入的模型
        # 2. AGENT_EVAL_WORKBUDDY_MODEL 环境变量
        # 3. AGENT_EVAL_WORKBUDDY_FAST=true 时使用 FAST_MODEL
        # 4. 后端 default_model
        env_model = os.environ.get("AGENT_EVAL_WORKBUDDY_MODEL")
        fast_mode = os.environ.get("AGENT_EVAL_WORKBUDDY_FAST", "").lower() in ("1", "true", "yes", "on")
        if model and model in self.SUPPORTED_MODELS:
            self.model = model
        elif env_model:
            self.model = env_model
        elif fast_mode:
            self.model = self.FAST_MODEL
        else:
            self.model = self.default_model
        if model and model not in self.SUPPORTED_MODELS and model:
            logger.info("workbuddy 模型 '%s' 不在已验证列表中，使用默认 %s", model, self.model)
        if self.model not in self.SUPPORTED_MODELS:
            logger.warning(
                "workbuddy 模型 '%s' 不在已验证列表 %s 中，可能不兼容",
                self.model, self.SUPPORTED_MODELS,
            )
        # WorkBuddy CLI 复用桌面应用的认证状态，不需要单独的 API Key
        # 但保留 api_key 参数以与其他后端接口兼容
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.cmd = cmd or _find_codebuddy_cmd()
        self.allowed_tools = allowed_tools or _DEFAULT_ALLOWED_TOOLS
        self.max_turns = max_turns

    def run(self, task, workspace) -> BackendResult:
        if not self.cmd:
            return BackendResult(
                status="error",
                error=(
                    "未找到 codebuddy 命令，请先安装 WorkBuddy 桌面应用"
                    "（https://www.workbuddy.ai）或设置 CODEBUDDY_CMD 环境变量"
                ),
            )

        cmd = [
            self.cmd,
            "-p", task.description,
            "--output-format", "json",
            "--model", self.model,
            "--permission-mode", "auto",
            "-y",
            "--max-turns", str(self.max_turns),
        ]

        env = os.environ.copy()
        # 禁用遥测
        env.setdefault("CODEBUDDY_TELEMETRY_DISABLED", "1")
        # WorkBuddy CLI 复用 ~/.workbuddy 的配置和认证状态
        # 为了评测隔离，复制配置到临时 HOME，跳过 sessions/traces/logs 等大目录
        tmp_home = Path(tempfile.mkdtemp(prefix="agent-eval-workbuddy-home-"))
        src = Path.home() / ".workbuddy"
        if src.is_dir():
            dst = tmp_home / ".workbuddy"
            dst.mkdir(parents=True, exist_ok=True)
            skip_dirs = {"sessions", "traces", "logs", "app", "file-history",
                          "shell-snapshots", "artifact-index", "audit-log", "memory",
                          "local_storage", "plugin-marketplace-state-new", "projects",
                          "tasks", "plans", "skills", "connectors-marketplace",
                          ".workbuddy-sqlite-migrations"}
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
            "workbuddy 执行开始 | model=%s timeout=%ds max_turns=%d",
            self.model, self.timeout_s, self.max_turns,
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
                logger.warning("workbuddy 超时 (>%ds)，已 kill", self.timeout_s)
                try:
                    out, err = proc.communicate(timeout=10)
                except Exception:
                    out, err = "", ""
                partial = ((out or "") + (("\n" + err) if err else ""))[-2000:]
                return BackendResult(
                    status="timeout",
                    steps=[
                        {"step": 1, "action": "workbuddy", "args": {"model": self.model},
                         "observation": partial, "ts": round(time.time(), 3)}
                    ],
                    duration_s=round(time.time() - start, 3),
                    error=f"workbuddy 超时（>{self.timeout_s}s）。输出尾部：{partial[:400]}",
                )

            full = (out or "") + (("\n" + err) if err else "")
            events = _parse_codebuddy_json(full)
            steps, traces, usage, final_text = _parse_codebuddy_events(events)

            if not steps:
                steps = [
                    {"step": 1, "action": "workbuddy", "args": {"model": self.model},
                     "observation": full[-3000:], "ts": round(time.time(), 3)}
                ]
            if not traces and final_text:
                traces = [
                    {"kind": "llm", "ts": 0, "model": self.model, "input": task.description[:300],
                     "output": str(final_text)[:2000], "phase": "final"}
                ]

            # 判定完成：退出码 0，或提取到有效 result 事件
            has_result = bool(final_text) or bool(usage) or bool(events)
            ok = rc == 0 or (rc != 0 and has_result)
            logger.info(
                "workbuddy 完成 | status=%s rc=%d dur=%.1fs steps=%d traces=%d usage=%s events=%d",
                "completed" if ok else "error", rc, time.time() - start,
                len(steps), len(traces), usage, len(events),
            )
            if not ok:
                logger.warning("workbuddy 未正常完成: 退出码 %d，stderr 尾部: %s", rc, (err or "")[-300:])
            return BackendResult(
                status="completed" if ok else "error",
                steps=steps,
                traces=traces,
                usage=usage,
                duration_s=round(time.time() - start, 3),
                stdout=str(final_text) if final_text else (out or "")[-1000:],
                error="" if ok else f"workbuddy 退出码 {rc}：{(err or out or '')[-400:]}",
            )
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)
