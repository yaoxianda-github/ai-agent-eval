"""可选可观测层（V2.2）：Langfuse trace 分析。

设计：默认 **no-op**（零依赖、零开销），不影响任何现有流程。仅当显式启用才接入
Langfuse 记录 LLM 调用（输入/输出/token/耗时），用于成本与行为归因分析。

启用方式（环境变量）：
- AGENT_EVAL_TRACE=langfuse        # 打开 trace
- LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY   # Langfuse 凭据

依赖：langfuse SDK（`pip install langfuse`）。未安装时静默降级为 no-op，不报错。

埋点约定：minimal_react 的每一步 LLM 调用与 llm_judge 判分调用均通过
trace_llm_call(...) 记录；埋点自身异常一律吞掉，不影响评测主流程。
"""

from __future__ import annotations

import os
from typing import Any

_client = None
_initialized = False


def is_enabled() -> bool:
    return os.environ.get("AGENT_EVAL_TRACE", "").strip().lower() == "langfuse"


def _get_client():
    """懒加载 Langfuse 客户端；未安装 SDK / 缺凭据 / 初始化失败 → None。"""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    if not is_enabled():
        return None
    try:
        from langfuse import Langfuse

        host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
        sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
        if not (pk and sk):
            return None
        _client = Langfuse(public_key=pk, secret_key=sk, host=host)
    except Exception:  # noqa: BLE001 - 观测层失败不影响主流程
        _client = None
    return _client


def trace_llm_call(
    kind: str,
    model: str,
    messages: Any = None,
    response: Any = None,
    usage: Any = None,
    duration_ms: int = 0,
    tags: list[str] | None = None,
    **_: Any,
) -> None:
    """记录一次 LLM 调用。默认 no-op；仅 AGENT_EVAL_TRACE=langfuse 时写入 Langfuse。"""
    if not is_enabled():
        return
    client = _get_client()
    if client is None:
        return
    try:
        trace = client.trace(
            name=f"agent_eval.{kind}",
            input={"messages": messages} if messages is not None else None,
            output={"response": response} if response is not None else None,
            metadata={"model": model, "tags": tags or []},
        )
        span = trace.span(
            name=f"llm.{kind}",
            model=model,
            input={"messages": messages} if messages is not None else None,
            output={"response": response} if response is not None else None,
            usage=usage,
        )
        span.end()
    except Exception:  # noqa: BLE001
        return
