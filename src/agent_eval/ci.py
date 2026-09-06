"""agent-eval ci —— 无头 CI 质量门禁（M1 骨架）。

流程：
1. 加载 ci/gate.yaml（core / full 两个 gate，配置驱动）
2. 对 gate 内任务逐个执行 runs 次评测（复用 runner.run_one，落盘 results/runs/）
3. task 级多数制判定：通过 run 数 / runs >= task_pass_ratio 视为该任务通过
   （单 run 通过 = 该 run 全部校验点通过，即 metrics.pass_rate >= 1.0）
4. gate 通过率 = 通过任务数 / 总任务数 >= min_pass_rate 则门禁 PASS，否则 FAIL
5. 输出：终端汇总 + JUnit XML（checkpoint 级 testcase）+ Allure results + 汇总 JSON
6. 退出码：门禁 FAIL -> 1（GitHub Actions 中非 0 退出码 + branch protection 阻断合并）

骨架边界（M1）：仅做命令骨架与报告输出；GitHub Actions workflow 接线见
ci/github-actions.example.yml（由使用方仓库引用本命令）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from agent_eval.log import get_logger
from agent_eval.spec import TaskSpec, find_tasks_dir, load_task_pack

logger = get_logger(__name__)

DEFAULT_CONFIG = "ci/gate.yaml"
DEFAULT_JUNIT_XML = "results/junit.xml"
DEFAULT_ALLURE_DIR = "results/allure-results"
DEFAULT_REPORT_JSON = "results/ci-report.json"


# ---------- 配置 ----------

def load_gate_config(path: Path | str = DEFAULT_CONFIG) -> dict:
    """读取 gate.yaml；缺失或格式错误时抛出带说明的异常。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"门禁配置不存在: {p}（可用 --config 指定，参考 ci/gate.yaml）")
    # gate.yaml 是 YAML；无第三方依赖时做最小解析（顶层缩进 + 数组 + 注释）
    data = _parse_gate_yaml(p.read_text(encoding="utf-8"))
    gates = data.get("gate") or {}
    if not isinstance(gates, dict) or not gates:
        raise ValueError(f"门禁配置 {p} 缺少 gate 段")
    out: dict[str, dict] = {}
    for name, cfg in gates.items():
        if not isinstance(cfg, dict):
            continue
        tasks = cfg.get("tasks", [])
        agents = cfg.get("agents")
        out[name] = {
            "tasks": tasks,  # list[str] 或 "*"
            "runs": int(cfg.get("runs", 3)),
            "task_pass_ratio": float(cfg.get("task_pass_ratio", 0.5)),
            "min_pass_rate": float(cfg.get("min_pass_rate", 0.9)),
            "agents": agents,  # 可选：多 agent 横向对比列表（未设 = 单 agent 由 --agent 指定）
        }
    return {"gates": out, "path": str(p)}


def _parse_gate_yaml(text: str) -> dict:
    """解析 ci/gate.yaml（固定两层结构：gate -> <name> -> keys，tasks 为列表项）。"""
    result: dict[str, Any] = {"gate": {}}
    gates = result["gate"]
    current: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or line.strip() == "---":
            continue
        indent = len(line) - len(line.lstrip())
        content = line.strip()
        if indent == 0 and content.startswith("gate:"):
            continue
        if indent == 2 and content.endswith(":"):
            current = content[:-1].strip()
            gates[current] = {}
            continue
        if current is None:
            continue
        if content.startswith("- "):
            gates[current].setdefault("tasks", []).append(_scalar(content[2:]))
            continue
        if ":" in content:
            key, val = content.split(":", 1)
            key = key.strip()
            if not val.strip():
                continue  # 空值键（如 tasks:）由列表项创建
            gates[current][key] = _scalar(val)
    return result


def _scalar(v: str):
    v = v.strip()
    if v == "*":
        return v
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


# ---------- 判定 ----------

def run_passed(record) -> bool:
    """单次 run 通过 = 该 run 全部校验点通过（error/超时视为未通过）。"""
    if record.status != "completed":
        return False
    return float(record.metrics.get("pass_rate", 0.0)) >= 1.0


def judge_task(records: list, task_pass_ratio: float) -> dict:
    """task 级多数制判定。records: 同一任务的 runs 次 RunRecord。"""
    n = len(records)
    passed_runs = sum(1 for r in records if run_passed(r))
    task_passed = (passed_runs / n) >= task_pass_ratio if n else False
    # checkpoint 级通过情况（用于 JUnit/Allure 细粒度展示）
    cp_stats: dict[str, dict] = {}
    for r in records:
        for v in r.verdicts or []:
            cid = str(v.get("id", "?") or "?")
            s = cp_stats.setdefault(cid, {"passed": 0, "total": 0})
            s["total"] += 1
            if v.get("passed"):
                s["passed"] += 1
    return {
        "task_id": records[0].task_id if records else "?",
        "level": records[0].task_level if records else "",
        "runs": n,
        "passed_runs": passed_runs,
        "task_passed": task_passed,
        "checkpoints": cp_stats,
        "duration_s": round(sum(r.duration_s for r in records) / n, 2) if n else 0.0,
        "error": next((r.error for r in records if r.error), ""),
    }


def judge_gate(task_results: list[dict], min_pass_rate: float) -> tuple[bool, float]:
    """gate 判定：通过任务数 / 总任务数 >= min_pass_rate。返回 (是否通过, 通过率)。"""
    if not task_results:
        return False, 0.0
    passed = sum(1 for t in task_results if t["task_passed"])
    rate = passed / len(task_results)
    return rate >= min_pass_rate, round(rate, 3)


# ---------- 报告输出 ----------

def _checkpoint_cases(task_result: dict, gate_name: str, task_spec: TaskSpec,
                        agent: str | None = None) -> list[dict]:
    """生成 checkpoint 级 testcase 描述（含 llm_judge 追加判分）。"""
    cids = [cp.id for cp in task_spec.checkpoints]
    if task_spec.verifier == "llm_judge":
        cids.append("llm_judge")
    cases = []
    for cid in cids:
        stat = task_result["checkpoints"].get(cid, {"passed": 0, "total": 0})
        detail = (
            f"校验点 {cid} 在 {task_result['runs']} 次 run 中通过 {stat['passed']}/{stat['total']}"
            f"（task 级{'通过' if task_result['task_passed'] else '未通过'}，"
            f"run 通过率 {task_result['passed_runs']}/{task_result['runs']}）"
        )
        if task_result["error"]:
            detail += f"；run error: {task_result['error'][:200]}"
        cases.append(
            {
                "name": f"{task_result['task_id']}::{cid}",
                "classname": (
                    f"{gate_name}.{agent}.{task_result['task_id']}" if agent
                    else f"{gate_name}.{task_result['task_id']}"
                ),
                "time": task_result["duration_s"],
                "passed": task_result["task_passed"],
                "detail": detail,
            }
        )
    return cases


def write_junit_xml(gate_name: str, groups: list[tuple[str, list[dict]]],
                    tasks: dict[str, TaskSpec], path: Path | str) -> Path:
    """生成 JUnit XML（checkpoint 级 testcase）。

    groups: [(agent, task_results), ...]；单 agent 传 [("minimal-react", task_results)]。
    每个 agent 一个 testsuite（多 agent 横向对比时按 agent 区分）。
    """
    import xml.etree.ElementTree as ET

    suites = ET.Element("testsuites")
    for agent, task_results in groups:
        cases = []
        for tr in task_results:
            spec = tasks.get(tr["task_id"])
            if spec is None:
                continue
            cases.extend(_checkpoint_cases(tr, gate_name, spec, agent=agent))
        suite_name = f"{gate_name}·{agent}" if agent else gate_name
        suite = ET.SubElement(
            suites, "testsuite",
            {
                "name": suite_name,
                "tests": str(len(cases)),
                "failures": str(sum(1 for c in cases if not c["passed"])),
                "errors": "0",
                "skipped": "0",
                "time": str(round(sum(c["time"] for c in cases), 2)),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        )
        for c in cases:
            tc = ET.SubElement(
                suite, "testcase",
                {"name": c["name"], "classname": c["classname"], "time": f"{c['time']:.3f}"},
            )
            if not c["passed"]:
                ET.SubElement(tc, "failure", {"message": f"{c['name']} 未通过"}).text = c["detail"]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(suites, encoding="unicode"),
        encoding="utf-8",
    )
    return p


def write_allure_results(gate_name: str, groups: list[tuple[str, list[dict]]],
                         tasks: dict[str, TaskSpec], env: dict,
                         allure_dir: Path | str) -> Path:
    """生成 Allure results 目录（result-*.json + environment.properties + categories.json）。

    groups: [(agent, task_results), ...]；多 agent 时 suite/agent label 按组区分。
    """
    d = Path(allure_dir)
    d.mkdir(parents=True, exist_ok=True)
    for agent, task_results in groups:
        for tr in task_results:
            spec = tasks.get(tr["task_id"])
            if spec is None:
                continue
            for c in _checkpoint_cases(tr, gate_name, spec, agent=agent):
                start = int(time.time() * 1000) - int(c["time"] * 1000)
                result = {
                    "uuid": uuid.uuid4().hex,
                    "name": c["name"],
                    "status": "passed" if c["passed"] else "failed",
                    "statusDetails": ({"message": c["detail"]} if not c["passed"] else {}),
                    "stage": "finished",
                    "start": start,
                    "stop": start + int(c["time"] * 1000),
                    "labels": [
                        {"name": "suite", "value": f"{gate_name}·{agent}" if agent else gate_name},
                        {"name": "task", "value": tr["task_id"]},
                        {"name": "agent", "value": agent or env.get("agent", "")},
                        {"name": "model", "value": env.get("model", "")},
                        {"name": "gate", "value": gate_name},
                    ],
                    "attachments": [],
                }
                (d / f"result-{result['uuid']}.json").write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
    (d / "environment.properties").write_text(
        "\n".join(f"{k}={v}" for k, v in env.items()), encoding="utf-8"
    )
    (d / "categories.json").write_text(
        json.dumps(
            [
                {
                    "name": "门禁失败",
                    "matchedStatuses": ["failed"],
                    "messageRegex": ".*未通过.*",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return d


# ---------- 门禁执行 ----------

def run_gate(
    gate_name: str,
    *,
    agent: str = "minimal-react",
    model: str = "deepseek-chat",
    runs_override: int | None = None,
    config_path: Path | str = DEFAULT_CONFIG,
    results_dir: Path | str = "results/runs",
    run_one_impl=None,
    track_balance: bool = True,
) -> dict:
    """执行一个 gate（core/full），返回完整结果 dict。

    单 agent：--agent 指定；多 agent 横向对比：gate 配置可带可选 agents 列表
    （如 core: agents: [minimal-react, deepseek-harness]），每个 agent 独立跑
    同一任务集并各自判定，全部 agent 达标 gate 才算 PASS。

    run_one_impl：可注入替代实现（测试用）；默认 runner.run_one。
    track_balance：开启"余额差分"真实成本采集（黑盒后端 token 成本为 0 时的兜底）。
    """
    from agent_eval.costing import pricing_for
    from agent_eval.runner import run_one as _run_one
    from agent_eval.balance import fetch_balance_cny

    run_one_fn = run_one_impl or _run_one
    results_dir = Path(results_dir)
    cfg = load_gate_config(config_path)
    gates = cfg["gates"]
    if gate_name not in gates:
        raise ValueError(f"gate 不存在: {gate_name}（可用: {sorted(gates)}）")
    g = gates[gate_name]

    tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
    task_ids: list[str] = []
    if g["tasks"] == "*" or g["tasks"] == ["*"]:
        task_ids = sorted(tasks)
    else:
        task_ids = [str(t) for t in g["tasks"]]
        missing = [t for t in task_ids if t not in tasks]
        if missing:
            raise ValueError(f"gate {gate_name} 引用了不存在的任务: {missing}")

    # 多 agent 横向对比：gate 配置 agents 列表优先；否则单 agent（--agent）
    agents = [str(a) for a in g["agents"]] if g.get("agents") else [agent]
    runs = runs_override or g["runs"]
    price = pricing_for()

    logger.info(
        "CI 门禁开始 | gate=%s agents=%s tasks=%d runs=%d task_pass_ratio=%.2f min_pass_rate=%.2f",
        gate_name, agents, len(task_ids), runs, g["task_pass_ratio"], g["min_pass_rate"],
    )

    def _run_one_agent(ag: str) -> dict:
        """单个 agent 在 gate 任务集上执行与判定（含余额差分）。"""
        logger.info("门禁 agent 开始 | agent=%s tasks=%d runs=%d", ag, len(task_ids), runs)
        bal_start = fetch_balance_cny() if track_balance else None
        a_start = time.time()
        a_task_results: list[dict] = []
        a_tokens = {"prompt_tokens": 0, "completion_tokens": 0}
        for tid in task_ids:
            records = []
            for _ in range(runs):
                rec = run_one_fn(
                    tasks[tid], ag,
                    config={"agent": {"model": model}},
                    results_dir=results_dir,
                )
                records.append(rec)
                u = rec.metrics.get("usage") or {}
                a_tokens["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
                a_tokens["completion_tokens"] += u.get("completion_tokens", 0) or 0
            tr = judge_task(records, g["task_pass_ratio"])
            tr["expected_runs"] = runs
            a_task_results.append(tr)
            logger.info(
                "  任务 %s | %s (%d/%d run通过)",
                tid, "PASS" if tr["task_passed"] else "FAIL",
                tr.get("passed_runs", 0), runs,
            )
        a_passed, a_rate = judge_gate(a_task_results, g["min_pass_rate"])
        a_cost = round(
            a_tokens["prompt_tokens"] / 1e6 * price["input_cny_per_m"]
            + a_tokens["completion_tokens"] / 1e6 * price["output_cny_per_m"],
            4,
        )
        bal_end = fetch_balance_cny() if track_balance else None
        a_bal_cost = None
        if bal_start is not None and bal_end is not None:
            a_bal_cost = max(round(bal_start - bal_end, 4), 0.0)
        logger.info(
            "门禁 agent 完成 | agent=%s passed=%s pass_rate=%.3f duration=%.1fs tokens=%d/%d cost=¥%.4f bal=¥%s",
            ag, a_passed, a_rate, time.time() - a_start,
            a_tokens["prompt_tokens"], a_tokens["completion_tokens"], a_cost,
            f"{a_bal_cost:.4f}" if a_bal_cost is not None else "N/A",
        )
        return {
            "agent": ag,
            "model": model,
            "runs": runs,
            "task_results": a_task_results,
            "passed": bool(a_passed),
            "pass_rate": a_rate,
            "duration_s": round(time.time() - a_start, 2),
            "tokens": a_tokens,
            "cost_cny": a_cost,
            "balance_cost_cny": a_bal_cost,
            "balance_start_cny": bal_start,
            "balance_end_cny": bal_end,
        }

    started = time.time()
    agent_results = [_run_one_agent(ag) for ag in agents]

    total_tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    for ar in agent_results:
        total_tokens["prompt_tokens"] += ar["tokens"]["prompt_tokens"]
        total_tokens["completion_tokens"] += ar["tokens"]["completion_tokens"]
    all_passed = all(ar["passed"] for ar in agent_results)
    all_rate = round(min(ar["pass_rate"] for ar in agent_results), 3)
    bal_costs = [ar["balance_cost_cny"] for ar in agent_results]
    total_duration = time.time() - started
    logger.info(
        "CI 门禁完成 | gate=%s %s | pass_rate=%.3f (阈值%.2f) | duration=%.1fs | tokens=%d/%d | agents=%d",
        gate_name, "PASS" if all_passed else "FAIL",
        all_rate, g["min_pass_rate"], total_duration,
        total_tokens["prompt_tokens"], total_tokens["completion_tokens"], len(agents),
    )
    return {
        "gate": gate_name,
        "agent": agents[0] if len(agents) == 1 else "multi-agent",
        "agents": agents,
        "model": model,
        "runs": runs,
        "task_pass_ratio": g["task_pass_ratio"],
        "min_pass_rate": g["min_pass_rate"],
        "task_ids": task_ids,
        "task_results": agent_results[0]["task_results"],
        "agent_results": agent_results,
        "passed": bool(all_passed),
        "pass_rate": all_rate,
        "duration_s": round(time.time() - started, 2),
        "tokens": total_tokens,
        "cost_cny": round(sum(ar["cost_cny"] for ar in agent_results), 4),
        "balance_cost_cny": round(sum(c for c in bal_costs if c is not None), 4)
        if any(c is not None for c in bal_costs) else None,
        "balance_start_cny": agent_results[0]["balance_start_cny"],
        "balance_end_cny": agent_results[-1]["balance_end_cny"],
        "config_path": cfg["path"],
    }

def run_gate_cli(
    gate_name: str,
    *,
    agent: str,
    model: str,
    runs_override: int | None,
    config_path: Path | str,
    junit_xml: Path | str,
    allure_dir: Path | str,
    report_json: Path | str,
    results_dir: Path | str,
) -> bool:
    """CLI 入口：执行门禁、打印汇总、写报告文件；返回门禁是否通过。"""
    from agent_eval.costing import pricing_for  # noqa: F401  (报告口径)

    result = run_gate(
        gate_name, agent=agent, model=model, runs_override=runs_override,
        config_path=config_path, results_dir=results_dir,
    )
    tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}

    # 终端汇总（多 agent 横向对比：每个 agent 一张表）
    typer_like = print
    agent_results = result.get("agent_results") or [
        {
            "agent": result["agent"],
            "model": result["model"],
            "runs": result["runs"],
            "task_results": result["task_results"],
            "passed": result["passed"],
            "pass_rate": result["pass_rate"],
            "duration_s": result["duration_s"],
            "cost_cny": result["cost_cny"],
            "balance_cost_cny": result.get("balance_cost_cny"),
            "balance_start_cny": result.get("balance_start_cny"),
            "balance_end_cny": result.get("balance_end_cny"),
            "tokens": result["tokens"],
        }
    ]
    for ar in agent_results:
        typer_like(
            f"\n== agent-eval ci · gate={result['gate']} · {ar['agent']} @ {ar['model']} =="
        )
        typer_like(f"{'task':<7}{'run通过':<9}{'判定':<6}{'耗时(s)':<10}{'校验点通过':<14}")
        typer_like("-" * 52)
        for tr in ar["task_results"]:
            cp = ",".join(
                f"{cid}:{s['passed']}/{s['total']}" for cid, s in tr["checkpoints"].items()
            )
            typer_like(
                f"{tr['task_id']:<7}{tr['passed_runs']}/{tr['runs']:<6}"
                f"{'PASS' if tr['task_passed'] else 'FAIL':<6}"
                f"{tr['duration_s']:<10.2f}{cp:<14}"
            )
        status = "PASS" if ar["passed"] else "FAIL"
        typer_like("-" * 52)
        typer_like(
            f"{ar['agent']} 通过率: {ar['pass_rate']} / 阈值 {result['min_pass_rate']}  ->  {status}"
        )
        # 成本口径：余额差分（真实扣费）优先；否则 token 计价；都无则标注未采集
        bal_cost = ar.get("balance_cost_cny")
        if bal_cost is not None:
            cost_desc = (
                f"成本 ¥{bal_cost:.4f}（余额差分，余额 "
                f"{ar.get('balance_start_cny')} → {ar.get('balance_end_cny')}）"
            )
        elif ar["cost_cny"] > 0:
            cost_desc = f"成本约 ¥{ar['cost_cny']:.4f}（token 计价）"
        else:
            cost_desc = "成本未采集（无 DEEPSEEK_API_KEY 或无 token 计量）"
        typer_like(
            f"耗时 {ar['duration_s']}s · token {ar['tokens']['prompt_tokens']:,}/"
            f"{ar['tokens']['completion_tokens']:,} · {cost_desc}"
        )
    status = "PASS" if result["passed"] else "FAIL"
    typer_like(f"\ngate 判定: {' / '.join(result['agents'])} 全部达标 ->  {status}")

    # 报告文件（多 agent 时按 agent 分组；JUnit 每个 agent 一个 suite）
    groups = [(ar["agent"], ar["task_results"]) for ar in agent_results]
    junit_path = write_junit_xml(result["gate"], groups, tasks, junit_xml)
    allure_path = write_allure_results(
        result["gate"], groups, tasks,
        {
            "gate": result["gate"],
            "agent": result["agent"],
            "model": result["model"],
            "runs": result["runs"],
            "task_pass_ratio": result["task_pass_ratio"],
            "min_pass_rate": result["min_pass_rate"],
            "pass_rate": result["pass_rate"],
            "status": status,
            "cost_cny": result["cost_cny"],
            "balance_cost_cny": result.get("balance_cost_cny"),
            "duration_s": result["duration_s"],
        },
        allure_dir,
    )
    rp = Path(report_json)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    typer_like(f"\n报告: JUnit {junit_path} · Allure {allure_path} · 汇总 {rp}")
    return result["passed"]
