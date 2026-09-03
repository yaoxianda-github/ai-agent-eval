"""Aider 后端（Day 4）：调用开源 Aider CLI 在评测工作目录内完成任务。

与自建 ReAct（步骤级轨迹）不同，Aider 是"黑盒单次调用"型后端：
- 轨迹记录为一次 aider 调用的输出（可观测性不同，报告需注明口径）
- 依赖 git 追踪：run 时先在干净工作目录 git init + baseline commit
- 适配层：aider 只编辑显式加入对话的文件（--file），故自动扫描工作目录文本输入文件传入

依赖：pip install aider-chat（或 pip install -e ".[aider]"）
模型：默认 deepseek/deepseek-chat（LiteLLM 格式），API Key 走 DEEPSEEK_API_KEY。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from agent_eval.backends.base import Backend, BackendResult

# aider 能直接处理的文本类扩展名
_TEXT_EXTS = {".md", ".csv", ".txt", ".py", ".json", ".yaml", ".yml", ".html", ".css", ".js"}


def _collect_input_files(workspace: Path) -> list[str]:
    """扫描工作目录中的文本输入文件（排除 .git / output / aider 临时文件），返回相对路径。"""
    files: list[str] = []
    for p in workspace.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _TEXT_EXTS:
            continue
        rel = p.relative_to(workspace).as_posix()
        if rel.startswith((".git/", "output/", ".aider")):
            continue
        files.append(rel)
    return files


class AiderBackend(Backend):
    name = "aider"
    version = "0.1.0"

    def __init__(
        self,
        model: str = "deepseek-chat",
        api_key: str | None = None,
        timeout_s: int = 300,
    ) -> None:
        # aider 用 LiteLLM 模型格式（provider/model）；无斜杠时自动补 deepseek/ 前缀
        self.model = model if "/" in model else f"deepseek/{model}"
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
            "LLM_API_KEY"
        )
        self.timeout_s = timeout_s

    def run(self, task, workspace) -> BackendResult:
        if shutil.which("aider") is None:
            return BackendResult(
                status="error",
                error='未找到 aider 命令，请先运行 pip install aider-chat（或 pip install -e ".[aider]"）',
            )
        if not self.api_key:
            return BackendResult(status="error", error="缺少 DEEPSEEK_API_KEY")

        # 1) 初始化 git baseline（Aider 依赖 git 追踪）
        for args in (
            ("init", "-q"),
            ("config", "user.name", "aider-bot"),
            ("config", "user.email", "aider@local"),
            ("add", "."),
            ("commit", "-q", "-m", "baseline"),
        ):
            p = subprocess.run(
                ["git", *args],
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if p.returncode != 0:
                return BackendResult(
                    status="error", error=f"git {args[0]} 失败: {(p.stderr or '')[:300]}"
                )

        # 2) 调用 aider
        env = os.environ.copy()
        env.setdefault("DEEPSEEK_API_KEY", self.api_key)
        env.setdefault("AIDER_ANALYTICS", "False")

        # 适配层：aider 只编辑显式加入对话的文件（--file）。
        # 自动扫描工作目录里的文本输入文件，转成 --file 参数，避免其陷入"要不要新建文件"的循环。
        cmd = [
            "aider",
            "--message",
            task.description,
            "--model",
            self.model,
            "--no-pretty",
            "--no-stream",
            "--no-detect-urls",
            "--no-auto-commits",
            "--no-suggest-shell-commands",
        ]
        for rel in _collect_input_files(workspace):
            cmd += ["--file", rel]
        cmd += ["--yes-always", "--exit"]
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
            # 超时：kill 并保留部分输出用于诊断
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
                        "action": "aider",
                        "args": {"model": self.model},
                        "observation": partial,
                        "ts": round(time.time(), 3),
                    }
                ],
                duration_s=round(time.time() - start, 3),
                error=f"aider 超时（>{self.timeout_s}s）。输出尾部：{partial[:400]}",
            )

        full = (out or "") + (("\n" + err) if err else "")
        tail = full[-3000:]
        steps = [
            {
                "step": 1,
                "action": "aider",
                "args": {"model": self.model},
                "observation": tail,
                "ts": round(time.time(), 3),
            }
        ]
        return BackendResult(
            status="completed" if rc == 0 else "error",
            steps=steps,
            duration_s=round(time.time() - start, 3),
            stdout=tail,
            error="" if rc == 0 else f"aider 退出码 {rc}",
        )
