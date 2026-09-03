"""V2.0-Day1 框架单测公共工具：构造 TaskSpec 工厂。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_eval.spec import Checkpoint, TaskSpec


@pytest.fixture
def make_task(tmp_path: Path):
    """在 tmp_path 下构造一个最小 TaskSpec（spec_path 指向 tmp/tasks/<id>/spec.yaml）。"""

    def _make(
        spec_id: str = "TFAKE",
        level: str = "L1",
        weight: float = 1.0,
        checkpoints: list[Checkpoint] | None = None,
        fixtures: dict | None = None,
    ) -> TaskSpec:
        spec_path = tmp_path / "tasks" / spec_id / "spec.yaml"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(f"id: {spec_id}", encoding="utf-8")
        return TaskSpec(
            id=spec_id,
            title="测试任务",
            level=level,
            description="测试描述",
            fixtures=fixtures or {},
            checkpoints=checkpoints or [],
            weight=weight,
            spec_path=spec_path,
        )

    return _make
