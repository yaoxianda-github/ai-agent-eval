"""命令沙箱（V2.0-Day2）：在子进程中执行命令，带超时与输出上限。

目的：防止失控命令 / 大输出拖垮评测进程。
Windows 兼容方案（无法用 POSIX resource 限制内存/CPU）：
- 超时强制终止（尽量杀进程树，避免遗留子进程）
- stdout/stderr 重定向到临时文件，只回读并截断，避免大输出占满内存
- 命令在指定工作目录（cwd）内运行
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CommandResult:
    ok: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""


def run_command_sandboxed(
    cmd: str,
    cwd: Path | str,
    *,
    timeout_s: float = 60.0,
    max_output_chars: int = 65536,
) -> CommandResult:
    """在沙箱子进程中执行 shell 命令，返回结构化结果。

    - cwd：命令运行目录
    - timeout_s：墙钟超时，超时即终止
    - max_output_chars：回读 stdout/stderr 的最大字符数（超出截断）
    """
    cwd = Path(cwd)
    tmp = Path(tempfile.mkdtemp(prefix="agent-eval-sbox-"))
    try:
        out_path = tmp / "stdout.log"
        err_path = tmp / "stderr.log"
        try:
            with out_path.open("w", encoding="utf-8", errors="replace") as fo, err_path.open(
                "w", encoding="utf-8", errors="replace"
            ) as fe:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=str(cwd),
                    stdout=fo,
                    stderr=fe,
                    stdin=subprocess.DEVNULL,
                )
                try:
                    proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    _terminate(proc)
                    return CommandResult(
                        ok=False,
                        exit_code=-1,
                        stdout=_read_truncated(out_path, max_output_chars),
                        stderr=_read_truncated(err_path, max_output_chars),
                        timed_out=True,
                        error=f"命令超时（>{timeout_s}s），已终止",
                    )
            return CommandResult(
                ok=proc.returncode == 0,
                exit_code=proc.returncode if proc.returncode is not None else -1,
                stdout=_read_truncated(out_path, max_output_chars),
                stderr=_read_truncated(err_path, max_output_chars),
            )
        except Exception as e:  # noqa: BLE001 - 需要把异常带回调用方
            return CommandResult(
                ok=False, exit_code=-1, error=f"命令执行异常: {type(e).__name__}: {e}"
            )
    finally:
        _rmtree_retry(tmp)


def _rmtree_retry(path: Path, attempts: int = 3, delay: float = 0.3) -> None:
    """删除临时目录；Windows 下子进程句柄可能延迟释放，重试 + 忽略失败（避免残留阻塞）。"""
    for _ in range(attempts):
        try:
            shutil.rmtree(path, ignore_errors=True)
            return
        except Exception:  # noqa: BLE001
            time.sleep(delay)


def _terminate(proc: subprocess.Popen) -> None:
    """尽力终止子进程及其进程树。"""
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        import os
        import signal

        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _read_truncated(path: Path, max_chars: int) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[已截断，超出 {max_chars} 字符]"
    return text
