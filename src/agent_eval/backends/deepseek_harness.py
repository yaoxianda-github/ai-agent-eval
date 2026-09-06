"""DeepSeek Harness（dsh）后端：以 DeepSeek 官方 Agent 运行框架作为被测黑盒对象。

DeepSeek Harness（https://github.com/deepseek-ai/deepseek-harness）是 DeepSeek AI
开源的 Agent harness（dsh，MIT，一切皆插件）。本后端通过其 headless 模式在评测
工作目录内一次性执行任务：

    dsh --profile headless "<task.description>"

- 工作目录 = 启动 dsh 时所在目录（即评测 workspace）
- 最终答案打印到 stdout；模型 reasoning 走 stderr
- 退出码 0 = 任务完成；1 = 中止 / 错误
- 轨迹：除 dsh 单步调用外，V2.4 起解析隔离 DSH_HOME 下的 session.jsonl.zstd，
  提取模型 reasoning / 工具决策（llm 节点）与工具执行结果（tool 节点），
  为黑盒后端补齐"模型生成"层回放数据

依赖：Node.js + dsh（npm install -g @deepseek-ai/dsh，或回退 npx @deepseek-ai/dsh）
API Key：环境变量 DEEPSEEK_API_KEY / LLM_API_KEY（headless 默认模型走 DeepSeek）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.traces import tool_category

_DEFAULT_DSH_CANDIDATES = (
    "/usr/local/bin/dsh",
    "/opt/homebrew/bin/dsh",
)


def _find_dsh_cmd() -> str | None:
    """探测 dsh 命令：环境变量 DSH_CMD > PATH 中的 dsh > 常见安装路径 > npx 回退。"""
    env_cmd = os.environ.get("DSH_CMD")
    if env_cmd:
        return env_cmd
    which = shutil.which("dsh")
    if which:
        return which
    for cand in _DEFAULT_DSH_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _default_dsh_home() -> Path:
    """评测专用的隔离 DSH_HOME（缓存到用户 cache 目录）。

    不能用 ~/.dsh：那可能带用户个人凭据/配置（实测旧凭据会导致 dsh 403
    预扣失败，且不可控）。隔离 home 首次运行自动初始化 headless profile，
    之后复用；凭据完全来自环境变量 DEEPSEEK_API_KEY，干净可控。
    """
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "agent-eval" / "dsh-home"


def _ensure_headless_profile(dsh_home: Path, cmd: str) -> None:
    """确保隔离 home 已初始化 headless profile（幂等）。"""
    marker = dsh_home / "profiles" / "headless" / "cordis.yml"
    if marker.is_file():
        return
    dsh_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["DSH_HOME"] = str(dsh_home)
    subprocess.run(
        [cmd, "--profile", "headless", "--dump-default-config"],
        cwd=str(dsh_home),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=120,
        check=False,
    )


# ---------- dsh session 解析（V2.4：黑盒后端补模型层回放数据） ----------

def _session_dir_for(workspace: Path, dsh_home: Path) -> Path | None:
    """按 workspace 绝对路径的编码目录名定位本次 dsh session 目录。

    dsh 把 session 存在 <DSH_HOME>/sessions/<"--" + abs_path.replace("/","-") + "--">/
    下，同一 workspace 多次运行会产生多个 session-<uuid> 子目录，取最新一个。
    """
    encoded = "--" + str(workspace.resolve()).lstrip("/").replace("/", "-") + "--"
    d = dsh_home / "sessions" / encoded
    if not d.is_dir():
        return None
    subs = sorted(
        (p for p in d.iterdir() if p.is_dir() and p.name.startswith("session-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return subs[0] if subs else None


def _decompress_zstd(path: Path) -> bytes | None:
    """解压 .zstd：优先 python zstandard 模块，回退 zstd CLI；都不可用返回 None。"""
    try:
        import zstandard as zstd

        with open(path, "rb") as f:
            return b"".join(zstd.ZstdDecompressor().stream_reader(f).read())
    except Exception:  # noqa: BLE001 - 模块缺失/解压失败都回退 CLI
        pass
    zc = shutil.which("zstd")
    if zc:
        try:
            r = subprocess.run([zc, "-dc", str(path)], capture_output=True, timeout=30)
            return r.stdout if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None
    return None


def parse_dsh_session(path: Path) -> list[dict]:
    """解析 session.jsonl.zstd → 回放 traces（llm / tool 节点）。

    - assistant/message.content 块：
      reasoning → llm 节点（phase=reasoning）；tool-call → llm 节点（phase=decision，
      输出为模型生成的工具参数）；text → llm 节点（phase=final）
    - tool/result → tool 节点（含 callId 与观察结果；retrieval 分类沿用统一规则）
    解析失败（无解压器/格式变化/文件损坏）返回 []，不影响评测本身。
    """
    raw = _decompress_zstd(path)
    if not raw:
        return []
    traces: list[dict] = []
    last_user = ""
    model = ""
    call_names: dict[str, str] = {}
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    for line in text.splitlines():
        try:
            o = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        t = o.get("type")
        data = o.get("data") or {}
        ts = round((o.get("time") or 0) / 1000.0, 3)
        try:
            if t == "user/message":
                last_user = "".join(
                    c.get("text", "") for c in (data.get("content") or []) if c.get("type") == "text"
                )[:300]
            elif t == "tool/call":
                call_names[data.get("callId") or ""] = data.get("name") or ""
            elif t == "assistant/message":
                msg = data.get("message") or {}
                src = msg.get("source") or {}
                model = src.get("model") or model
                for c in (msg.get("content") or []):
                    ct = c.get("type")
                    text_c = c.get("text") or ""
                    if ct == "reasoning" and text_c.strip():
                        traces.append(
                            {"kind": "llm", "ts": ts, "model": model, "input": last_user,
                             "output": text_c, "phase": "reasoning"}
                        )
                    elif ct == "tool-call":
                        traces.append(
                            {"kind": "llm", "ts": ts, "model": model, "input": last_user,
                             "output": c.get("arguments") or "", "tool": c.get("name") or "",
                             "phase": "decision"}
                        )
                    elif ct == "text" and text_c.strip():
                        traces.append(
                            {"kind": "llm", "ts": ts, "model": model, "input": last_user,
                             "output": text_c, "phase": "final"}
                        )
            elif t == "tool/result":
                msg = data.get("message") or {}
                src = msg.get("source") or {}
                tool_name = call_names.get(src.get("callId") or "", "")
                args = src.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:  # noqa: BLE001
                        args = {"raw": args}
                obs = ""
                for c in (msg.get("content") or []):
                    if c.get("type") == "tool-result":
                        obs = "".join(
                            x.get("text", "") for x in (c.get("content") or []) if x.get("type") == "text"
                        )
                traces.append(
                    {"kind": "tool", "category": tool_category(tool_name), "ts": ts,
                     "tool": tool_name or "tool", "args": args, "observation": obs}
                )
        except Exception:  # noqa: BLE001 - 单条事件解析失败不影响其余
            continue
    return traces


class DeepseekHarnessBackend(Backend):
    name = "deepseek-harness"
    version = "0.1.0"

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str | None = None,
        timeout_s: int = 300,
        cmd: str | None = None,
        dsh_home: str | None = None,
        max_steps: int | None = None,
    ) -> None:
        # model 保留接口：dsh 的模型通过其 profile 配置选择，headless 用默认模型
        # max_steps 保留接口：dsh 黑盒无步数概念，由 dsh 自身循环控制
        self.model = model
        self.max_steps = max_steps
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "LLM_API_KEY"
        )
        self.timeout_s = timeout_s
        self.cmd = cmd or _find_dsh_cmd()
        self.dsh_home = Path(dsh_home) if dsh_home else _default_dsh_home()

    def _traces_from_session(self, workspace: Path) -> list[dict]:
        """解析本次运行的 dsh session（隔离 DSH_HOME）→ 回放 traces。

        黑盒后端补"模型生成"层：reasoning / 工具决策（llm 节点）+ 工具执行（tool 节点）。
        解析失败（无 session/zstd/格式变化）返回 []，不影响评测。
        """
        try:
            sdir = _session_dir_for(workspace, self.dsh_home)
            if not sdir:
                return []
            sfile = sdir / "session.jsonl.zstd"
            if not sfile.is_file():
                return []
            return parse_dsh_session(sfile)
        except Exception:  # noqa: BLE001 - 回放数据非关键路径
            return []

    def run(self, task, workspace) -> BackendResult:
        if not self.cmd:
            return BackendResult(
                status="error",
                error='未找到 dsh 命令，请先安装：npm install -g @deepseek-ai/dsh（或设置 DSH_CMD）',
            )
        if not self.api_key:
            return BackendResult(status="error", error="缺少 DEEPSEEK_API_KEY")

        # 隔离 DSH_HOME：避免 ~/.dsh 用户凭据污染（实测旧凭据导致 403 预扣失败）
        _ensure_headless_profile(self.dsh_home, self.cmd)

        # dsh headless：一条一次性任务，退出码 0=完成 / 1=中止或错误
        cmd = [self.cmd, "--profile", "headless", task.description]
        env = os.environ.copy()
        env.setdefault("DEEPSEEK_API_KEY", self.api_key)
        env["DSH_HOME"] = str(self.dsh_home)

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
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                out, err = "", ""
            partial = ((out or "") + (("\n" + err) if err else ""))[-2000:]
            return BackendResult(
                status="timeout",
                steps=[
                    {
                        "step": 1,
                        "action": "dsh",
                        "args": {"profile": "headless", "model": self.model},
                        "observation": partial,
                        "ts": round(time.time(), 3),
                    }
                ],
                traces=self._traces_from_session(workspace),
                duration_s=round(time.time() - start, 3),
                error=f"dsh 超时（>{self.timeout_s}s）。输出尾部：{partial[:400]}",
            )

        full = (out or "") + (("\n" + err) if err else "")
        tail = full[-3000:]
        steps = [
            {
                "step": 1,
                "action": "dsh",
                "args": {"profile": "headless", "model": self.model},
                "observation": tail,
                "ts": round(time.time(), 3),
            }
        ]
        # 退出码 0 且 stdout 有最终答案 → completed；其余按 error 记录（含 dsh 错误码）
        ok = rc == 0 and (out or "").strip() != ""
        return BackendResult(
            status="completed" if ok else "error",
            steps=steps,
            traces=self._traces_from_session(workspace),
            duration_s=round(time.time() - start, 3),
            stdout=(out or "").strip(),
            error="" if ok else f"dsh 退出码 {rc}：{(err or out or '')[-300:]}",
        )
