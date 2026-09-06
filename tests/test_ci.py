"""M1：agent-eval ci 门禁骨架测试（判定逻辑 / JUnit / Allure / 注入执行）。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from agent_eval.ci import (
    judge_gate,
    judge_task,
    load_gate_config,
    run_gate,
    run_passed,
    write_allure_results,
    write_junit_xml,
)
from agent_eval.runner import RunRecord
from agent_eval.spec import Checkpoint, TaskSpec


def make_record(
    task_id: str = "T001",
    status: str = "completed",
    passed_flags: tuple[bool, ...] = (True,),
    error: str = "",
    usage: dict | None = None,
) -> RunRecord:
    """构造假 RunRecord（不触发真实 LLM/后端）。"""
    verdicts = [
        {"id": f"c{i + 1}", "passed": p, "detail": "ok" if p else "未通过"}
        for i, p in enumerate(passed_flags)
    ]
    passed = sum(1 for v in verdicts if v["passed"])
    total = len(verdicts)
    return RunRecord(
        run_id="r1",
        agent_id="minimal-react",
        agent_ver="test",
        task_id=task_id,
        task_level="L1",
        status=status,
        steps=[],
        duration_s=1.5,
        metrics={
            "score": round(passed / total, 3) if total else 0.0,
            "weight": 1.0,
            "pass_rate": round(passed / total, 3) if total else 0.0,
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2},
        },
        verdicts=verdicts,
        error=error,
        workspace="",
    )


def make_spec(task_id: str, cids: list[str], verifier: str = "deterministic") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=f"{task_id} 测试",
        level="L1",
        description="d",
        fixtures={},
        checkpoints=[Checkpoint(id=cid, type="content_contains", pattern="x") for cid in cids],
        weight=1.0,
        verifier=verifier,
        spec_path=Path(f"/tmp/{task_id}/spec.yaml"),
    )


# ---------- 单 run 通过判定 ----------

def test_run_passed_requires_completed_and_full_pass():
    assert run_passed(make_record(status="completed", passed_flags=(True, True)))
    assert not run_passed(make_record(status="error", passed_flags=(True,)))
    assert not run_passed(make_record(status="completed", passed_flags=(True, False)))
    assert not run_passed(make_record(status="max_steps", passed_flags=(True,)))


# ---------- task 级多数制 ----------

def test_judge_task_majority_pass():
    records = [
        make_record(passed_flags=(True, True)),
        make_record(passed_flags=(True, True)),
        make_record(passed_flags=(True, False)),  # 1 run 挂
    ]
    tr = judge_task(records, task_pass_ratio=0.5)
    assert tr["task_passed"] is True
    assert tr["passed_runs"] == 2
    assert tr["runs"] == 3
    # checkpoint 统计
    assert tr["checkpoints"]["c1"] == {"passed": 3, "total": 3}
    assert tr["checkpoints"]["c2"] == {"passed": 2, "total": 3}


def test_judge_task_majority_fail():
    records = [
        make_record(passed_flags=(True, True)),
        make_record(passed_flags=(True, False)),
        make_record(passed_flags=(True, False)),
    ]
    tr = judge_task(records, task_pass_ratio=0.5)
    assert tr["task_passed"] is False
    assert tr["passed_runs"] == 1


def test_judge_task_threshold_boundary():
    # 4 run 中 2 过：2/4 == 0.5 >= 0.5 -> 通过
    records = [
        make_record(passed_flags=(True,)),
        make_record(passed_flags=(True,)),
        make_record(passed_flags=(False,)),
        make_record(passed_flags=(False,)),
    ]
    assert judge_task(records, task_pass_ratio=0.5)["task_passed"] is True
    # 4 run 中 1 过：0.25 < 0.5 -> 不通过
    records2 = [
        make_record(passed_flags=(True,)),
        make_record(passed_flags=(False,)),
        make_record(passed_flags=(False,)),
        make_record(passed_flags=(False,)),
    ]
    assert judge_task(records2, task_pass_ratio=0.5)["task_passed"] is False


# ---------- gate 级阈值 ----------

def test_judge_gate_thresholds():
    ok = [{"task_passed": True} for _ in range(9)] + [{"task_passed": False}]
    passed, rate = judge_gate(ok, min_pass_rate=0.9)
    assert passed is True and rate == 0.9
    bad = [{"task_passed": True} for _ in range(8)] + [{"task_passed": False} for _ in range(2)]
    passed2, rate2 = judge_gate(bad, min_pass_rate=0.9)
    assert passed2 is False and rate2 == 0.8
    passed3, _ = judge_gate([], min_pass_rate=0.9)
    assert passed3 is False


# ---------- 配置解析 ----------

def test_load_gate_config_real(tmp_path):
    # 复制真实 gate.yaml 到临时位置，避免测试依赖项目根
    cfg_path = Path(__file__).resolve().parent.parent / "ci" / "gate.yaml"
    if not cfg_path.exists():
        pytest.skip("ci/gate.yaml 不存在")
    data = load_gate_config(cfg_path)
    gates = data["gates"]
    assert "core" in gates and "full" in gates
    core = gates["core"]
    assert core["runs"] == 3
    assert core["task_pass_ratio"] == 0.5
    assert core["min_pass_rate"] == 0.9
    # V2.5：core 扩至 12 任务（新增 RAG 检索 T701/T702，采样稳定后纳入卡口）
    assert len(core["tasks"]) == 12
    assert "T001" in core["tasks"]
    assert "T701" in core["tasks"] and "T702" in core["tasks"]


# ---------- JUnit XML ----------

def test_write_junit_xml(tmp_path):
    tasks = {"T001": make_spec("T001", ["c1", "c2"])}
    task_results = [
        {
            "task_id": "T001",
            "runs": 3,
            "passed_runs": 2,
            "task_passed": False,
            "duration_s": 1.5,
            "error": "",
            "checkpoints": {"c1": {"passed": 3, "total": 3}, "c2": {"passed": 2, "total": 3}},
        }
    ]
    out = write_junit_xml("core", [("minimal-react", task_results)], tasks, tmp_path / "junit.xml")
    root = ET.parse(out).getroot()
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.get("name") == "core·minimal-react"
    assert suite.get("tests") == "2"  # checkpoint 级 testcase
    assert suite.get("failures") == "2"  # task 失败 -> 全部 checkpoint 记为失败
    names = [tc.get("name") for tc in suite.findall("testcase")]
    assert names == ["T001::c1", "T001::c2"]
    assert suite.findall("testcase/failure")  # 失败详情


def test_write_junit_xml_passed_task_no_failure(tmp_path):
    tasks = {"T001": make_spec("T001", ["c1"])}
    task_results = [
        {
            "task_id": "T001",
            "runs": 3,
            "passed_runs": 3,
            "task_passed": True,
            "duration_s": 2.0,
            "error": "",
            "checkpoints": {"c1": {"passed": 3, "total": 3}},
        }
    ]
    out = write_junit_xml("core", [("minimal-react", task_results)], tasks, tmp_path / "junit.xml")
    root = ET.parse(out).getroot()
    suite = root.find("testsuite")
    assert suite.get("failures") == "0"
    assert not suite.findall("testcase/failure")


# ---------- Allure results ----------

def test_write_allure_results(tmp_path):
    tasks = {"T001": make_spec("T001", ["c1"]), "T002": make_spec("T002", ["c1"])}
    task_results = [
        {
            "task_id": "T001", "runs": 2, "passed_runs": 2, "task_passed": True,
            "duration_s": 1.0, "error": "",
            "checkpoints": {"c1": {"passed": 2, "total": 2}},
        },
        {
            "task_id": "T002", "runs": 2, "passed_runs": 0, "task_passed": False,
            "duration_s": 1.0, "error": "boom",
            "checkpoints": {"c1": {"passed": 0, "total": 2}},
        },
    ]
    d = write_allure_results(
        "core", [("minimal-react", task_results)], tasks,
        {"gate": "core", "agent": "minimal-react", "model": "deepseek-chat"},
        tmp_path / "allure",
    )
    results = list(d.glob("result-*.json"))
    assert len(results) == 2
    statuses = []
    for f in results:
        r = json.loads(f.read_text(encoding="utf-8"))
        statuses.append(r["status"])
        labels = {x["name"]: x["value"] for x in r["labels"]}
        assert labels["suite"] == "core·minimal-react"
        assert labels["agent"] == "minimal-react"
    assert sorted(statuses) == ["failed", "passed"]
    env = (d / "environment.properties").read_text(encoding="utf-8")
    assert "gate=core" in env
    assert (d / "categories.json").exists()


# ---------- 门禁执行（注入假 run_one） ----------

def test_run_gate_injected_runner(tmp_path):
    cfg = tmp_path / "gate.yaml"
    cfg.write_text(
        "gate:\n"
        "  core:\n"
        "    tasks: [T001, T106]\n"
        "    runs: 2\n"
        "    task_pass_ratio: 0.5\n"
        "    min_pass_rate: 0.5\n",
        encoding="utf-8",
    )

    calls = []

    def fake_run_one(task, agent, config=None, results_dir=None, **kw):
        calls.append(task.id)
        passed = task.id == "T001"  # T001 全过，T106 全挂
        return make_record(
            task_id=task.id, passed_flags=(True,) if passed else (False,),
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )

    result = run_gate(
        "core", agent="minimal-react", model="deepseek-chat",
        config_path=cfg, results_dir=tmp_path / "runs", run_one_impl=fake_run_one,
        track_balance=False,
    )
    assert calls == ["T001", "T001", "T106", "T106"]  # 2 任务 × 2 runs
    by_id = {t["task_id"]: t for t in result["task_results"]}
    assert by_id["T001"]["task_passed"] is True
    assert by_id["T106"]["task_passed"] is False
    assert result["passed"] is True  # 1/2 = 0.5 >= 0.5
    assert result["pass_rate"] == 0.5
    assert result["tokens"] == {"prompt_tokens": 400, "completion_tokens": 80}
    assert result["cost_cny"] > 0


def test_run_gate_balance_diff(tmp_path, monkeypatch):
    """余额差分：跑前 100 → 跑后 99.5，balance_cost_cny=0.5（黑盒后端兜底成本）。"""
    cfg = tmp_path / "gate.yaml"
    cfg.write_text(
        "gate:\n"
        "  core:\n"
        "    tasks: [T001]\n"
        "    runs: 1\n"
        "    task_pass_ratio: 0.5\n"
        "    min_pass_rate: 0.5\n",
        encoding="utf-8",
    )

    def fake_run_one(task, agent, config=None, results_dir=None, **kw):
        return make_record(
            task_id=task.id, passed_flags=(True,),
            usage=None,  # 黑盒后端无 token
        )

    balances = iter([100.0, 99.5])
    monkeypatch.setattr(
        "agent_eval.balance.fetch_balance_cny",
        lambda *a, **k: next(balances),
    )
    result = run_gate(
        "core", agent="deepseek-harness", model="deepseek-chat",
        config_path=cfg, results_dir=tmp_path / "runs", run_one_impl=fake_run_one,
        track_balance=True,
    )
    assert result["balance_start_cny"] == 100.0
    assert result["balance_end_cny"] == 99.5
    assert result["balance_cost_cny"] == 0.5
    # 黑盒后端 token=0 → token 计价为 0，差分补上真实成本
    assert result["cost_cny"] == 0.0
    assert result["balance_cost_cny"] > 0


def test_run_gate_multi_agent_matrix(tmp_path):
    """gate 配置 agents 列表 → 每 agent 独立跑任务集，全部达标 gate 才 PASS。"""
    cfg = tmp_path / "gate.yaml"
    cfg.write_text(
        "gate:\n"
        "  compare:\n"
        "    tasks: [T001, T305]\n"
        "    runs: 2\n"
        "    task_pass_ratio: 0.5\n"
        "    min_pass_rate: 0.5\n"
        "    agents: [minimal-react, deepseek-harness]\n",
        encoding="utf-8",
    )

    def fake_run_one(task, agent, config=None, results_dir=None, **kw):
        # minimal-react：T305 全挂；deepseek-harness：全过
        fail = agent == "minimal-react" and task.id == "T305"
        return make_record(
            task_id=task.id, passed_flags=(not fail,),
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    result = run_gate(
        "compare", agent="ignored", model="deepseek-chat",
        config_path=cfg, results_dir=tmp_path / "runs", run_one_impl=fake_run_one,
        track_balance=False,
    )
    assert result["agents"] == ["minimal-react", "deepseek-harness"]
    assert result["agent"] == "multi-agent"
    assert len(result["agent_results"]) == 2
    by_agent = {ar["agent"]: ar for ar in result["agent_results"]}
    assert by_agent["minimal-react"]["pass_rate"] == 0.5   # T001 过、T305 挂
    assert by_agent["deepseek-harness"]["pass_rate"] == 1.0
    assert by_agent["minimal-react"]["passed"] is True     # 0.5 >= 0.5 各自达标
    assert result["passed"] is True                        # 全部达标
    assert result["pass_rate"] == 0.5                      # 取最小值
    # 任务矩阵：同一任务在两个 agent 下各自记录
    assert result["task_results"] == by_agent["minimal-react"]["task_results"]


def test_run_gate_multi_agent_fail_if_any(tmp_path):
    """任一 agent 未达标 → gate FAIL（保守卡口语义）。"""
    cfg = tmp_path / "gate.yaml"
    cfg.write_text(
        "gate:\n"
        "  compare:\n"
        "    tasks: [T001]\n"
        "    runs: 1\n"
        "    task_pass_ratio: 0.5\n"
        "    min_pass_rate: 0.9\n"
        "    agents: [minimal-react, deepseek-harness]\n",
        encoding="utf-8",
    )

    def fake_run_one(task, agent, config=None, results_dir=None, **kw):
        fail = agent == "minimal-react"
        return make_record(task_id=task.id, passed_flags=(not fail,), usage=None)

    result = run_gate(
        "compare", agent="x", model="deepseek-chat",
        config_path=cfg, results_dir=tmp_path / "runs", run_one_impl=fake_run_one,
        track_balance=False,
    )
    assert result["agent_results"][0]["passed"] is False  # minimal-react 0/1
    assert result["agent_results"][1]["passed"] is True   # deepseek-harness 1/1
    assert result["passed"] is False                      # 任一失败 → gate FAIL
