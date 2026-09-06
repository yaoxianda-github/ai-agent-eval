"""轨迹回放公共工具（V2.4）。

traces 时间线节点分类：intent / llm / retrieval（知识检索）/ tool（工具执行）。
`tool_category` 供后端埋点与 runner/Web 兜底合成复用，避免分类规则三处漂移。
"""

from __future__ import annotations

# 读取/检索类工具 → 时间线"知识/检索"（retrieval）节点；其余为工具执行（tool）
# 含 dsh 黑盒的工具名（read/grep 等），让外部 Agent 的读取行为也归入知识层
_RETRIEVAL_TOOLS = frozenset(
    {"read_file", "list_dir", "search", "query", "search_kb", "read", "grep"}
)


def tool_category(tool: str | None) -> str:
    return "retrieval" if (tool or "") in _RETRIEVAL_TOOLS else "tool"
