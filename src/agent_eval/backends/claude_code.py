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
import tempfile
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


def _extract_json(text: str) -> dict:
    """从 claude --output-format json 输出中提取 JSON 对象（高健壮性）。

    处理场景：
    - 前缀非 JSON 文本（日志/警告/权限提示）
    - 多行 JSON / 多个 JSON 对象（取最长的完整对象）
    - 输出截断（括号匹配找完整 {} 对，再不行逐步截断修复）
    - JSON 嵌套在字符串中
    """
    if not text:
        return {}

    # 策略1：直接从第一个 { 解析（最常见、最快）
    idx = text.find("{")
    if idx >= 0:
        try:
            return json.loads(text[idx:])
        except (json.JSONDecodeError, ValueError):
            pass

    # 策略2：单次扫描找所有顶层完整 {} 对，取可解析的最大对象
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
            if not stack:  # 顶层 {} 对
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
    if best:
        return best

    # 策略3：截断修复——从第一个 { 开始逐步去掉末尾，直到能解析
    if idx >= 0:
        truncated = text[idx:]
        max_cut = min(1000, len(truncated) - 1)
        for cut in range(1, max_cut):
            try:
                return json.loads(truncated[:-cut])
            except (json.JSONDecodeError, ValueError):
                continue

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


# ---------- claude session JSONL 解析（补多轮步骤/轨迹/token） ----------

def _find_claude_session(tmp_home: Path, workspace: Path) -> Path | None:
    """定位本次运行的 claude session JSONL 文件。

    claude CLI 把 session 存在 <HOME>/.claude/projects/<编码路径>/<uuid>.jsonl，
    编码路径 = "-" + abs_path.lstrip("/").replace("/", "-")。
    同一 workspace 可能有多个 session，取最新修改的一个。
    """
    encoded = "-" + str(workspace.resolve()).lstrip("/").replace("/", "-")
    proj_dir = tmp_home / ".claude" / "projects" / encoded
    if not proj_dir.is_dir():
        return None
    sessions = sorted(
        (p for p in proj_dir.iterdir() if p.suffix == ".jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return sessions[0] if sessions else None


def parse_claude_session(path: Path) -> tuple[list[dict], list[dict], dict | None]:
    """解析 claude session JSONL → (steps, traces, usage)。

    - user 消息：初始 prompt 记录为 last_user；tool_result 配对工具调用
    - assistant 消息：content 块 thinking→llm(reasoning)、tool_use→llm(decision)+step、
      text→llm(final)；usage 累计 token
    - 同一条 assistant 消息可能分多个 JSONL chunk（相同 message.id），需去重
    返回 (steps, traces, usage)，解析失败返回 ([], [], None)。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001
        return [], [], None

    steps: list[dict] = []
    traces: list[dict] = []
    last_user = ""
    model = ""
    step_no = 0
    pending: dict[str, int] = {}  # tool_use_id -> steps index
    seen_msg_ids: set[str] = set()
    total_input = 0
    total_output = 0
    found_usage = False

    for line in lines:
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        mtype = o.get("type")
        msg = o.get("message") or {}
        ts_str = o.get("timestamp", "")
        try:
            ts = 0.0
            if ts_str:
                from datetime import datetime
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            ts = 0.0

        if mtype == "user":
            content = msg.get("content")
            if isinstance(content, str):
                last_user = content[:300]
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        cid = c.get("tool_use_id") or ""
                        obs = c.get("content", "")
                        if isinstance(obs, list):
                            obs = "".join(
                                x.get("text", "") for x in obs if isinstance(x, dict) and x.get("type") == "text"
                            )
                        # 优先用 toolUseResult 中的详细结果
                        tur = o.get("toolUseResult") or {}
                        if tur:
                            if tur.get("stdout"):
                                obs = tur["stdout"]
                            elif tur.get("type") == "text" and tur.get("file"):
                                obs = tur["file"].get("content", obs)
                        if cid in pending:
                            steps[pending[cid]]["observation"] = str(obs)[:2000]
                            steps[pending[cid]]["ts"] = ts or steps[pending[cid]]["ts"]
                        traces.append({
                            "kind": "tool", "category": tool_category("bash"),
                            "ts": ts, "tool": "tool_result", "args": {},
                            "observation": str(obs)[:2000],
                        })

        elif mtype == "assistant":
            mid = msg.get("id") or ""
            if mid and mid in seen_msg_ids:
                continue  # 去重：同一 message 可能分多个 chunk
            if mid:
                seen_msg_ids.add(mid)
            model = msg.get("model") or model
            usage = msg.get("usage") or {}
            inp = usage.get("input_tokens") or 0
            out = usage.get("output_tokens") or 0
            if inp or out:
                total_input += int(inp)
                total_output += int(out)
                found_usage = True
            for c in (msg.get("content") or []):
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "thinking" and c.get("thinking", "").strip():
                    traces.append({
                        "kind": "llm", "ts": ts, "model": model, "input": last_user,
                        "output": c["thinking"][:2000], "phase": "reasoning",
                    })
                elif ct == "text" and c.get("text", "").strip():
                    traces.append({
                        "kind": "llm", "ts": ts, "model": model, "input": last_user,
                        "output": c["text"][:2000], "phase": "final",
                    })
                elif ct == "tool_use":
                    step_no += 1
                    name = c.get("name") or "tool"
                    inp_args = c.get("input") or {}
                    cid = c.get("id") or ""
                    pending[cid] = len(steps)
                    steps.append({
                        "step": step_no, "action": name, "args": inp_args,
                        "observation": "", "ts": ts,
                    })
                    traces.append({
                        "kind": "llm", "ts": ts, "model": model, "input": last_user,
                        "output": json.dumps(inp_args, ensure_ascii=False)[:500],
                        "tool": name, "phase": "decision",
                    })

    usage_out = {"prompt_tokens": total_input, "completion_tokens": total_output} if found_usage else None
    return steps, traces, usage_out


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
    default_model = "claude-opus-4-8"
    # 已验证可用的模型列表（costing.py 中有对应定价）
    SUPPORTED_MODELS = ("claude-opus-4-8", "claude-opus-4-5", "claude-sonnet-4-5")
    # 快速模式默认模型（性价比高，适合大规模回归测试）
    FAST_MODEL = "claude-sonnet-4-5"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: int = 300,
        cmd: str | None = None,
        allowed_tools: str | None = None,
        max_budget_usd: float = _DEFAULT_MAX_BUDGET_USD,
    ) -> None:
        # 模型选择优先级（从高到低）：
        # 1. 显式传入的 claude 模型
        # 2. AGENT_EVAL_CLAUDE_MODEL 环境变量
        # 3. AGENT_EVAL_CLAUDE_FAST=true 时使用 FAST_MODEL（sonnet，快速低成本）
        # 4. 后端 default_model（opus，高质量）
        # runner 可能传入全局默认 deepseek-chat（对 claude-code 无效），需自动回退
        env_model = os.environ.get("AGENT_EVAL_CLAUDE_MODEL")
        fast_mode = os.environ.get("AGENT_EVAL_CLAUDE_FAST", "").lower() in ("1", "true", "yes", "on")
        if model and model.startswith("claude"):
            self.model = model
        elif env_model:
            self.model = env_model
        elif fast_mode:
            self.model = self.FAST_MODEL
        else:
            self.model = self.default_model
        if model and not model.startswith("claude"):
            logger.info("claude-code 忽略非 claude 模型 '%s'，使用 %s", model, self.model)
        if self.model not in self.SUPPORTED_MODELS:
            logger.warning(
                "claude-code 模型 '%s' 不在已验证列表 %s 中，可能缺少定价配置或不兼容",
                self.model, self.SUPPORTED_MODELS,
            )
        if fast_mode and self.model == self.FAST_MODEL:
            logger.info("claude-code 快速模式已启用：使用 %s（低成本高吞吐）", self.FAST_MODEL)
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
        # 认证隔离：实测 claude 即使 --bare 也依赖 ~/.claude 里的状态，
        # 空/新配置目录（无论 CLAUDE_CONFIG_DIR 还是 HOME）都会返回 403。
        # 方案：复制用户 ~/.claude 的配置文件到临时 HOME，跳过 sessions/projects/backups
        # 等历史大目录，既保留认证状态又隔离写入，不污染用户真实配置。
        tmp_home = Path(tempfile.mkdtemp(prefix="agent-eval-claude-home-"))
        src = Path.home() / ".claude"
        if src.is_dir():
            dst = tmp_home / ".claude"
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                if item.name in ("sessions", "projects", "backups"):
                    continue
                if item.is_dir():
                    shutil.copytree(item, dst / item.name, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst / item.name)
        env["HOME"] = str(tmp_home)
        env.pop("CLAUDE_CONFIG_DIR", None)  # 绝不能设，否则 403

        logger.info(
            "claude-code 执行开始 | model=%s timeout=%ds budget=$%s",
            self.model, self.timeout_s, self.max_budget_usd,
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
            data = _extract_json(full)
            # 优先从 claude session JSONL 提取多轮步骤/轨迹/token（更完整）
            session_steps: list[dict] = []
            session_traces: list[dict] = []
            session_usage: dict | None = None
            try:
                sfile = _find_claude_session(tmp_home, workspace)
                if sfile:
                    session_steps, session_traces, session_usage = parse_claude_session(sfile)
            except Exception:  # noqa: BLE001 - session 解析非关键路径
                pass

            # steps：优先 session 多轮步骤，降级为 _parse_messages，再降级为黑盒单步
            steps, traces = _parse_messages(data)
            if session_steps:
                steps = session_steps
            if session_traces:
                traces = session_traces
            if not steps:
                steps = [
                    {"step": 1, "action": "claude", "args": {"model": self.model},
                     "observation": full[-3000:], "ts": round(time.time(), 3)}
                ]
            # usage：优先 session 累计（含所有轮次），降级为 JSON 单次
            usage = session_usage or _parse_usage(data)
            final = (
                data.get("message")
                or data.get("text")
                or data.get("final_text")
                or (data.get("result") if isinstance(data.get("result"), str) else "")
                or (isinstance(data.get("result"), dict) and data["result"].get("message"))
                or ""
            )
            if not traces and final:
                traces = [
                    {"kind": "llm", "ts": 0, "model": self.model, "input": task.description[:300],
                     "output": str(final)[:2000], "phase": "final"}
                ]

            # 判定完成：退出码 0，或退出码非零但提取到有效 JSON 结果（含 final/text/message）
            has_result = bool(final) or bool(data.get("result")) or bool(usage)
            ok = rc == 0 or (rc != 0 and has_result and bool(data))
            cost_usd = data.get("total_cost_usd") or data.get("cost")
            logger.info(
                "claude-code 完成 | status=%s rc=%d dur=%.1fs steps=%d traces=%d usage=%s cost=$%s session=%s",
                "completed" if ok else "error", rc, time.time() - start,
                len(steps), len(traces), usage, cost_usd,
                "yes" if session_steps else "no",
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
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)
