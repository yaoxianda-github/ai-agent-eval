"""评测执行编排（Day 2-3）。

流程：加载任务 spec → 干净工作目录（复制 fixtures）→ 调用后端 → 记录轨迹 →
执行判定与评分（verdicts + metrics）→ 落盘 run.json（results/runs/<run_id>/）。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_eval.backends import get_backend
from agent_eval.scoring import score_task
from agent_eval.spec import TaskSpec
from agent_eval.verifiers import run_checkpoints


@dataclass
class RunRecord:
    run_id: str
    agent_id: str
    agent_ver: str
    task_id: str
    task_level: str
    status: str
    steps: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    metrics: dict = field(default_factory=dict)
    verdicts: list[dict] = field(default_factory=list)
    error: str = ""
    workspace: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def default_results_dir() -> Path:
    return Path("results") / "runs"


def run_one(
    task: TaskSpec,
    agent_id: str,
    *,
    config: dict | None = None,
    results_dir: Path | None = None,
    keep_workspace: bool = True,
    run_id: str | None = None,
) -> RunRecord:
    """对单个任务执行一次评测，返回并落盘 RunRecord。

    run_id：可选。不传时自动生成（CLI 默认行为）；Web 工作台传入预生成的
    run_id，保证 API 返回的 run_id 与实际落盘目录一致。
    """
    config = config or {}
    run_id = run_id or uuid.uuid4().hex[:12]
    results_dir = results_dir or default_results_dir()
    run_dir = results_dir / run_id
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    _copy_fixtures(task, workspace)

    # 后端默认超时取任务 spec 的 timeout_s，可被 config 覆盖
    agent_kwargs = dict(config.get("agent", {}))
    agent_kwargs.setdefault("timeout_s", task.timeout_s)
    backend = get_backend(agent_id, **agent_kwargs)
    start = time.time()
    result = backend.run(task, workspace)
    duration = round(time.time() - start, 3)

    # Day 3：执行判定与评分（仅当后端未发生 error 时）
    verdicts: list[dict] = []
    metrics: dict = {}
    if result.status != "error":
        verdicts = run_checkpoints(task, workspace)
        metrics = score_task(task, verdicts)

    record = RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        agent_ver=getattr(backend, "version", "dev"),
        task_id=task.id,
        task_level=task.level,
        status=result.status,
        steps=result.steps,
        duration_s=duration,
        metrics=metrics,
        verdicts=verdicts,
        error=result.error or "",
        workspace=_rel_or_abs(workspace) if keep_workspace else "",
    )

    if not keep_workspace:
        shutil.rmtree(workspace, ignore_errors=True)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def _copy_fixtures(task: TaskSpec, workspace: Path) -> None:
    base = task.spec_path.parent
    src = (base / task.fixtures.get("source", "fixtures")).resolve()
    if src.exists():
        shutil.copytree(src, workspace, dirs_exist_ok=True)


def _rel_or_abs(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
