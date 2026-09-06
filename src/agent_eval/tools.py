"""Agent 可用最小工具集（Day 2；V2.0-Day2 接入命令沙箱）。

每个工具函数签名统一为 (workspace, args) -> (ok, observation)。
安全性：路径用 _safe_path 限制在工作目录内；run_command 经 sandbox 子进程执行
（超时强制终止 + 输出截断），OS 级隔离后续用 Docker/VM 补齐。
"""

from __future__ import annotations

from pathlib import Path

from agent_eval.sandbox import run_command_sandboxed


def run_tool(tool: str, args: dict, workspace: Path) -> tuple[bool, str]:
    handlers = {
        "list_dir": _list_dir,
        "read_file": _read_file,
        "write_file": _write_file,
        "run_command": _run_command,
        "search_kb": _search_kb,
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
    max_out = int(args.get("max_output_chars", 65536))
    r = run_command_sandboxed(cmd, ws, timeout_s=timeout, max_output_chars=max_out)
    if r.timed_out:
        return False, r.error
    out = r.stdout + (("\n" + r.stderr) if r.stderr else "")
    out = out[:4000]
    return r.ok, f"exit={r.exit_code}\n{out}"


# ---------- RAG 检索工具（V2.4：评测集真实检索环节） ----------

_KB_EXTS = {".md", ".txt", ".csv"}


def _extract_keywords(query: str) -> list[str]:
    """从查询中提取检索关键词：2+ 连续中文字符、英文单词（≥3 字母）。"""
    import re

    kws: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z0-9_]{3,}", query):
        if len(seg) >= 2:
            kws.append(seg)
    return kws


def _search_kb(ws: Path, args: dict) -> tuple[bool, str]:
    """在知识库 kb/ 下做关键词检索（RAG 检索环节的评测实现）。

    检索范围：kb/ 下所有 .md/.txt/.csv 文件。按 ~6 行一个片段打分
    （英文词命中×2、中文词组命中×1），返回 Top-N 片段，附来源定位
    `[kb/xxx.md:起-止行]`，供轨迹回放"知识/检索"节点展示命中的知识片段。
    """
    query = str(args.get("query", "")).strip()
    top_k = max(1, min(int(args.get("top_k", 3)), 5))
    if not query:
        return False, "缺少 query 参数"
    kb = ws / "kb"
    if not kb.is_dir():
        return False, "知识库 kb/ 不存在：本任务没有可检索的知识库（fixtures 未提供 kb/）"
    kws = _extract_keywords(query)
    if not kws:
        return False, f"无法从查询提取检索关键词: {query!r}"

    files = sorted(
        p for p in kb.rglob("*")
        if p.is_file() and p.suffix.lower() in _KB_EXTS
    )
    if not files:
        return False, "知识库 kb/ 下没有可检索的文档（.md/.txt/.csv）"

    scored: list[tuple[float, str, int, int, str]] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for start in range(0, len(lines), 6):
            chunk = lines[start : start + 6]
            block = "\n".join(chunk)
            score = 0.0
            for kw in kws:
                cnt = block.count(kw)
                if cnt:
                    score += cnt * (2.0 if kw.isascii() else 1.0)
            if score > 0:
                scored.append((score, str(p.relative_to(ws)), start + 1, start + len(chunk), block))

    if not scored:
        return False, f"知识库中未检索到与「{query}」相关的内容（关键词: {', '.join(kws)}）"

    scored.sort(key=lambda x: -x[0])
    out = []
    for score, rel, s, e, block in scored[:top_k]:
        rel = rel.replace("kb/", "kb/", 1)
        head = f"[{rel}:{s}-{e}] (得分 {score:.1f})"
        snippet = block[:600]
        out.append(f"{head}\n{snippet}")
    return True, "\n\n".join(out)
