"""任务包生成（V2.1 任务管理）：按表单生成 tasks/<id>/spec.yaml + fixtures 骨架 + 更新 manifest。

严格对齐引擎契约（spec.py 的 TaskSpec.from_yaml / manifest.yaml 结构），
保证新生成的任务可被 list-tasks / run 直接消费。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from agent_eval.spec import LEVELS, VERIFIERS

_ID_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
CHECKPOINT_TYPES = (
    "file_exists",
    "file_not_exists",
    "content_contains",
    "content_not_contains",
    "cmd_exit_zero",
)


def build_spec(data: dict) -> dict:
    """把表单数据整理成可直接 dump 的 spec dict，并做基础校验。"""
    task_id = str(data.get("id", "")).strip()
    if not _ID_RE.match(task_id):
        raise ValueError("任务 ID 必须为 3-20 位字母/数字/下划线")
    title = str(data.get("title", "")).strip()
    if not title:
        raise ValueError("缺少标题")
    level = data.get("level", "L1")
    if level not in LEVELS:
        raise ValueError(f"level 必须为 {sorted(LEVELS)} 之一")
    verifier = data.get("verifier", "deterministic")
    if verifier not in VERIFIERS:
        raise ValueError(f"verifier 必须为 {sorted(VERIFIERS)} 之一")

    checkpoints = []
    for cp in data.get("checkpoints", []):
        ctype = cp.get("type", "")
        if ctype not in CHECKPOINT_TYPES:
            raise ValueError(f"校验点类型非法: {ctype}")
        item: dict = {"id": str(cp.get("id", "")).strip(), "type": ctype}
        for k in ("desc", "path", "pattern", "cmd"):
            v = cp.get(k)
            if v is not None and str(v).strip():
                item[k] = str(v).strip()
        if not item["id"]:
            raise ValueError("存在缺少 id 的校验点")
        checkpoints.append(item)
    if not checkpoints:
        raise ValueError("至少需要一个校验点")

    return {
        "id": task_id,
        "title": title,
        "level": level,
        "description": str(data.get("description", "")).strip() or f"{task_id} 任务说明待补充",
        "tags": [t.strip() for t in str(data.get("tags", "")).split(",") if t.strip()],
        "fixtures": {"source": "fixtures/"},
        "ground_truth": {"checkpoints": checkpoints},
        "verifier": verifier,
        "weight": float(data.get("weight", 1.0)),
        "cost_budget_usd": float(data.get("cost_budget_usd", 0.5)),
        "timeout_s": int(data.get("timeout_s", 300)),
    }


def generate_task_pack(tasks_dir: Path, data: dict) -> dict:
    """在 tasks_dir 下生成任务目录，返回 {task_dir, spec_path, manifest_updated}。"""
    spec = build_spec(data)
    task_id = spec["id"]
    task_dir = tasks_dir / task_id
    if task_dir.exists():
        raise FileExistsError(f"任务目录已存在: {task_dir}")
    task_dir.mkdir(parents=True)
    fixtures_in = task_dir / "fixtures" / "input"
    fixtures_in.mkdir(parents=True)
    (fixtures_in / "README.md").write_text(
        f"# {task_id} fixtures\n\n将任务输入文件放到本目录（input/）。\n当前为骨架，待补充真实输入。\n",
        encoding="utf-8",
    )
    spec_path = task_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    manifest_path = tasks_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    tasks = list(manifest.get("tasks", []))
    updated = task_id in tasks
    if not updated:
        tasks.append(task_id)
        manifest["tasks"] = tasks
        manifest_path.write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return {
        "task_dir": str(task_dir),
        "spec_path": str(spec_path),
        "manifest_updated": updated,
    }
