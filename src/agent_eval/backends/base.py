"""后端抽象接口（Day 2）。

Backend 是"被测 Agent"的统一适配层：新增一个 Agent = 新增一个 Backend 子类并注册。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BackendResult:
    """一次后端执行的结果。

    status: completed（Agent 自报完成）/ max_steps（步数耗尽）/ timeout / error
    steps:  步骤级轨迹，每步 {step, action, args, observation, ts}
    usage:  可选 LLM token 用量累计（{prompt_tokens, completion_tokens}），
            供 CI 成本核算；黑盒后端（如 dsh）拿不到时可为 None。
    """

    status: str = "completed"
    steps: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    stdout: str = ""
    error: str = ""
    usage: dict | None = None
    traces: list[dict] = field(default_factory=list)


class Backend(ABC):
    name: str = "base"
    version: str = "dev"
    # 该后端的推荐默认模型（工作台选后端时自动填入，runner 未指定 model 时生效）
    default_model: str = "deepseek-chat"

    @staticmethod
    def resolve_api_key(env_names: list[str]) -> tuple[str | None, str]:
        """解析 API Key，支持 .env 配置优先、系统配置兜底。

        参数:
            env_names: 环境变量名列表，按优先级排序（第一个优先）

        返回:
            (api_key, source)
            - api_key: 解析到的 API Key，未找到时为 None
            - source: 来源描述（"current:DEEPSEEK_API_KEY" / "fallback:DEEPSEEK_API_KEY" / "none"）

        逻辑:
        1. 先从当前进程环境（os.environ）查找，这是 .env 覆盖后的值
        2. 如果没找到，从 config_manager 的 fallback 备份中查找（被 .env 覆盖前的系统原始值）
        3. 按 env_names 的优先级依次尝试
        """
        from agent_eval.config_manager import get_fallback_env
        for name in env_names:
            current = os.environ.get(name)
            if current:
                return current, f"current:{name}"
        for name in env_names:
            fallback = get_fallback_env(name)
            if fallback:
                return fallback, f"fallback:{name}"
        return None, "none"

    @abstractmethod
    def run(self, task, workspace: Path) -> BackendResult:
        """在 workspace 内执行任务，返回结果与轨迹。"""

    def check_api_key(self) -> dict:
        """检查 API Key 连通性。

        返回 dict:
        - ok: bool，是否通过
        - status: str，状态描述（ok / missing / invalid / error / unsupported）
        - message: str，详细信息
        - latency_ms: float | None，测试耗时（毫秒）
        """
        return {
            "ok": False,
            "status": "unsupported",
            "message": f"{self.name} 后端暂不支持 API Key 连通性检查",
            "latency_ms": None,
        }
