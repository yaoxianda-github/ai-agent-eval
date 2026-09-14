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


def run_checkpoints(task, workspace: Path, traces: list[dict] | None = None) -> list[dict]:
    """对工作目录执行任务的全部校验点。traces 用于 tool_call_assert 类型校验。"""
    return [_run_checkpoint(cp, workspace, traces) for cp in task.checkpoints]


def _run_checkpoint(cp, workspace: Path, traces: list[dict] | None = None) -> dict:
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
    elif name == "ui_element_exists":
        passed, detail = _check_ui_element(cp)
        detail = f"{cp.desc}：{detail}" if cp.desc else detail
    elif name == "browser_url_contains":
        passed, detail = _check_browser_url(cp)
        detail = f"{cp.desc}：{detail}" if cp.desc else detail
    elif name == "http_status":
        passed, detail = _check_http_status(cp)
        detail = f"{cp.desc}：{detail}" if cp.desc else detail
    elif name == "tool_call_assert":
        passed, detail = _check_tool_call_assert(cp, traces or [])
        detail = f"{cp.desc}：{detail}" if cp.desc else detail
    else:
        logger.warning("未知校验点类型: %s (id=%s)", name, cp.id)
        return {"id": cp.id, "type": name, "passed": False, "detail": f"未知校验点类型: {name}", "stage": getattr(cp, "stage", "final_answer")}
    logger.debug("校验点 %s (%s): %s", cp.id, name, "PASS" if passed else "FAIL")
    return {"id": cp.id, "type": name, "passed": passed, "detail": detail, "stage": getattr(cp, "stage", "final_answer")}


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


# ============================================================
# M4：RPA/UI 操作评测的 checkpoint 实现
# ============================================================

def _get_browser_env():
    """懒加载浏览器环境（避免无 UI checkpoint 时启动浏览器）。"""
    from agent_eval.browser_env import get_browser_env
    return get_browser_env()


def _check_ui_element(cp) -> tuple[bool, str]:
    """检查 URL 页面中是否存在 CSS 选择器匹配的元素。

    cp.path = 目标 URL
    cp.pattern = CSS 选择器
    """
    if not cp.path:
        return False, "缺少 URL（path 字段）"
    if not cp.pattern:
        return False, "缺少 CSS 选择器（pattern 字段）"
    try:
        env = _get_browser_env()
        return env.element_exists(cp.path, cp.pattern)
    except ImportError:
        return False, "Playwright 未安装，无法执行 UI 校验"
    except Exception as e:  # noqa: BLE001
        return False, f"UI 元素校验异常: {e}"


def _check_browser_url(cp) -> tuple[bool, str]:
    """导航到起始 URL，检查最终 URL 是否包含 pattern。

    cp.path = 起始 URL
    cp.pattern = 期望 URL 包含的字符串
    """
    if not cp.path:
        return False, "缺少起始 URL（path 字段）"
    if not cp.pattern:
        return False, "缺少期望 URL 模式（pattern 字段）"
    try:
        env = _get_browser_env()
        return env.url_contains(cp.path, cp.pattern)
    except ImportError:
        return False, "Playwright 未安装，无法执行 URL 校验"
    except Exception as e:  # noqa: BLE001
        return False, f"浏览器 URL 校验异常: {e}"


def _check_http_status(cp) -> tuple[bool, str]:
    """检查 URL 的 HTTP 状态码是否匹配 pattern。

    cp.path = 目标 URL
    cp.pattern = 状态码模式（"200"、"2"、"200-299"），默认 "2"
    """
    if not cp.path:
        return False, "缺少 URL（path 字段）"
    pattern = cp.pattern or "2"
    try:
        env = _get_browser_env()
        return env.http_status(cp.path, pattern)
    except ImportError:
        return False, "Playwright 未安装，无法执行 HTTP 状态校验"
    except Exception as e:  # noqa: BLE001
        return False, f"HTTP 状态校验异常: {e}"


def _check_tool_call_assert(cp, traces: list[dict]) -> tuple[bool, str]:
    """V3.8 P1：参数级 checkpoint——断言工具调用的参数值。

    cp.tool = 工具名（必填）
    cp.param = 参数名（可选，不填则只检查工具是否被调用）
    cp.pattern = 参数值的正则表达式（可选，支持值域判断如 ">=\\s*\\d+"）

    从运行轨迹 traces 中提取工具调用，检查是否有指定工具的调用，
    且参数值匹配 pattern。支持非确定性参数的值域判断（正则而非字符串相等）。
    """
    if not cp.tool:
        return False, "缺少工具名（tool 字段）"

    # 从 traces 中提取指定工具的所有调用
    tool_calls = []
    for t in traces:
        tool_name = t.get("tool") or t.get("action") or ""
        if tool_name == cp.tool:
            args = t.get("args") or {}
            tool_calls.append({"ts": t.get("ts", 0), "args": args})

    if not tool_calls:
        return False, f"未调用工具 {cp.tool}（共 {len(traces)} 条轨迹）"

    # 如果没有指定参数名，只检查工具是否被调用
    if not cp.param:
        return True, f"工具 {cp.tool} 被调用 {len(tool_calls)} 次"

    # 检查参数值
    if not cp.pattern:
        # 没有 pattern，只检查参数是否存在
        for call in tool_calls:
            if cp.param in call["args"]:
                return True, f"工具 {cp.tool} 的参数 {cp.param} 存在（值: {call['args'][cp.param]})"
        return False, f"工具 {cp.tool} 被调用 {len(tool_calls)} 次，但参数 {cp.param} 不存在"

    # 用正则匹配参数值（值域判断）
    try:
        pattern = re.compile(cp.pattern)
    except re.error as e:
        return False, f"pattern 正则解析失败: {e}"

    matched_calls = []
    for call in tool_calls:
        param_val = call["args"].get(cp.param)
        if param_val is None:
            continue
        param_str = str(param_val)
        if pattern.search(param_str):
            matched_calls.append(call)

    if matched_calls:
        val = matched_calls[0]["args"].get(cp.param)
        return True, f"参数 {cp.param}={val} 匹配 pattern（{len(matched_calls)}/{len(tool_calls)} 次调用匹配）"
    else:
        all_vals = [str(c["args"].get(cp.param, "N/A")) for c in tool_calls]
        return False, f"参数 {cp.param} 未匹配 pattern，实际值: {', '.join(all_vals[:3])}"
