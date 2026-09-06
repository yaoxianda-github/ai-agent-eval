"""V2.7 多 Agent 对比矩阵 + License 收费墙自测。

用 FakeBackend（monkeypatch runner.get_backend）跑批次，不依赖外部 LLM。
覆盖：license 档位、三 agent 批次矩阵聚合、社区版降级卡口、Pro 解锁与 CSV 导出、
store 批次 CRUD。
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

from agent_eval import license as license_mod
from agent_eval.backends.base import Backend, BackendResult

pytestmark = pytest.mark.skipif(not HAS_FASTAPI, reason="需要 fastapi")

_MANIFEST = """name: test-pack
version: 0.1.0
schema: task-spec@v1
tasks: [T600]
"""

_T600_SPEC = """id: T600
title: 测试任务
level: L1
description: 矩阵测试用最小任务
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

THREE_AGENTS = ["aider", "deepseek-harness", "minimal-react"]


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
            steps=[{"step": 1, "action": "write_file", "observation": "ok"}],
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
    license_mod.reset_cache()
    monkeypatch.delenv("AGENT_EVAL_LICENSE", raising=False)
    # 切到临时目录，避免项目根真实 license.key 让社区版用例意外变成 Pro
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("agent_eval.runner.get_backend", lambda name, **kw: FakeBackend(**kw))
    from agent_eval.web.app import create_app

    app = create_app(
        tasks_dir=_make_tasks_dir(tmp_path),
        results_dir=tmp_path / "results" / "runs",
        report_dir=tmp_path / "reports",
        db_path=tmp_path / "results" / "run_history.db",
    )
    c = TestClient(app)
    yield c
    license_mod.reset_cache()


def _wait_batch_done(c, bid, timeout_s=15):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = c.get(f"/api/batches/{bid}")
        assert r.status_code == 200, r.text
        b = r.json()
        if b["status"] == "done":
            return b
        time.sleep(0.1)
    raise AssertionError("批次超时未完成")


# ---------- License ----------
def test_license_community_default(client):
    r = client.get("/api/license")
    ent = r.json()
    assert ent["plan"] == "community"
    assert ent["max_compare_agents"] == 2
    assert ent["export_csv"] is False
    assert ent["show_cost_stability"] is False


def test_license_issue_verify_roundtrip():
    tok = license_mod.issue_license("pro", 30)
    p = license_mod.verify_license(tok)
    assert p["plan"] == "pro"
    with pytest.raises(ValueError):
        license_mod.verify_license(tok[:-1] + ("0" if tok[-1] != "0" else "1"))


# ---------- 社区版降级卡口 ----------
def test_community_blocks_three_agents(client):
    r = client.post("/api/batches", json={"agents": THREE_AGENTS, "task_ids": ["T600"], "runs": 1})
    assert r.status_code == 403
    assert "社区版" in r.json()["detail"]


def test_community_allows_two_agents(client):
    r = client.post(
        "/api/batches", json={"agents": THREE_AGENTS[:2], "task_ids": ["T600"], "runs": 1}
    )
    assert r.status_code == 200, r.text
    _wait_batch_done(client, r.json()["batch_id"])


# ---------- 三 Agent 矩阵聚合 ----------
def test_three_agent_matrix_and_drilldown(client, monkeypatch):
    # 开 Pro 以允许三 agent
    monkeypatch.setenv("AGENT_EVAL_LICENSE", license_mod.issue_license("pro", 365))
    license_mod.reset_cache()

    r = client.post(
        "/api/batches", json={"agents": THREE_AGENTS, "task_ids": ["T600"], "runs": 2}
    )
    assert r.status_code == 200, r.text
    bid = r.json()["batch_id"]
    assert r.json()["total_runs"] == 6  # 3 agent × 1 task × 2 runs

    b = _wait_batch_done(client, bid)
    m = b["summary"]
    assert sorted(m["agents"]) == sorted(THREE_AGENTS)
    assert m["tasks"] == ["T600"]
    # 每格 2 次运行，全部通过
    for a in THREE_AGENTS:
        cell = m["cells"][f"{a}|T600"]
        assert cell["n"] == 2
        assert cell["best"] == 1.0
        assert cell["pass_rate"] == 1.0
        assert len(cell["runs"]) == 2  # 下钻数据
        tot = m["totals"][a]
        assert tot["tasks_passed"] == 1
        assert tot["weighted_score"] == 1.0
    assert m["conclusion"]  # 自动结论非空

    # /api/matrix 独立接口一致
    mm = client.get("/api/matrix", params={"batch_id": bid}).json()
    assert mm["agents"] == m["agents"]


def test_pro_csv_export(client, monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_LICENSE", license_mod.issue_license("pro", 365))
    license_mod.reset_cache()
    r = client.post(
        "/api/batches", json={"agents": THREE_AGENTS, "task_ids": ["T600"], "runs": 1}
    )
    bid = r.json()["batch_id"]
    _wait_batch_done(client, bid)

    exp = client.get("/api/matrix/export", params={"batch_id": bid})
    assert exp.status_code == 200
    assert "text/csv" in exp.headers["content-type"]
    assert "attachment" in exp.headers["content-disposition"]
    body = exp.content.decode("utf-8-sig")
    assert "Agent" in body and "加权总分" in body
    for a in THREE_AGENTS:
        assert a in body


def test_community_export_blocked(client):
    # 社区版：先用两个 agent 建一个批次
    r = client.post(
        "/api/batches", json={"agents": THREE_AGENTS[:2], "task_ids": ["T600"], "runs": 1}
    )
    bid = r.json()["batch_id"]
    _wait_batch_done(client, bid)
    exp = client.get("/api/matrix/export", params={"batch_id": bid})
    assert exp.status_code == 403


def test_batch_list_and_404(client):
    r = client.post(
        "/api/batches", json={"agents": THREE_AGENTS[:1], "task_ids": ["T600"], "runs": 1}
    )
    bid = r.json()["batch_id"]
    _wait_batch_done(client, bid)
    lst = client.get("/api/batches").json()["batches"]
    assert any(x["batch_id"] == bid for x in lst)
    assert client.get("/api/batches/nope").status_code == 404
