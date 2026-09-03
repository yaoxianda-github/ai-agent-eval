"""V2.0-Day1：spec 加载与校验单测。"""

from __future__ import annotations

import pytest

from agent_eval.spec import Checkpoint, TaskSpec


def test_from_yaml_loads_checkpoints(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        """
id: T901
title: 示例任务
level: L2
description: 描述
weight: 1.2
verifier: deterministic
timeout_s: 120
fixtures:
  source: fixtures
ground_truth:
  checkpoints:
    - id: c1
      type: file_exists
      path: output/result.md
    - id: c2
      type: content_contains
      path: output/result.md
      pattern: "2026-"
""",
        encoding="utf-8",
    )
    task = TaskSpec.from_yaml(spec)
    assert task.id == "T901"
    assert task.level == "L2"
    assert task.weight == 1.2
    assert task.timeout_s == 120
    assert len(task.checkpoints) == 2
    assert task.checkpoints[0].type == "file_exists"
    assert task.checkpoints[1].pattern == "2026-"
    assert task.spec_path == spec


def test_from_yaml_defaults(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text("id: T902\ntitle: 默认值任务\n", encoding="utf-8")
    task = TaskSpec.from_yaml(spec)
    assert task.level == "L1"
    assert task.weight == 1.0
    assert task.timeout_s == 300
    assert task.verifier == "deterministic"


def test_validate_missing_id(tmp_path):
    task = TaskSpec(id="", title="t", level="L1", description="d")
    assert "缺少 id" in task.validate()


def test_validate_bad_level(make_task):
    task = make_task(level="L9")
    errors = task.validate()
    assert any("level 必须为" in e for e in errors)


def test_validate_bad_verifier(make_task):
    task = make_task()
    task.verifier = "llm_judge_x"
    errors = task.validate()
    assert any("verifier 必须为" in e for e in errors)


def test_validate_bad_checkpoint_type(make_task):
    task = make_task(checkpoints=[Checkpoint(id="c1", type="delete_disk")])
    errors = task.validate()
    assert any("类型非法" in e for e in errors)


def test_from_yaml_raises_on_invalid(tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text("id: T903\ntitle: bad\nlevel: L9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        TaskSpec.from_yaml(spec)


def test_fixtures_dir(make_task):
    task = make_task()
    assert task.fixtures_dir().name == "fixtures"
    assert task.fixtures_dir().parent == task.spec_path.parent
