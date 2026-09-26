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
    # V4.4 起综合得分含轨迹效率（60/25/15 加权），FakeBackend 单步效率 0.98 → 总分 0.995
    assert data["metrics"]["score"] >= 0.9
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


def test_task_generate_llm_judge_rubric(tmp_path, monkeypatch):
    """V2.2：生成 llm_judge 任务时写入 rubric，且生成后可被框架正常加载。"""
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
            "id": "T602",
            "title": "LLM 判分任务",
            "level": "L5",
            "verifier": "llm_judge",
            "weight": 2.0,
            "timeout_s": 300,
            "tags": "report",
            "description": "基于聊天记录输出周报",
            "rubric": "1. 内容完整性(30分); 2. 信息准确性(25分)",
            "checkpoints": [
                {"id": "c1", "type": "file_exists", "path": "output/report.md", "desc": "已生成周报"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    text = Path(r.json()["spec_path"]).read_text(encoding="utf-8")
    assert "verifier: llm_judge" in text
    assert "rubric" in text and "内容完整性" in text

    tasks = c.get("/api/tasks").json()["tasks"]
    t = next(x for x in tasks if x["id"] == "T602")
    assert t["verifier"] == "llm_judge"
    assert "内容完整性" in t["rubric"]


def test_read_file_with_relative_results_dir(tmp_path, monkeypatch):
    """生产形态回归：results_dir 为相对路径时（工作台默认），产物文件读取不 500。

    历史 bug：read_run_file 返回行用未 resolve 的 ws（相对）对绝对 target 做
    relative_to → ValueError → 500；绝对路径测试环境掩盖了该问题。
    """
    monkeypatch.chdir(tmp_path)  # 模拟工作台在项目根启动
    monkeypatch.setattr(
        "agent_eval.runner.get_backend", lambda name, **kw: FakeBackend(**kw)
    )
    from agent_eval.web.app import create_app

    tasks_dir = _make_tasks_dir(tmp_path)
    results_dir = Path("results/runs")  # 相对路径（default_results_dir 形态）
    app = create_app(
        tasks_dir=tasks_dir,
        results_dir=results_dir,
        report_dir=Path("reports"),
        db_path=Path("results/run_history.db"),
    )
    c = TestClient(app)
    run_id = wait_done(
        c,
        c.post("/api/runs", json={"task_id": "T600", "agent_id": "minimal-react"}).json()["last_run_id"],
    )["run_id"]
    files = c.get(f"/api/runs/{run_id}/files").json()["files"]
    assert any(f["path"] == "output/ok.txt" for f in files)
    r = c.get(f"/api/runs/{run_id}/file?path=output/ok.txt")
    assert r.status_code == 200, r.text
    assert "done" in r.json()["content"]


# ---------- V5.0 团队回归看板 ----------
def test_regression_board_empty(client):
    """空数据回归看板返回完整结构。"""
    r = client.get("/api/regression/board")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "regression_task_count" in data
    assert "converted_badcase_count" in data
    assert "schedule" in data
    assert data["regression_run_count"] == 0
    assert data["health"] == "unknown"


def test_badcases_convert_to_tasks_batch(client, tmp_path):
    """批量转化 badcase 为回归任务（自动编号 T-REG-NNN）。"""
    r = client.post("/api/badcases", json={
        "task_id": "T600", "agent_id": "minimal-react",
        "title": "输出缺失", "description": "Agent 没有生成 ok.txt",
        "category": "format", "severity": "P1",
    })
    assert r.status_code == 200, r.text
    b1 = r.json()
    r = client.post("/api/badcases", json={
        "task_id": "T600", "agent_id": "minimal-react",
        "title": "解析失败", "description": "输出格式错误",
        "category": "reasoning", "severity": "P2",
    })
    assert r.status_code == 200, r.text
    b2 = r.json()

    r = client.post("/api/badcases/convert-to-tasks", json={
        "badcase_ids": [b1["id"], b2["id"]],
        "add_to_manifest": False,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["converted"]) == 2, data
    for item in data["converted"]:
        tid = item["new_task_id"]
        assert tid.startswith("T-REG-"), tid
        spec = tmp_path / "tasks" / tid / "spec.yaml"
        assert spec.exists(), spec
        assert "regression" in spec.read_text(encoding="utf-8")
    # 已转化 badcase 再次批量转化应跳过
    r = client.post("/api/badcases/convert-to-tasks", json={
        "badcase_ids": [b1["id"]], "add_to_manifest": False,
    })
    data = r.json()
    assert len(data["skipped"]) == 1, data
    # badcase 已关联回归任务
    rb = client.get("/api/regression-badcases").json()["items"]
    assert len(rb) >= 2


def test_regression_run_and_finalize(client):
    """发起回归 → 批次完成 → 回归记录回写（通过率/退化/基线）。"""
    r = client.post("/api/regression/run", json={"agents": ["minimal-react"], "runs": 1})
    assert r.status_code == 200, r.text
    data = r.json()
    reg_id, batch_id = data["reg_id"], data["batch_id"]
    assert data["total_runs"] == 1

    deadline = time.time() + 20
    done = False
    while time.time() < deadline:
        rb = client.get(f"/api/batches/{batch_id}")
        assert rb.status_code == 200, rb.text
        if rb.json().get("status") == "done":
            done = True
            break
        time.sleep(0.2)
    assert done, "回归批次未在预期时间内完成"
    time.sleep(0.3)  # 等待回归记录回写
    rr = client.get(f"/api/regression/runs/{reg_id}")
    assert rr.status_code == 200, rr.text
    detail = rr.json()
    assert detail["status"] == "done", detail
    assert detail["pass_rate"] > 0, detail
    assert detail["degraded"] == []  # 首次回归无基线
    assert "matrix" in detail

    # 第二次回归：应与第一次对比（基线存在；FakeBackend 稳定无退化）
    r = client.post("/api/regression/run", json={"agents": ["minimal-react"], "runs": 1})
    data = r.json()
    reg2_id, batch2_id = data["reg_id"], data["batch_id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        rb = client.get(f"/api/batches/{batch2_id}")
        if rb.json().get("status") == "done":
            break
        time.sleep(0.2)
    time.sleep(0.3)
    rr2 = client.get(f"/api/regression/runs/{reg2_id}").json()
    assert rr2["baseline_reg_id"] == reg_id, rr2
    assert rr2["degraded"] == []

    # 看板健康状态与趋势
    board = client.get("/api/regression/board").json()
    assert board["regression_run_count"] >= 2
    assert board["health"] == "healthy", board
    trend = client.get("/api/regression/trend").json()
    assert "minimal-react" in trend["agents"]
    assert len(trend["series"]["minimal-react"]) >= 2


def test_regression_schedule_roundtrip(client):
    """定期回归配置读写与调度触发。"""
    r = client.get("/api/regression/schedule")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["enabled"] is False or s["enabled"] is True

    r = client.post("/api/regression/schedule", json={
        "enabled": True, "interval_hours": 2,
        "agents": ["minimal-react"], "runs": 1,
    })
    assert r.status_code == 200, r.text
    s = r.json()["schedule"]
    assert s["enabled"] is True
    assert s["interval_hours"] == 2
    assert s["agents"] == ["minimal-react"]
    assert s.get("next_run_at"), "启用后应设置首次执行时间"

    r = client.post("/api/regression/schedule", json={"enabled": False})
    s = r.json()["schedule"]
    assert s["enabled"] is False

    # 非法后端应被拒绝
    r = client.post("/api/regression/schedule", json={"agents": ["不存在的后端"]})
    assert r.status_code == 400
