"""后端抽象接口（Day 2）。

Backend 是"被测 Agent"的统一适配层：新增一个 Agent = 新增一个 Backend 子类并注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BackendResult:
    """一次后端执行的结果。

    status: completed（Agent 自报完成）/ max_steps（步数耗尽）/ timeout / error
    steps:  步骤级轨迹，每步 {step, action, args, observation, ts}
    """

    status: str = "completed"
    steps: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    stdout: str = ""
    error: str = ""


class Backend(ABC):
    name: str = "base"
    version: str = "dev"

    @abstractmethod
    def run(self, task, workspace: Path) -> BackendResult:
        """在 workspace 内执行任务，返回结果与轨迹。"""
