"""Task Spec 数据模型与加载（Day 1）。

Task Spec 是任务包与评测框架之间的契约：
- 任务作者编写 tasks/<id>/spec.yaml（字段见 docs/task-spec.md）
- 框架据此加载任务、执行后端 Agent、运行判定器
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml

CheckpointType = Literal[
    "file_exists",
    "file_not_exists",
    "content_contains",
    "content_not_contains",
    "cmd_exit_zero",
]

LEVELS = {"L1", "L2", "L3", "L4", "L5"}
VERIFIERS = {"deterministic", "llm_judge"}


@dataclass
class Checkpoint:
    id: str
    type: CheckpointType
    desc: str = ""
    path: str = ""
    pattern: str = ""
    cmd: str = ""


@dataclass
class TaskSpec:
    id: str
    title: str
    level: str
    description: str
    fixtures: dict = field(default_factory=dict)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    verifier: str = "deterministic"
    weight: float = 1.0
    cost_budget_usd: float = 0.5
    timeout_s: int = 300
    tags: list[str] = field(default_factory=list)
    rubric: str = ""  # V2.2：verifier=llm_judge 时的评分标准（任务作者自定义）
    spec_path: Optional[Path] = None

    @classmethod
    def from_yaml(cls, path: Path) -> "TaskSpec":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        checkpoints = [
            Checkpoint(
                id=cp.get("id", ""),
                type=cp.get("type", ""),
                desc=cp.get("desc", ""),
                path=cp.get("path", ""),
                pattern=cp.get("pattern", ""),
                cmd=cp.get("cmd", ""),
            )
            for cp in data.get("ground_truth", {}).get("checkpoints", [])
        ]
        spec = cls(
            id=data.get("id", path.parent.name),
            title=data.get("title", ""),
            level=data.get("level", "L1"),
            description=data.get("description", ""),
            fixtures=data.get("fixtures", {}),
            checkpoints=checkpoints,
            verifier=data.get("verifier", "deterministic"),
            weight=float(data.get("weight", 1.0)),
            cost_budget_usd=float(data.get("cost_budget_usd", 0.5)),
            timeout_s=int(data.get("timeout_s", 300)),
            tags=list(data.get("tags", [])),
            rubric=str(data.get("rubric", "")),
            spec_path=path,
        )
        errors = spec.validate()
        if errors:
            raise ValueError(f"任务 {spec.id} spec 校验失败: {'; '.join(errors)}")
        return spec

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.id:
            errors.append("缺少 id")
        if not self.title:
            errors.append("缺少 title")
        if self.level not in LEVELS:
            errors.append(f"level 必须为 {sorted(LEVELS)} 之一，当前: {self.level}")
        if self.verifier not in VERIFIERS:
            errors.append(
                f"verifier 必须为 {sorted(VERIFIERS)} 之一，当前: {self.verifier}"
            )
        for cp in self.checkpoints:
            if not cp.id:
                errors.append("存在缺少 id 的校验点")
            if cp.type not in CheckpointType.__args__:
                errors.append(f"校验点 {cp.id} 类型非法: {cp.type}")
        return errors

    def fixtures_dir(self) -> Path:
        """fixtures 绝对目录（tasks/<id>/fixtures）。"""
        assert self.spec_path is not None
        return self.spec_path.parent / "fixtures"


def load_manifest(tasks_dir: Path) -> dict:
    path = tasks_dir / "manifest.yaml"
    if not path.exists():
        raise FileNotFoundError(f"找不到任务包清单: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_task_pack(tasks_dir: Path) -> list[TaskSpec]:
    """加载 tasks_dir 下 manifest 声明的全部任务。"""
    manifest = load_manifest(tasks_dir)
    tasks: list[TaskSpec] = []
    for task_id in manifest.get("tasks", []):
        spec_path = tasks_dir / task_id / "spec.yaml"
        if not spec_path.exists():
            raise FileNotFoundError(f"任务 {task_id} 缺少 spec.yaml: {spec_path}")
        tasks.append(TaskSpec.from_yaml(spec_path))
    return tasks


def find_tasks_dir() -> Path:
    """定位任务包目录：优先环境变量 AGENT_EVAL_TASKS，其次当前目录，最后包默认位置。"""
    env = os.environ.get("AGENT_EVAL_TASKS")
    if env:
        return Path(env)
    cwd = Path.cwd() / "tasks"
    if cwd.is_dir():
        return cwd
    return Path(__file__).resolve().parents[2] / "tasks"
