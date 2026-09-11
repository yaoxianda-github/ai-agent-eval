"""Trae Agent 后端（V1.0）：调用字节跳动 Trae Agent CLI 非交互模式执行评测任务。

Trae Agent（https://github.com/bytedance/TRAE-agent）是字节跳动开源的命令行 Code Agent，
对标 Claude Code，支持多种 LLM provider。本后端通过其 headless 模式在评测工作目录内一次性执行任务：

    trae-cli -p "<task.description>" --output-format stream-json --yolo --allowed-tool Bash,Edit,Write,Read,Glob,Grep

关键设计：
- 工作目录 = 启动 trae-cli 时所在目录（即评测 workspace）
- -p/--print：非交互模式，打印响应并退出
- --output-format stream-json：流式 JSON 输出，含每轮的工具调用和结果
- --yolo：绕过工具权限检查（评测隔离环境）
- --allowed-tool：自动批准的工具列表
- --add-dir：允许 agent 访问的额外目录
- 退出码 0 = 完成
- 轨迹：stream-json 输出含 tool_call/tool_result 事件，解析为步骤级 steps 与回放 traces
- session 目录：~/.trae/sessions/，可提取更完整的多轮轨迹

依赖：Trae Agent CLI 已安装（sh -c "$(curl -L https://trae.cn/trae-cli/install.sh)"）
认证：需配置 ~/.trae/trae_cli.yaml 中的 model 和 provider（支持 OpenAI/Anthropic/DeepSeek 等）
模型：见 SUPPORTED_MODELS（需在配置中指定，支持多种 provider 的模型）
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

# Trae Agent CLI 常见安装路径
_DEFAULT_TRAE_CANDIDATES = (
    str(Path.home() / ".local" / "bin" / "trae-cli"),
    "/usr/local/bin/trae-cli",
    "/opt/homebrew/bin/trae-cli",
)
# 评测默认工具白名单：文件操作 + 命令 + 检索
_DEFAULT_ALLOWED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,List,Replace,MultiEdit"


def _find_trae_cmd() -> str | None:
    """探测 trae-cli 命令：环境变量 TRAE_CMD > PATH > 常见安装路径。"""
    env_cmd = os.environ.get("TRAE_CMD")
    if env_cmd:
        return env_cmd
    which = shutil.which("trae-cli")
    if which:
        return which
    which2 = shutil.which("trae")
    if which2:
        return which2
    for cand in _DEFAULT_TRAE_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _parse_stream_json(text: str) -> list[dict]:
    """从 trae-cli --output-format stream-json 输出中提取 JSON 事件。

    stream-json 输出是每行一个 JSON 对象（NDJSON），但可能有前缀日志文本。
    处理场景：
    - 前缀非 JSON 文本（日志/警告）
    - 每行一个 JSON 对象
    - 输出截断（跳过不完整的行）
    """
    if not text:
        return []

    events: list[dict] = []
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


def _parse_trae_events(events: list[dict]) -> tuple[list[dict], list[dict], dict | None, str]:
    """解析 Trae Agent stream-json 事件 → (steps, traces, usage, final_text)。

    Trae Agent 的 stream-json 事件类型（基于 Claude Code 兼容设计）：
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


class TraeAgentBackend(Backend):
    """Trae Agent CLI 后端（字节跳动）。"""

    name = "trae-agent"
    version = "1.0.0"
    default_model = "claude-opus-4-8"
    # Trae Agent 支持多种 provider 的模型，这里列出常见的
    # 实际可用模型取决于 ~/.trae/trae_cli.yaml 中的配置
    SUPPORTED_MODELS = (
        "claude-opus-4-8",
        "claude-sonnet-4-5",
        "deepseek-chat",
        "deepseek-reasoner",
        "gpt-5",
        "gpt-4o",
        "kimi-k2",
        "glm-5.1",
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
        # 2. AGENT_EVAL_TRAE_MODEL 环境变量
        # 3. AGENT_EVAL_TRAE_FAST=true 时使用 FAST_MODEL
        # 4. 后端 default_model
        env_model = os.environ.get("AGENT_EVAL_TRAE_MODEL")
        fast_mode = os.environ.get("AGENT_EVAL_TRAE_FAST", "").lower() in ("1", "true", "yes", "on")
        if model and model in self.SUPPORTED_MODELS:
            self.model = model
        elif env_model:
            self.model = env_model
        elif fast_mode:
            self.model = self.FAST_MODEL
        else:
            self.model = self.default_model
        if model and model not in self.SUPPORTED_MODELS and model:
            logger.info("trae-agent 模型 '%s' 不在已验证列表中，使用默认 %s", model, self.model)
        # Trae Agent CLI 认证：需配置 ~/.trae/trae_cli.yaml 中的 model 和 provider
        # 也支持通过环境变量传递 API Key（如 OPENAI_API_KEY, ANTHROPIC_API_KEY, DEEPSEEK_API_KEY）
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.cmd = cmd or _find_trae_cmd()
        self.allowed_tools = allowed_tools or _DEFAULT_ALLOWED_TOOLS

    def run(self, task, workspace) -> BackendResult:
        if not self.cmd:
            return BackendResult(
                status="error",
                error=(
                    "未找到 trae-cli 命令，请先安装："
                    "sh -c \"$(curl -L https://trae.cn/trae-cli/install.sh)\""
                    "（或设置 TRAE_CMD 环境变量）"
                ),
            )

        cmd = [
            self.cmd,
            "-p", task.description,
            "--output-format", "stream-json",
            "--yolo",
            "--allowed-tool", self.allowed_tools,
            "--add-dir", str(workspace),
            "--query-timeout", f"{self.timeout_s}s",
            "--bash-tool-timeout", "120s",
            "-c", f"model.name={self.model}",
        ]

        env = os.environ.copy()
        # 传递常见 API Key 环境变量
        if self.api_key:
            # 根据模型类型推断使用哪个 API Key 环境变量
            if "claude" in self.model.lower():
                env["ANTHROPIC_API_KEY"] = self.api_key
            elif "gpt" in self.model.lower() or "o1" in self.model.lower() or "o3" in self.model.lower():
                env["OPENAI_API_KEY"] = self.api_key
            elif "deepseek" in self.model.lower():
                env["DEEPSEEK_API_KEY"] = self.api_key
            elif "kimi" in self.model.lower() or "moonshot" in self.model.lower():
                env["MOONSHOT_API_KEY"] = self.api_key
            else:
                env["TRAE_API_KEY"] = self.api_key
        # 禁用遥测
        env.setdefault("TRAE_TELEMETRY_DISABLED", "1")
        # Trae Agent CLI 配置目录隔离
        tmp_home = Path(tempfile.mkdtemp(prefix="agent-eval-trae-home-"))
        src = Path.home() / ".trae"
        if src.is_dir():
            dst = tmp_home / ".trae"
            dst.mkdir(parents=True, exist_ok=True)
            skip_dirs = {"sessions", "logs", "cache", "tmp"}
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

        # 创建/更新配置文件，定义模型列表（trae-cli 需要 models 列表才能工作）
        trae_config_dir = tmp_home / ".trae"
        trae_config_dir.mkdir(parents=True, exist_ok=True)
        config_path = trae_config_dir / "trae_cli.yaml"
        # 根据模型类型生成 provider 配置
        model_lower = self.model.lower()
        if "claude" in model_lower:
            provider_config = f"""    claude:
      model: "{self.model}"
      api_key: "${{ANTHROPIC_API_KEY}}"
      max_tokens: 8192"""
        elif "gpt" in model_lower or "o1" in model_lower or "o3" in model_lower or "o4" in model_lower:
            provider_config = f"""    open_ai:
      model: "{self.model}"
      api_key: "${{OPENAI_API_KEY}}"
      max_tokens: 16384"""
        elif "deepseek" in model_lower:
            provider_config = f"""    open_ai:
      model: "{self.model}"
      api_key: "${{DEEPSEEK_API_KEY}}"
      base_url: "https://api.deepseek.com/v1"
      max_tokens: 8192"""
        elif "kimi" in model_lower or "moonshot" in model_lower:
            provider_config = f"""    open_ai:
      model: "{self.model}"
      api_key: "${{MOONSHOT_API_KEY}}"
      base_url: "https://api.moonshot.cn/v1"
      max_tokens: 8192"""
        else:
            provider_config = f"""    open_ai:
      model: "{self.model}"
      api_key: "${{TRAE_API_KEY}}"
      max_tokens: 8192"""

        config_content = f"""# agent-eval 自动生成的 trae-cli 配置
model:
  name: "{self.model}"

models:
  - name: "{self.model}"
    description: "{self.model} (agent-eval)"
{provider_config}
"""
        # 如果已有配置文件，合并模型配置（保留原有配置，追加/覆盖 model 和 models）
        if config_path.exists():
            try:
                existing = config_path.read_text(encoding="utf-8")
                # 简单处理：如果已有 model/models 配置，追加我们的模型到 models 列表
                if "models:" in existing and "model:" in existing:
                    # 在 models 列表末尾追加我们的模型
                    existing += f"\n  - name: \"{self.model}\"\n    description: \"{self.model} (agent-eval)\"\n{provider_config}\n"
                    # 更新 model.name
                    import re
                    existing = re.sub(r'model:\s*\n\s*name:\s*"[^"]*"', f'model:\n  name: "{self.model}"', existing)
                    config_content = existing
                else:
                    config_content = existing + "\n" + config_content
            except Exception:
                pass
        config_path.write_text(config_content, encoding="utf-8")

        logger.info(
            "trae-agent 执行开始 | model=%s timeout=%ds config=%s",
            self.model, self.timeout_s, config_path,
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
                logger.warning("trae-agent 超时 (>%ds)，已 kill", self.timeout_s)
                try:
                    out, err = proc.communicate(timeout=10)
                except Exception:
                    out, err = "", ""
                partial = ((out or "") + (("\n" + err) if err else ""))[-2000:]
                return BackendResult(
                    status="timeout",
                    steps=[
                        {"step": 1, "action": "trae-agent", "args": {"model": self.model},
                         "observation": partial, "ts": round(time.time(), 3)}
                    ],
                    duration_s=round(time.time() - start, 3),
                    error=f"trae-agent 超时（>{self.timeout_s}s）。输出尾部：{partial[:400]}",
                )

            full = (out or "") + (("\n" + err) if err else "")
            events = _parse_stream_json(full)
            steps, traces, usage, final_text = _parse_trae_events(events)

            if not steps:
                steps = [
                    {"step": 1, "action": "trae-agent", "args": {"model": self.model},
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
                "trae-agent 完成 | status=%s rc=%d dur=%.1fs steps=%d traces=%d usage=%s events=%d",
                "completed" if ok else "error", rc, time.time() - start,
                len(steps), len(traces), usage, len(events),
            )
            if not ok:
                logger.warning("trae-agent 未正常完成: 退出码 %d，stderr 尾部: %s", rc, (err or "")[-300:])
            return BackendResult(
                status="completed" if ok else "error",
                steps=steps,
                traces=traces,
                usage=usage,
                duration_s=round(time.time() - start, 3),
                stdout=str(final_text) if final_text else (out or "")[-1000:],
                error="" if ok else f"trae-agent 退出码 {rc}：{(err or out or '')[-400:]}",
            )
        finally:
            shutil.rmtree(tmp_home, ignore_errors=True)
