"""后端注册表（MVP）：按名称实例化 Agent 后端。

扩展方式：定义 Backend 子类后调用 register_backend(cls)，或直接在 _BACKENDS 注册。
"""

from __future__ import annotations

from agent_eval.backends.base import Backend, BackendResult
from agent_eval.backends.minimal_react import MinimalReactBackend
from agent_eval.backends.aider import AiderBackend

_BACKENDS: dict[str, type[Backend]] = {
    MinimalReactBackend.name: MinimalReactBackend,
    AiderBackend.name: AiderBackend,
}


def register_backend(cls: type[Backend]) -> None:
    _BACKENDS[cls.name] = cls


def get_backend(name: str, **kwargs) -> Backend:
    if name not in _BACKENDS:
        raise ValueError(f"未知后端: {name}，可用: {sorted(_BACKENDS)}")
    return _BACKENDS[name](**kwargs)


def list_backends() -> list[str]:
    return sorted(_BACKENDS)


__all__ = ["Backend", "BackendResult", "register_backend", "get_backend", "list_backends"]
