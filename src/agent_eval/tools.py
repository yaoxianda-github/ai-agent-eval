"""Agent 可用最小工具集（Day 2；V2.0-Day2 接入命令沙箱）。

每个工具函数签名统一为 (workspace, args) -> (ok, observation)。
安全性：路径用 _safe_path 限制在工作目录内；run_command 经 sandbox 子进程执行
（超时强制终止 + 输出截断），OS 级隔离后续用 Docker/VM 补齐。
"""

from __future__ import annotations

from pathlib import Path

from agent_eval.log import get_logger
from agent_eval.sandbox import run_command_sandboxed

logger = get_logger(__name__)


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
        logger.warning("未知工具调用: %s", tool)
        return False, f"未知工具: {tool}（可用: {', '.join(handlers)}）"
    try:
        ok, obs = fn(workspace, args)
        if not ok:
            logger.debug("工具 %s 执行失败: %s", tool, obs[:150])
        return ok, obs
    except Exception as e:  # noqa: BLE001 - 观察信息要带回给 LLM
        logger.warning("工具 %s 执行异常: %s: %s", tool, type(e).__name__, e)
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


# ---------- RAG 检索工具（V2.4：评测集真实检索环节；V2.5：BM25 混合检索） ----------

_KB_EXTS = {".md", ".txt", ".csv"}
_BM25_K1 = 1.5
_BM25_B = 0.75


def _extract_keywords(query: str) -> list[str]:
    """从查询中提取检索关键词：2+ 连续中文字符、英文单词（≥3 字母）。"""
    import re

    kws: list[str] = []
    for seg in re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z0-9_]{3,}", query):
        if len(seg) >= 2:
            kws.append(seg)
    return kws


def _bm25_tokens(text: str) -> list[str]:
    """BM25 词项：英文/数字词（≥3 字符，小写）+ 中文连续串 2-gram。"""
    import re

    tokens: list[str] = []
    for w in re.findall(r"[A-Za-z0-9_]{3,}", text):
        tokens.append(w.lower())
    for seg in re.findall(r"[\u4e00-\u9fa5]+", text):
        if len(seg) >= 2:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
        else:
            tokens.append(seg)
    return tokens


def _bm25_scores(query_tokens: list[str], chunks: list[dict]) -> list[tuple[float, dict]]:
    """对片段列表计算 BM25 得分，返回 (score, chunk) 降序。"""
    import math

    n = len(chunks)
    avgdl = sum(c["dl"] for c in chunks) / max(n, 1)
    df = {t: sum(1 for c in chunks if t in c["toks"]) for t in set(query_tokens)}
    scored: list[tuple[float, dict]] = []
    for c in chunks:
        score = 0.0
        for t in query_tokens:
            f = c["toks"].count(t)
            if not f:
                continue
            idf = math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5))
            denom = f + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * c["dl"] / avgdl)
            score += idf * (f * (_BM25_K1 + 1.0)) / max(denom, 1e-9)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return scored


def _search_kb(ws: Path, args: dict) -> tuple[bool, str]:
    """在知识库 kb/ 下做 BM25 检索（RAG 检索环节的评测实现）。

    检索范围：kb/ 下所有 .md/.txt/.csv 文件。按 ~6 行一个片段，对查询
    （英文/数字词 + 中文 2-gram）计算 BM25（k1=1.5, b=0.75），返回 Top-N
    片段，附来源定位 `[kb/xxx.md:起-止行]` 与得分，供轨迹回放"知识/检索"
    节点展示命中的知识片段。
    """
    query = str(args.get("query", "")).strip()
    top_k = max(1, min(int(args.get("top_k", 3)), 5))
    if not query:
        return False, "缺少 query 参数"
    kb = ws / "kb"
    if not kb.is_dir():
        return False, "知识库 kb/ 不存在：本任务没有可检索的知识库（fixtures 未提供 kb/）"
    q_tokens = _bm25_tokens(query)
    if not q_tokens:
        return False, f"无法从查询提取检索词项: {query!r}"

    files = sorted(
        p for p in kb.rglob("*")
        if p.is_file() and p.suffix.lower() in _KB_EXTS
    )
    if not files:
        return False, "知识库 kb/ 下没有可检索的文档（.md/.txt/.csv）"

    chunks: list[dict] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        for start in range(0, len(lines), 6):
            block = "\n".join(lines[start : start + 6])
            toks = _bm25_tokens(block)
            chunks.append(
                {
                    "rel": str(p.relative_to(ws)),
                    "start": start + 1,
                    "end": start + len(lines[start : start + 6]),
                    "text": block,
                    "toks": toks,
                    "dl": len(toks),
                }
            )

    scored = _bm25_scores(q_tokens, chunks)
    if not scored:
        logger.debug("search_kb 无命中 | query=%r docs=%d chunks=%d", query, len(files), len(chunks))
        return False, f"知识库中未检索到与「{query}」相关的内容"

    out = []
    for score, c in scored[:top_k]:
        head = f"[{c['rel']}:{c['start']}-{c['end']}] (得分 {score:.2f})"
        out.append(f"{head}\n{c['text'][:600]}")
    logger.info(
        "search_kb 命中 | query=%r top%d best=%.2f docs=%d chunks=%d",
        query, top_k, scored[0][0], len(files), len(chunks),
    )
    return True, "\n\n".join(out)
