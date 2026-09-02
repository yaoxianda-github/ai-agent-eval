"""Agent 可用最小工具集（Day 2）。

每个工具函数签名统一为 (workspace, args) -> (ok, observation)。
安全性：路径用 _safe_path 限制在工作目录内；run_command 依赖沙箱隔离（MVP 用干净目录，
OS 级隔离在后续阶段用 Docker/VM 补齐）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_tool(tool: str, args: dict, workspace: Path) -> tuple[bool, str]:
    handlers = {
        "list_dir": _list_dir,
        "read_file": _read_file,
        "write_file": _write_file,
        "run_command": _run_command,
        "finish": lambda ws, a: (True, "任务结束"),
    }
    fn = handlers.get(tool)
    if fn is None:
        return False, f"未知工具: {tool}（可用: {', '.join(handlers)}）"
    try:
        return fn(workspace, args)
    except Exception as e:  # noqa: BLE001 - 观察信息要带回给 LLM
        return False, f"工具执行异常: {type(e).__name__}: {e}"


def _safe_path(workspace: Path, rel: str) -> Path:
    p = (workspace / rel).resolve()
    if not p.is_relative_to(workspace.resolve()):
        raise PermissionError(f"禁止访问工作目录之外: {rel}")
    return p


def _list_dir(ws: Path, args: dict) -> tuple[bool, str]:
    rel = args.get("path", ".")
    p = _safe_path(ws, rel)
    if not p.exists():
        return False, f"路径不存在: {rel}"
    entries = []
    for child in sorted(p.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        entries.append(f"{child.name} ({kind})")
    return True, "\n".join(entries) if entries else "(空目录)"


def _read_file(ws: Path, args: dict) -> tuple[bool, str]:
    rel = args.get("path", "")
    p = _safe_path(ws, rel)
    if not p.is_file():
        return False, f"文件不存在: {rel}"
    data = p.read_text(encoding="utf-8", errors="replace")
    return True, data[:4000]


def _write_file(ws: Path, args: dict) -> tuple[bool, str]:
    rel = args.get("path", "")
    content = args.get("content", "")
    p = _safe_path(ws, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return True, f"已写入 {rel}（{len(content)} 字符）"


def _run_command(ws: Path, args: dict) -> tuple[bool, str]:
    cmd = args.get("command", "")
    if not cmd:
        return False, "缺少 command"
    timeout = int(args.get("timeout", 30))
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out[:4000]
    return proc.returncode == 0, f"exit={proc.returncode}\n{out}"
