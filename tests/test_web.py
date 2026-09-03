"""V2.1 Web 工作台自测：FastAPI TestClient + FakeBackend（不依赖外部 LLM 服务）。

覆盖：meta/tasks/backends、创建运行并轮询、历史列表、产物文件读写（含路径穿越防护）、
汇总、报告生成、任务包生成。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

try:
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    HAS_FASTAPI = False

from agent_eval.backends.base import Backend, BackendResult

pytestmark = pytest.mark.skipif(
    not HAS_FASTAPI, reason="需要 fastapi：pip install -e '.[web]'"
)

_MANIFEST = """name: test-pack
version: 0.1.0
schema: task-spec@v1
tasks: [T600]
"""

_T600_SPEC = """id: T600
title: 测试任务
level: L1
description: Web 层测试用的最小任务
fixtures:
  source: fixtures/
ground_truth:
  checkpoints:
    - id: c1
      type: file_exists
      path: output/ok.txt
      desc: 已生成 ok.txt
verifier: deterministic
weight: 1.0
timeout_s: 30
"""


class FakeBackend(Backend):
    name = "fake"
    version = "9.9.9"

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run(self, task, workspace):
        out = workspace / "output"
        out.mkdir(exist_ok=True)
        (out / "ok.txt").write_text("done\n", encoding="utf-8")
        return BackendResult(
            status="completed",
            steps=[{"step": 1, "action": "write_file", "args": "output/ok.txt", "observation": "ok"}],
        )


def _make_tasks_dir(tmp_path: Path) -> Path:
    d = tmp_path / "tasks"
    d.mkdir()
    (d / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    t = d / "T600"
    t.mkdir()
    (t / "spec.yaml").write_text(_T600_SPEC, encoding="utf-8")
    return d


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_eval.runner.get_backend", lambda name, **kw: FakeBackend(**kw)
    )
    from agent_eval.web.app import create_app

    tasks_dir = _make_tasks_dir(tmp_path)
    results_dir = tmp_path / "results" / "runs"
    report_dir = tmp_path / "reports"
    db_path = tmp_path / "results" / "run_history.db"
    app = create_app(
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        report_dir=report_dir,
        db_path=db_path,
    )
    return TestClient(app)


def wait_done(c, run_id, timeout_s=15):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = c.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if not data.get("running"):
            return data
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} 超时未完成")


def test_meta(client):
    r = client.get("/api/meta")
    assert r.status_code == 200
    assert r.json()["version"]


def test_tasks_list(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert any(t["id"] == "T600" for t in tasks)


def test_backends_list(client):
    r = client.get("/api/backends")
    assert r.status_code == 200
    names = [b["id"] for b in r.json()["backends"]]
    assert "minimal-react" in names


def test_create_run_and_poll(client):
    r = client.post(
        "/api/runs",
        json={"task_id": "T600", "agent_id": "minimal-react", "runs": 1},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["last_run_id"]

    data = wait_done(client, run_id)
    assert data["running"] is False
    assert data["status"] == "completed"
    assert data["metrics"]["score"] == pytest.approx(1.0)
    assert data["metrics"]["weight"] == pytest.approx(1.0)
    assert all(v["passed"] for v in data["verdicts"])


def test_multirun(client):
    r = client.post(
        "/api/runs", json={"task_id": "T600", "agent_id": "minimal-react", "runs": 3}
    )
    run_ids = r.json()["run_ids"]
    assert len(run_ids) == 3
    wait_done(client, r.json()["last_run_id"])

    hist = client.get("/api/runs?task_id=T600").json()["runs"]
    new_ids = {x["run_id"] for x in hist}
    assert set(run_ids) <= new_ids


def test_list_runs_history(client):
    wait_done(
        client,
        client.post("/api/runs", json={"task_id": "T600", "agent_id": "minimal-react"}).json()["last_run_id"],
    )
    r = client.get("/api/runs")
    assert r.status_code == 200
    assert len(r.json()["runs"]) >= 1
    row = r.json()["runs"][0]
    assert row["task_id"] == "T600"
    assert row["status"] == "completed"


def test_run_files_and_read(client):
    run_id = wait_done(
        client,
        client.post("/api/runs", json={"task_id": "T600", "agent_id": "minimal-react"}).json()["last_run_id"],
    )["run_id"]

    files = client.get(f"/api/runs/{run_id}/files").json()["files"]
    assert any(f["path"] == "output/ok.txt" for f in files)

    content = client.get(f"/api/runs/{run_id}/file?path=output/ok.txt").json()["content"]
    assert "done" in content


def test_file_path_traversal_rejected(client):
    run_id = wait_done(
        client,
        client.post("/api/runs", json={"task_id": "T600", "agent_id": "minimal-react"}).json()["last_run_id"],
    )["run_id"]
    r = client.get(f"/api/runs/{run_id}/file?path=../../manifest.yaml")
    assert r.status_code in (400, 404)


def test_run_404(client):
    assert client.get("/api/runs/nonexistent").status_code == 404


def test_summary(client):
    wait_done(
        client,
        client.post("/api/runs", json={"task_id": "T600", "agent_id": "minimal-react"}).json()["last_run_id"],
    )
    r = client.get("/api/summary")
    assert r.status_code == 200
    assert r.json()["total_runs"] >= 1


def test_report_generate(client):
    wait_done(
        client,
        client.post("/api/runs", json={"task_id": "T600", "agent_id": "minimal-react"}).json()["last_run_id"],
    )
    r = client.post("/api/report", json={"out_name": "report.html"})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    page = client.get(url)
    assert page.status_code == 200
    assert "评测报告" in page.text


def test_task_generate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_eval.runner.get_backend", lambda name, **kw: FakeBackend(**kw)
    )
    from agent_eval.web.app import create_app

    tasks_dir = _make_tasks_dir(tmp_path)
    app = create_app(
        tasks_dir=tasks_dir,
        results_dir=tmp_path / "results" / "runs",
        report_dir=tmp_path / "reports",
        db_path=tmp_path / "results" / "run_history.db",
    )
    c = TestClient(app)

    r = c.post(
        "/api/tasks/generate",
        json={
            "id": "T601",
            "title": "新增任务",
            "level": "L2",
            "verifier": "deterministic",
            "weight": 1.2,
            "timeout_s": 60,
            "tags": "file,text",
            "description": "生成测试",
            "checkpoints": [
                {
                    "id": "c1",
                    "type": "file_exists",
                    "path": "output/a.txt",
                    "desc": "生成了 a.txt",
                },
                {
                    "id": "c2",
                    "type": "content_contains",
                    "path": "output/a.txt",
                    "pattern": "hello",
                    "desc": "内容包含 hello",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    spec = Path(r.json()["spec_path"])
    assert spec.exists()
    assert "T601" in spec.read_text(encoding="utf-8")

    manifest = tasks_dir / "manifest.yaml"
    assert "T601" in manifest.read_text(encoding="utf-8")

    tasks = c.get("/api/tasks").json()["tasks"]
    assert any(t["id"] == "T601" for t in tasks)


def test_task_generate_invalid(client):
    r = client.post(
        "/api/tasks/generate",
        json={"id": "bad id!!", "title": "x", "level": "L9", "checkpoints": []},
    )
    assert r.status_code == 400
