"""DeepSeek Harness（dsh）后端：以 DeepSeek 官方 Agent 运行框架作为被测黑盒对象。

DeepSeek Harness（https://github.com/deepseek-ai/deepseek-harness）是 DeepSeek AI
开源的 Agent harness（dsh，MIT，一切皆插件）。本后端通过其 headless 模式在评测
工作目录内一次性执行任务：

    dsh --profile headless "<task.description>"

- 工作目录 = 启动 dsh 时所在目录（即评测 workspace）
- 最终答案打印到 stdout；模型 reasoning 走 stderr
- 退出码 0 = 任务完成；1 = 中止 / 错误
- 与 aider 一样属于"黑盒单次调用"型后端：轨迹记为一次 dsh 调用的输出，报告需注明口径

依赖：Node.js + dsh（npm install -g @deepseek-ai/dsh，或回退 npx @deepseek-ai/dsh）
API Key：环境变量 DEEPSEEK_API_KEY / LLM_API_KEY（headless 默认模型走 DeepSeek）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult

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
            duration_s=round(time.time() - start, 3),
            stdout=(out or "").strip(),
            error="" if ok else f"dsh 退出码 {rc}：{(err or out or '')[-300:]}",
        )
