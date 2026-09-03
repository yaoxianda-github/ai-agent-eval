"""Langfuse 可选 trace 层测试（V2.2）。

核心保证：默认 no-op（不抛、不依赖 langfuse SDK）；显式启用但未装 SDK 时
静默降级，不影响主流程。
"""

from __future__ import annotations

import agent_eval.observability as obs


def _reset(monkeypatch) -> None:
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs, "_client", None)


def test_disabled_is_noop(monkeypatch) -> None:
    _reset(monkeypatch)
    monkeypatch.delenv("AGENT_EVAL_TRACE", raising=False)
    assert obs.is_enabled() is False
    assert obs.trace_llm_call("agent_step", "deepseek-chat", messages=[], response="x") is None


def test_enabled_without_sdk_degrades(monkeypatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("AGENT_EVAL_TRACE", "langfuse")
    assert obs.is_enabled() is True
    # langfuse SDK 未安装/凭据缺失 → 静默 no-op，不抛异常
    assert obs.trace_llm_call("judge", "m", messages="x", response="y") is None


def test_unknown_mode_is_noop(monkeypatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setenv("AGENT_EVAL_TRACE", "otlp")
    assert obs.is_enabled() is False
    assert obs.trace_llm_call("agent_step", "m") is None
