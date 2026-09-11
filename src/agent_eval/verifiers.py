"""确定性判定器（Day 3）：执行任务 spec 中的校验点，产出 verdict 列表。

支持 5 种校验点类型：
- file_exists / file_not_exists：path 存在性（支持 glob）
- content_contains / content_not_contains：path 内容匹配正则 pattern（支持 glob，作用于全部匹配文件）
- cmd_exit_zero：运行 cmd 返回码为 0；约定 "python @scripts/xxx.py ."，
  @scripts/ 前缀解析为项目 scripts/ 目录，"." 解析为工作目录
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from agent_eval.log import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def find_scripts_dir() -> Path:
    """定位校验脚本目录：优先 AGENT_EVAL_SCRIPTS 环境变量，其次 cwd/scripts，最后包默认。

    pip 安装后 REPO_ROOT 指向 site-packages 上层，包内不含 scripts/，
    因此 CI 场景须依赖 cwd（被测仓库根）或环境变量显式指定。
    """
    env = os.environ.get("AGENT_EVAL_SCRIPTS")
    if env:
        return Path(env)
    cwd = Path.cwd() / "scripts"
    if cwd.is_dir():
        return cwd
    return REPO_ROOT / "scripts"


_SCRIPTS_DIR = find_scripts_dir()


def run_checkpoints(task, workspace: Path) -> list[dict]:
    """对工作目录执行任务的全部校验点。"""
    return [_run_checkpoint(cp, workspace) for cp in task.checkpoints]


def _run_checkpoint(cp, workspace: Path) -> dict:
    name = cp.type
    if name in ("file_exists", "file_not_exists"):
        passed = _check_file(cp, workspace)
        detail = _label(cp, passed)
    elif name in ("content_contains", "content_not_contains"):
        passed = _check_content(cp, workspace)
        detail = _label(cp, passed)
    elif name == "cmd_exit_zero":
        passed, detail = _check_cmd(cp, workspace)
        detail = f"{cp.desc}：{detail}" if cp.desc else detail
    else:
        logger.warning("未知校验点类型: %s (id=%s)", name, cp.id)
        return {"id": cp.id, "type": name, "passed": False, "detail": f"未知校验点类型: {name}"}
    logger.debug("校验点 %s (%s): %s", cp.id, name, "PASS" if passed else "FAIL")
    return {"id": cp.id, "type": name, "passed": passed, "detail": detail}


def _label(cp, passed: bool) -> str:
    label = "通过" if passed else "未通过"
    return f"{cp.desc}：{label}" if cp.desc else label


def _resolve_paths(workspace: Path, rel: str) -> list[Path]:
    p = workspace / rel
    if any(ch in rel for ch in "*?["):
        if not p.parent.exists():
            return []
        return sorted(p.parent.glob(p.name))
    return [p]


def _check_file(cp, workspace: Path) -> bool:
    want_exists = cp.type == "file_exists"
    paths = _resolve_paths(workspace, cp.path)
    if not paths:
        return not want_exists
    # file_exists 检查路径是否存在（文件或目录均可），不强制 is_file
    # 因为任务 spec 中常常用 file_exists 检查输出目录是否创建
    if want_exists:
        return all(p.exists() for p in paths)
    # file_not_exists 要求路径不存在（文件或目录都算存在）
    return all(not p.exists() for p in paths)


def _check_content(cp, workspace: Path) -> bool:
    paths = _resolve_paths(workspace, cp.path)
    if not paths:
        # 无匹配文件：要求"包含"则失败，要求"不包含"则视为无残留
        return cp.type == "content_not_contains"
    pattern = re.compile(cp.pattern)
    want_hit = cp.type == "content_contains"
    for p in paths:
        if not p.is_file():
            return False
        text = p.read_text(encoding="utf-8", errors="replace")
        found = bool(pattern.search(text))
        if want_hit and not found:
            return False
        if not want_hit and found:
            return False
    return True


def _check_cmd(cp, workspace: Path) -> tuple[bool, str]:
    try:
        tokens = shlex.split(cp.cmd)
    except ValueError as e:
        return False, f"cmd 解析失败: {e}"

    argv = []
    for tok in tokens:
        if tok == ".":
            # 关键：传绝对路径，否则相对路径在 cwd=workspace 下解析错位
            argv.append(str(workspace.resolve()))
        elif tok.startswith("@scripts/"):
            argv.append(str(_SCRIPTS_DIR / tok[len("@scripts/") :]))
        elif tok == "python":
            argv.append(sys.executable)
        else:
            argv.append(tok)

    try:
        proc = subprocess.run(
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            tail = (proc.stdout or "") + (proc.stderr or "")
            return False, f"退出码 {proc.returncode}：{tail[:200]}"
        return True, "命令执行成功"
    except subprocess.TimeoutExpired:
        return False, "命令执行超时"
    except Exception as e:  # noqa: BLE001
        return False, f"命令执行异常: {e}"
