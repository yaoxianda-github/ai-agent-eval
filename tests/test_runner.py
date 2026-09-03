"""V2.0-Day1：runner 集成单测（用 FakeBackend，不调用真实 LLM）。

覆盖：fixtures 复制 → 后端执行 → 判定 → 评分 → 落盘 run.json →
keep_workspace 保留/清理。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_eval.backends.base import BackendResult
from agent_eval.runner import default_results_dir, run_one
from agent_eval.spec import Checkpoint


class FakeBackend:
    """模拟一个能产出通过产物的 Agent。"""

    version = "9.9.9"

    def __init__(self, **kwargs):
        self.timeout_s = kwargs.get("timeout_s", 300)

    def run(self, task, workspace):
        time.sleep(0.01)  # 保证 duration_s 可被记录为 > 0
        (workspace / "output").mkdir(parents=True, exist_ok=True)
        (workspace / "output" / "result.md").write_text("done: 2026-09-03", encoding="utf-8")
        return BackendResult(status="completed", steps=[{"step": 1, "action": "write_file"}], duration_s=0.1, stdout="", error="")


def _make_task_with_checkpoint(make_task, spec_id="TFAKE"):
    return make_task(
        spec_id=spec_id,
        checkpoints=[Checkpoint(id="c1", type="file_exists", path="output/result.md")],
    )


def test_run_one_full_pipeline(tmp_path, make_task, monkeypatch):
    monkeypatch.setattr("agent_eval.runner.get_backend", lambda *a, **k: FakeBackend(**k))
    task = _make_task_with_checkpoint(make_task)
    results_dir = tmp_path / "results" / "runs"

    rec = run_one(task, "fake", config={"agent": {"model": "x"}}, results_dir=results_dir)

    assert rec.status == "completed"
    assert rec.agent_ver == "9.9.9"
    assert len(rec.verdicts) == 1 and rec.verdicts[0]["passed"] is True
    assert rec.metrics["score"] == task.weight
    assert rec.duration_s > 0

    # run.json 落盘且可回读
    run_file = results_dir / rec.run_id / "run.json"
    assert run_file.exists()
    data = json.loads(run_file.read_text(encoding="utf-8"))
    assert data["task_id"] == "TFAKE"
    assert data["verdicts"][0]["passed"] is True


def test_run_one_default_results_dir():
    assert default_results_dir() == (Path("results") / "runs")


def test_run_one_keep_workspace_false(tmp_path, make_task, monkeypatch):
    monkeypatch.setattr("agent_eval.runner.get_backend", lambda *a, **k: FakeBackend(**k))
    task = _make_task_with_checkpoint(make_task)
    results_dir = tmp_path / "results" / "runs"

    rec = run_one(task, "fake", results_dir=results_dir, keep_workspace=False)

    assert rec.workspace == ""
    assert not (results_dir / rec.run_id / "workspace").exists()
    # run.json 仍在
    assert (results_dir / rec.run_id / "run.json").exists()


def test_run_one_timeout_s_from_spec(tmp_path, make_task, monkeypatch):
    captured = {}

    def fake_get_backend(*a, **k):
        captured.update(k)
        return FakeBackend(**k)

    monkeypatch.setattr("agent_eval.runner.get_backend", fake_get_backend)
    task = _make_task_with_checkpoint(make_task)
    task.timeout_s = 777

    run_one(task, "fake", results_dir=tmp_path / "r")

    assert captured.get("timeout_s") == 777  # 后端默认超时来自 spec
