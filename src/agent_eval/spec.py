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

from agent_eval.log import get_logger

logger = get_logger(__name__)

CheckpointType = Literal[
    "file_exists",
    "file_not_exists",
    "content_contains",
    "content_not_contains",
    "cmd_exit_zero",
    # M4：RPA/UI 操作评测的 checkpoint 类型
    "ui_element_exists",   # path=URL, pattern=CSS selector
    "browser_url_contains", # path=起始URL, pattern=期望URL包含的字符串
    "http_status",         # path=URL, pattern=状态码（如 "200"、"2"、"200-299"）
]

LEVELS = {"L1", "L2", "L3", "L4", "L5"}
VERIFIERS = {"deterministic", "llm_judge"}
# HarnessDev 论文归纳的六类 Harness 控制能力
CAPABILITIES = {"execution", "tools", "context", "state", "lifecycle", "verification"}


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
    max_steps: Optional[int] = None  # 可选：覆盖后端默认步数上限（如 L4 修复类任务提额）
    tags: list[str] = field(default_factory=list)
    rubric: str = ""  # V2.2：verifier=llm_judge 时的评分标准（任务作者自定义）
    capabilities: list[str] = field(default_factory=list)  # V2.5：任务考察的 Harness 能力（六类）
    mcp_servers: list[dict] = field(default_factory=list)  # M2：任务声明的 MCP server 列表（stdio 模式）
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
            max_steps=int(data["max_steps"]) if data.get("max_steps") else None,
            tags=list(data.get("tags", [])),
            rubric=str(data.get("rubric", "")),
            capabilities=list(data.get("capabilities", [])),
            mcp_servers=list(data.get("mcp_servers", [])),
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
        for cap in self.capabilities:
            if cap not in CAPABILITIES:
                errors.append(f"capabilities 包含非法值 '{cap}'，必须为 {sorted(CAPABILITIES)} 之一")
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
    """加载 tasks_dir 下 manifest 声明的全部任务。

    支持 manifest includes 字段：引用已安装的任务包，自动合并包内任务。
    本地任务与 includes 任务 ID 冲突时，本地任务优先。
    """
    from agent_eval.taskpack import load_included_tasks

    manifest = load_manifest(tasks_dir)
    tasks: list[TaskSpec] = []
    seen_ids: set[str] = set()

    # 先加载本地任务
    for task_id in manifest.get("tasks", []):
        spec_path = tasks_dir / task_id / "spec.yaml"
        if not spec_path.exists():
            raise FileNotFoundError(f"任务 {task_id} 缺少 spec.yaml: {spec_path}")
        tasks.append(TaskSpec.from_yaml(spec_path))
        seen_ids.add(task_id)

    # 再加载 includes 引用的任务包（跳过 ID 冲突的）
    includes = manifest.get("includes", [])
    if includes:
        for task_id, spec_path in load_included_tasks(includes):
            if task_id in seen_ids:
                logger.warning(f"任务 ID 冲突，本地任务优先: {task_id}（跳过任务包中的同名任务）")
                continue
            tasks.append(TaskSpec.from_yaml(spec_path))
            seen_ids.add(task_id)

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
