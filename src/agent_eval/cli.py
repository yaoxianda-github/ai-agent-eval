"""agent-eval 命令行入口。

命令：list-tasks / run / ci / report / convert / taskpack / workbench / dreaming / preflight。

V3.9：CLI 能力开放——所有命令支持 --json 结构化输出，新增 preflight 预检命令，
统一退出码（0通过/1未通过/2参数错误/3API Key问题/4超时/5内部错误），
方便外部系统、其他 agent、CI 流水线直接调用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from agent_eval.log import setup_logging

app = typer.Typer(help="通用 AI Agent 评测框架")

# CLI 入口统一初始化日志（控制台 + results/logs/ 文件）；幂等，重复调用安全
setup_logging()

# 退出码常量
EXIT_OK = 0          # 成功 / 评测全部通过
EXIT_FAIL = 1        # 评测未通过（有失败 checkpoint）
EXIT_PARAM = 2       # 参数错误 / 任务不存在
EXIT_API_KEY = 3     # API Key 缺失或无效
EXIT_TIMEOUT = 4     # 执行超时
EXIT_INTERNAL = 5    # 内部错误


@app.command("list-tasks")
def list_tasks(
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON（适合程序调用）"),
) -> None:
    """列出任务包中的所有任务（读取 manifest + 各 spec.yaml）。"""
    from agent_eval.spec import find_tasks_dir, load_task_pack
    from agent_eval.costing import estimate_cost, load_benchmark

    tasks_dir = find_tasks_dir()
    try:
        tasks = load_task_pack(tasks_dir)
    except FileNotFoundError as exc:
        typer.echo(f"错误：{exc}")
        raise typer.Exit(code=EXIT_PARAM)

    bench = load_benchmark()
    default_agent = "minimal-react"

    if json_output:
        task_list = []
        for t in sorted(tasks, key=lambda x: x.id):
            est = estimate_cost(
                default_agent, t.id, level=t.level, verifier=t.verifier, runs=1, benchmark=bench
            )
            task_list.append({
                "id": t.id,
                "title": t.title,
                "level": t.level,
                "weight": t.weight,
                "verifier": t.verifier,
                "risk_level": getattr(t, "risk_level", None),
                "risk_category": getattr(t, "risk_category", None),
                "estimated_cost_cny": round(est["cost_cny"], 4),
                "cost_source": est["source"],
                "checkpoints_count": len(getattr(t, "checkpoints", [])),
            })
        typer.echo(json.dumps({
            "tasks_dir": str(tasks_dir),
            "total": len(task_list),
            "default_agent": default_agent,
            "tasks": task_list,
        }, ensure_ascii=False, indent=2))
        return

    typer.echo(f"任务包: {tasks_dir}")
    typer.echo(f"{'ID':<6}{'级别':<5}{'权重':<7}{'判定':<13}{'预计成本':<14}标题")
    typer.echo("-" * 78)
    for t in sorted(tasks, key=lambda x: x.id):
        est = estimate_cost(
            default_agent, t.id, level=t.level, verifier=t.verifier, runs=1, benchmark=bench
        )
        mark = "" if est["source"] == "measured" else "~"
        typer.echo(
            f"{t.id:<6}{t.level:<5}{t.weight:<7.1f}{t.verifier:<13}"
            f"{mark}¥{est['cost_cny']:<12.4f}{t.title}"
        )
    typer.echo(
        "\n共 {} 个任务（预计成本：{} 基准，官方口径 ¥2/M 输入 + ¥3/M 输出，~ 为估算）".format(
            len(tasks), default_agent
        )
    )


@app.command("run")
def run(
    task: Optional[str] = typer.Option(None, "--task", help="任务 ID，如 T001（--stdin 模式下可省略）"),
    agent: str = typer.Option("minimal-react", "--agent", help="后端 Agent 名称"),
    model: str = typer.Option("deepseek-chat", "--model", help="LLM 模型名"),
    timeout: Optional[int] = typer.Option(None, "--timeout", help="覆盖任务默认超时（秒）"),
    runs: int = typer.Option(1, "--runs", min=1, max=20, help="运行次数（采样，对抗非确定性）"),
    stdin: bool = typer.Option(False, "--stdin", help="从 stdin 读取任务 spec（YAML/JSON），动态创建任务"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON（适合程序调用）"),
    quiet: bool = typer.Option(False, "--quiet", help="静默模式，只输出最终结果（配合 --json）"),
) -> None:
    """对单个任务执行评测；--runs N 时输出采样统计（best/mean/std/pass_rate）。

    --stdin 模式：从 stdin 读取任务 spec（YAML 或 JSON），动态创建临时任务执行评测，
    适合外部系统通过管道传入动态任务。

    退出码：0=通过，1=未通过，2=参数错误，3=API Key问题，4=超时，5=内部错误。
    """
    from agent_eval.runner import run_one
    from agent_eval.spec import find_tasks_dir, load_task_pack, TaskSpec
    from agent_eval.stats import summarize_scores
    from agent_eval.costing import estimate_cost
    import yaml
    import tempfile

    if stdin:
        # 从 stdin 读取任务 spec（YAML 或 JSON）
        spec_text = sys.stdin.read()
        if not spec_text.strip():
            msg = "错误：--stdin 模式下 stdin 为空"
            if json_output:
                typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
            else:
                typer.echo(msg)
            raise typer.Exit(code=EXIT_PARAM)
        try:
            spec_data = yaml.safe_load(spec_text)
        except yaml.YAMLError:
            try:
                spec_data = json.loads(spec_text)
            except json.JSONDecodeError:
                msg = "错误：stdin 内容不是有效的 YAML 或 JSON"
                if json_output:
                    typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
                else:
                    typer.echo(msg)
                raise typer.Exit(code=EXIT_PARAM)
        # 构造临时任务：创建临时目录，写入 spec.yaml 和 fixtures
        import atexit
        import shutil
        tmpdir = tempfile.mkdtemp(prefix="agent-eval-stdin-")
        # 注册退出时清理临时目录
        atexit.register(shutil.rmtree, tmpdir, ignore_errors=True)
        task_id = spec_data.get("id", "STDIN")
        spec_data["id"] = task_id
        (Path(tmpdir) / "spec.yaml").write_text(yaml.dump(spec_data, allow_unicode=True), encoding="utf-8")
        # 从 spec 中提取 fixtures（如果有 inline_fixtures 字段）
        fixtures = spec_data.get("inline_fixtures", {})
        if fixtures:
            fixtures_dir = Path(tmpdir) / "fixtures"
            fixtures_dir.mkdir(exist_ok=True)
            fixtures_root = fixtures_dir.resolve()
            for rel_path, content in fixtures.items():
                fpath = (fixtures_dir / rel_path).resolve()
                # 安全校验：写入路径必须在 fixtures 目录内，防止路径遍历
                if not str(fpath).startswith(str(fixtures_root) + "/") and fpath != fixtures_root:
                    msg = f"错误：inline_fixtures 路径越界（禁止路径遍历）: {rel_path}"
                    if json_output:
                        typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
                    else:
                        typer.echo(msg)
                    raise typer.Exit(code=EXIT_PARAM)
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")
        # 加载临时任务
        t = TaskSpec.from_yaml(Path(tmpdir) / "spec.yaml")
        if not json_output and not quiet:
            typer.echo(f"stdin 任务: {t.id} - {t.title} ({t.level})")
    else:
        if not task:
            msg = "错误：必须指定 --task 或使用 --stdin 模式"
            if json_output:
                typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
            else:
                typer.echo(msg)
            raise typer.Exit(code=EXIT_PARAM)
        tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
        if task not in tasks:
            msg = f"错误：找不到任务 {task}，可用: {sorted(tasks)}"
            if json_output:
                typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
            else:
                typer.echo(msg)
            raise typer.Exit(code=EXIT_PARAM)
        t = tasks[task]

    # 执行前提示预计 LLM 成本（实测基准 / 分级估算）
    est = estimate_cost(
        agent, t.id, level=t.level, verifier=t.verifier, runs=runs, model=model
    )
    src = "实测基准" if est["source"] == "measured" else "分级估算"
    if not json_output and not quiet:
        typer.echo(
            f"预计成本: ¥{est['cost_cny']:.4f}（{agent} × {runs} run · {src} · {est['note']}）"
        )

    config: dict = {"agent": {"model": model}}
    if timeout is not None:
        config["agent"]["timeout_s"] = timeout

    if runs <= 1:
        rec = run_one(t, agent, config=config)
        if json_output:
            _emit_run_json(rec, est)
        else:
            _print_record(rec)
        # 退出码：根据 status 和 pass_rate 判断
        if rec.status == "timeout":
            raise typer.Exit(code=EXIT_TIMEOUT)
        if rec.status == "error":
            if "API Key" in (rec.error or "") or "api_key" in (rec.error or "").lower():
                raise typer.Exit(code=EXIT_API_KEY)
            raise typer.Exit(code=EXIT_INTERNAL)
        # completed / max_steps：检查是否通过
        passed = sum(1 for v in rec.verdicts if v.get("passed"))
        total = len(rec.verdicts)
        if total > 0 and passed < total:
            raise typer.Exit(code=EXIT_FAIL)
        raise typer.Exit(code=EXIT_OK)

    # 多次采样
    records = []
    for i in range(1, runs + 1):
        rec = run_one(t, agent, config=config)
        records.append(rec)
        score = rec.metrics.get("score", 0.0)
        if not json_output and not quiet:
            typer.echo(f"run {i}/{runs}: {rec.run_id}  {rec.status}  {rec.duration_s}s  score={score}")
    stats = summarize_scores([r.metrics.get("score", 0.0) for r in records])
    pass_count = sum(1 for r in records if all(v.get("passed") for v in r.verdicts) if r.verdicts)
    pass_rate = pass_count / len(records) if records else 0.0

    if json_output:
        typer.echo(json.dumps({
            "task_id": task,
            "agent": agent,
            "model": model,
            "runs": runs,
            "estimated_cost_cny": round(est["cost_cny"], 4),
            "stats": {
                "n": stats["n"],
                "best": stats["best"],
                "mean": stats["mean"],
                "std": stats["std"],
                "pass_rate": pass_rate,
            },
            "runs": [_run_to_dict(r) for r in records],
        }, ensure_ascii=False, indent=2))
    else:
        typer.echo(
            f"统计 (N={stats['n']}): best={stats['best']} mean={stats['mean']} "
            f"std={stats['std']} pass_rate={pass_rate:.0%}"
        )
    # 多次采样：pass_rate < 100% 视为未通过
    if pass_rate < 1.0:
        raise typer.Exit(code=EXIT_FAIL)
    raise typer.Exit(code=EXIT_OK)


def _run_to_dict(rec) -> dict:
    """将 RunRecord 转为适合 JSON 输出的精简字典。"""
    usage = rec.metrics.get("usage") or {}
    return {
        "run_id": rec.run_id,
        "task_id": rec.task_id,
        "agent": rec.agent_id,
        "agent_version": rec.agent_ver,
        "status": rec.status,
        "duration_s": rec.duration_s,
        "steps_count": len(rec.steps),
        "score": rec.metrics.get("score", 0.0),
        "weight": rec.metrics.get("weight", 0.0),
        "pass_rate": (
            sum(1 for v in rec.verdicts if v.get("passed")) / len(rec.verdicts)
            if rec.verdicts else None
        ),
        "verdicts": rec.verdicts,
        "tokens": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
        "cost_cny": rec.metrics.get("cost_cny"),
        "failure_attribution": rec.metrics.get("failure_attribution"),
        "hard_gate_blocked": rec.metrics.get("hard_gate_blocked", False),
        "confidence": rec.metrics.get("confidence"),
        "error": rec.error or None,
    }


def _emit_run_json(rec, est: dict) -> None:
    """输出单次运行的 JSON 结果。"""
    out = _run_to_dict(rec)
    out["estimated_cost_cny"] = round(est["cost_cny"], 4)
    out["cost_source"] = est["source"]
    typer.echo(json.dumps(out, ensure_ascii=False, indent=2))


def _print_record(record) -> None:
    typer.echo(f"run_id: {record.run_id}")
    typer.echo(f"task:   {record.task_id} ({record.task_level})")
    typer.echo(f"agent:  {record.agent_id} @ {record.agent_ver}")
    typer.echo(
        f"status: {record.status}   duration: {record.duration_s}s   steps: {len(record.steps)}"
    )
    if record.error:
        typer.echo(f"error:  {record.error}")
    if record.verdicts:
        passed = sum(1 for v in record.verdicts if v.get("passed"))
        total = len(record.verdicts)
        typer.echo(f"verdict: {passed}/{total} 通过")
        for v in record.verdicts:
            mark = "PASS" if v.get("passed") else "FAIL"
            typer.echo(f"  [{mark}] {v.get('id')}  {v.get('detail')}")
        score = record.metrics.get("score", 0)
        weight = record.metrics.get("weight", 0)
        typer.echo(f"score:  {score} / {weight}")
    typer.echo(f"detail: {record.workspace}" if record.workspace else "detail: （工作目录已清理）")


@app.command("preflight")
def preflight(
    task: str = typer.Option(..., "--task", help="任务 ID，如 T001"),
    agent: str = typer.Option("minimal-react", "--agent", help="后端 Agent 名称"),
    model: str = typer.Option("deepseek-chat", "--model", help="LLM 模型名"),
    runs: int = typer.Option(1, "--runs", min=1, max=20, help="运行次数（影响成本预估）"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """执行前预检：检查 API Key、模型连通性、任务完整性、成本预估。

    在实际评测前确认所有前置条件，避免跑了一半才发现配置问题。
    退出码：0=就绪，3=API Key问题，2=参数/任务错误。
    """
    from agent_eval.backends import get_backend
    from agent_eval.spec import find_tasks_dir, load_task_pack
    from agent_eval.costing import estimate_cost

    result = {
        "task": task,
        "agent": agent,
        "model": model,
        "runs": runs,
        "checks": {},
        "ready": True,
        "errors": [],
    }

    # 1. 检查任务是否存在
    tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
    if task not in tasks:
        result["checks"]["task_exists"] = {"ok": False, "message": f"任务 {task} 不存在"}
        result["ready"] = False
        result["errors"].append(f"任务 {task} 不存在，可用: {sorted(tasks)}")
        if json_output:
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            typer.echo(f"✗ 任务不存在: {task}")
            typer.echo(f"  可用任务: {', '.join(sorted(tasks)[:10])}{'...' if len(tasks) > 10 else ''}")
        raise typer.Exit(code=EXIT_PARAM)

    t = tasks[task]
    result["checks"]["task_exists"] = {"ok": True, "message": f"{t.title} ({t.level})"}

    # 2. 检查任务 fixtures 是否完整
    from pathlib import Path
    task_dir = find_tasks_dir() / task
    fixtures_ok = task_dir.is_dir()
    fixture_files = []
    if fixtures_ok:
        fixture_files = [str(p.relative_to(task_dir)) for p in task_dir.rglob("*") if p.is_file()]
    result["checks"]["fixtures"] = {
        "ok": fixtures_ok,
        "message": f"{len(fixture_files)} 个文件" if fixtures_ok else "任务目录不存在",
        "files": fixture_files[:20],
    }
    if not fixtures_ok:
        result["ready"] = False
        result["errors"].append(f"任务目录不存在: {task_dir}")

    # 3. 检查后端是否可用
    try:
        backend = get_backend(agent, model=model)
        result["checks"]["backend"] = {"ok": True, "message": f"{backend.name} v{backend.version}"}
    except (ValueError, KeyError) as e:
        result["checks"]["backend"] = {"ok": False, "message": str(e)}
        result["ready"] = False
        result["errors"].append(f"后端不可用: {e}")
        if json_output:
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            typer.echo(f"✗ 后端不可用: {agent} - {e}")
        raise typer.Exit(code=EXIT_PARAM)

    # 4. 检查 API Key 连通性（调用后端的 check_api_key）
    api_check = backend.check_api_key()
    result["checks"]["api_key"] = {
        "ok": api_check.get("ok", False),
        "status": api_check.get("status", "unknown"),
        "message": api_check.get("message", ""),
        "latency_ms": api_check.get("latency_ms"),
    }
    if not api_check.get("ok", False):
        result["ready"] = False
        result["errors"].append(f"API Key: {api_check.get('status')} - {api_check.get('message')}")

    # 5. 成本预估
    est = estimate_cost(
        agent, task, level=t.level, verifier=t.verifier, runs=runs, model=model
    )
    result["estimated_cost"] = {
        "cny": round(est["cost_cny"], 4),
        "source": est["source"],
        "note": est.get("note", ""),
        "runs": runs,
    }

    # 输出
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        typer.echo(f"预检: {task} × {agent} @ {model}")
        typer.echo("-" * 50)
        for name, check in result["checks"].items():
            mark = "✓" if check["ok"] else "✗"
            typer.echo(f"  {mark} {name}: {check['message']}")
        typer.echo(f"  预计成本: ¥{est['cost_cny']:.4f}（{est['source']}，{runs} run）")
        typer.echo("-" * 50)
        if result["ready"]:
            typer.echo("✓ 就绪，可以开始评测")
        else:
            typer.echo("✗ 未就绪，请修复以上问题后重试")
            for err in result["errors"]:
                typer.echo(f"  - {err}")

    raise typer.Exit(code=EXIT_OK if result["ready"] else EXIT_API_KEY)


@app.command("commands")
def commands(
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """显示完整命令清单和用法速查。"""
    cmd_list = [
        {
            "category": "评测执行",
            "commands": [
                {"name": "run", "desc": "单任务评测", "usage": "agent-eval run --task T001 --agent claude-code", "options": "--task, --agent, --model, --runs, --json, --stdin, --quiet"},
                {"name": "preflight", "desc": "执行前预检（API Key/模型/任务/成本）", "usage": "agent-eval preflight --task T001 --agent claude-code", "options": "--task, --agent, --model, --runs, --json"},
                {"name": "ci", "desc": "CI 门禁评测（支持 gate 阻断）", "usage": "agent-eval ci --agent claude-code --gate core", "options": "--agent, --model, --gate, --runs, --output"},
            ],
        },
        {
            "category": "查询分析",
            "commands": [
                {"name": "runs-list", "desc": "历史运行列表（分页+筛选）", "usage": "agent-eval runs-list --agent claude-code --limit 20", "options": "--limit, --offset, --task, --agent, --status, --json"},
                {"name": "runs-show", "desc": "单次运行完整详情", "usage": "agent-eval runs-show <run_id>", "options": "--json"},
                {"name": "tasks-show", "desc": "任务 spec 完整详情", "usage": "agent-eval tasks-show T001", "options": "--json"},
                {"name": "list-tasks", "desc": "任务包列表（含预计成本）", "usage": "agent-eval list-tasks", "options": "--json"},
                {"name": "report", "desc": "生成评测报告", "usage": "agent-eval report --run <run_id>", "options": "--run, --output, --format"},
                {"name": "dreaming", "desc": "系统性模式分析（失败归因）", "usage": "agent-eval dreaming --agent claude-code", "options": "--agent, --task, --min-pattern, --auto-badcase"},
                {"name": "judge-calibrate", "desc": "LLM Judge 人工校准（一致率≥85%才可信）", "usage": "agent-eval judge-calibrate --labeled <labeled.json>", "options": "--labeled, --rubric, --json"},
            ],
        },
        {
            "category": "资源管理",
            "commands": [
                {"name": "badcase-list", "desc": "Badcase 列表（分页+筛选）", "usage": "agent-eval badcase-list --severity P0 --status pending", "options": "--limit, --offset, --task, --agent, --severity, --status, --json"},
                {"name": "badcase-show", "desc": "Badcase 完整详情", "usage": "agent-eval badcase-show <badcase_id>", "options": "--json"},
                {"name": "taskpack", "desc": "任务包管理（list/info/install）", "usage": "agent-eval taskpack list", "options": "子命令: list, info, install"},
            ],
        },
        {
            "category": "平台管理",
            "commands": [
                {"name": "workbench", "desc": "启动 Web 评测工作台", "usage": "agent-eval workbench", "options": "--host, --port, --reload"},
                {"name": "convert", "desc": "格式转换（OpenAPI spec → 任务）", "usage": "agent-eval convert --input spec.yaml --output tasks/", "options": "--input, --output, --format"},
            ],
        },
    ]

    exit_codes = [
        {"code": 0, "meaning": "成功 / 评测全部通过"},
        {"code": 1, "meaning": "评测未通过（有失败 checkpoint）"},
        {"code": 2, "meaning": "参数错误 / 任务不存在"},
        {"code": 3, "meaning": "API Key 缺失或无效"},
        {"code": 4, "meaning": "执行超时"},
        {"code": 5, "meaning": "内部错误"},
    ]

    if json_output:
        typer.echo(json.dumps({
            "commands": cmd_list,
            "exit_codes": exit_codes,
            "total_commands": sum(len(c["commands"]) for c in cmd_list),
        }, ensure_ascii=False, indent=2))
        return

    typer.echo("=" * 70)
    typer.echo("  agent-eval 命令清单")
    typer.echo("=" * 70)
    for cat in cmd_list:
        typer.echo(f"\n【{cat['category']}】")
        typer.echo("-" * 70)
        for cmd in cat["commands"]:
            typer.echo(f"  {cmd['name']:<14} {cmd['desc']}")
            typer.echo(f"  {'':14} 用法: {cmd['usage']}")
            typer.echo(f"  {'':14} 选项: {cmd['options']}")
            typer.echo("")

    typer.echo("【退出码】")
    typer.echo("-" * 70)
    for ec in exit_codes:
        typer.echo(f"  {ec['code']}  {ec['meaning']}")

    typer.echo("\n提示: 任何命令加 --help 查看详细参数，加 --json 输出结构化结果")
    typer.echo("=" * 70)


def _get_store():
    """获取 RunStore 实例（CLI 用）。"""
    from pathlib import Path
    from agent_eval.web.store import RunStore
    db_path = Path("results") / "run_history.db"
    return RunStore(db_path)


@app.command("runs-list")
def runs_list(
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="返回条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="偏移量"),
    task_id: Optional[str] = typer.Option(None, "--task", help="按任务 ID 筛选"),
    agent_id: Optional[str] = typer.Option(None, "--agent", help="按 Agent 筛选"),
    status: Optional[str] = typer.Option(None, "--status", help="按状态筛选"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """列出历史运行记录（分页 + 筛选）。"""
    store = _get_store()
    rows, total = store.list_runs(limit=limit, offset=offset, task_id=task_id, agent_id=agent_id, status=status)

    if json_output:
        typer.echo(json.dumps({
            "total": total,
            "limit": limit,
            "offset": offset,
            "runs": rows,
        }, ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"历史运行（共 {total} 条，显示 {offset+1}-{offset+len(rows)}）")
    typer.echo(f"{'run_id':<14}{'task':<8}{'agent':<16}{'status':<12}{'score':<7}{'dur(s)':<8}{'created_at'}")
    typer.echo("-" * 90)
    for r in rows:
        typer.echo(
            f"{r['run_id']:<14}{r['task_id']:<8}{r['agent_id']:<16}"
            f"{r['status']:<12}{r['score']:<7.2f}{r['duration_s']:<8.1f}{r['created_at']}"
        )


@app.command("runs-show")
def runs_show(
    run_id: str = typer.Argument(..., help="运行 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """查看单次运行的完整详情（从 results/runs/<run_id>/run.json 读取）。"""
    from pathlib import Path
    import re
    # 安全校验：run_id 只允许字母数字，防止路径遍历
    if not re.match(r'^[a-zA-Z0-9]+$', run_id):
        msg = f"错误：无效的 run_id 格式（只允许字母数字）"
        if json_output:
            typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
        else:
            typer.echo(msg)
        raise typer.Exit(code=EXIT_PARAM)
    runs_root = Path("results/runs").resolve()
    run_path = (runs_root / run_id / "run.json").resolve()
    # 双重校验：规范化后路径必须在 runs 目录内
    if not str(run_path).startswith(str(runs_root) + "/"):
        msg = f"错误：无效的 run_id（路径越界）"
        if json_output:
            typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
        else:
            typer.echo(msg)
        raise typer.Exit(code=EXIT_PARAM)
    if not run_path.exists():
        msg = f"错误：找不到运行 {run_id}（{run_path} 不存在）"
        if json_output:
            typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
        else:
            typer.echo(msg)
        raise typer.Exit(code=EXIT_PARAM)

    data = json.loads(run_path.read_text(encoding="utf-8"))

    if json_output:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"运行详情: {run_id}")
    typer.echo("-" * 50)
    typer.echo(f"  任务: {data.get('task_id')} ({data.get('task_level')})")
    typer.echo(f"  Agent: {data.get('agent_id')} @ {data.get('agent_ver')}")
    typer.echo(f"  状态: {data.get('status')}")
    typer.echo(f"  耗时: {data.get('duration_s', 0):.1f}s")
    typer.echo(f"  步骤: {len(data.get('steps', []))}")
    m = data.get("metrics", {})
    typer.echo(f"  得分: {m.get('score', 0):.3f} (权重 {m.get('weight', 0)})")
    usage = m.get("usage", {})
    if usage:
        typer.echo(f"  Token: {usage.get('prompt_tokens', 0)}/{usage.get('completion_tokens', 0)}")
    if m.get("cost_cny"):
        typer.echo(f"  成本: ¥{m['cost_cny']:.4f}")
    if m.get("failure_attribution"):
        fa = m["failure_attribution"]
        typer.echo(f"  失败归因: {fa.get('category')} - {fa.get('label')}")
    if m.get("hard_gate_blocked"):
        typer.echo(f"  Hard Gate: 已阻断")
    verdicts = data.get("verdicts", [])
    if verdicts:
        passed = sum(1 for v in verdicts if v.get("passed"))
        typer.echo(f"  判定: {passed}/{len(verdicts)} 通过")
        # V4.1 P1：按 category 分层展示通过率（业务结果/硬门禁/软质量）
        try:
            from agent_eval.spec import find_tasks_dir, load_task_pack
            tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
            task_spec = tasks.get(data.get("task_id"))
            cp_category = {}
            if task_spec:
                for c in getattr(task_spec, "checkpoints", []):
                    cp_category[c.id] = getattr(c, "category", "outcome")
            # 按 category 统计
            cat_stats = {}
            for v in verdicts:
                cat = cp_category.get(v.get("id", ""), "outcome")
                if cat not in cat_stats:
                    cat_stats[cat] = {"total": 0, "passed": 0}
                cat_stats[cat]["total"] += 1
                if v.get("passed"):
                    cat_stats[cat]["passed"] += 1
            cat_labels = {"outcome": "业务结果", "gate": "硬门禁", "quality": "软质量"}
            cat_summary_parts = []
            for cat in ["outcome", "gate", "quality"]:
                if cat in cat_stats:
                    s = cat_stats[cat]
                    rate = s["passed"] / s["total"] if s["total"] else 0
                    cat_summary_parts.append(f"{cat_labels.get(cat, cat)} {s['passed']}/{s['total']} ({rate:.0%})")
            if cat_summary_parts:
                typer.echo(f"  分层: {' | '.join(cat_summary_parts)}")
                # 硬门禁失败时高亮警告
                if "gate" in cat_stats and cat_stats["gate"]["passed"] < cat_stats["gate"]["total"]:
                    typer.echo(f"  ⚠ 硬门禁未通过：此任务应判失败，不被软质量高分平均")
        except Exception:  # noqa: BLE001 - 分层统计失败不影响主流程
            pass
        for v in verdicts:
            mark = "✓" if v.get("passed") else "✗"
            typer.echo(f"    {mark} [{v.get('id')}] {v.get('type')}: {v.get('detail', '')[:60]}")


@app.command("tasks-show")
def tasks_show(
    task_id: str = typer.Argument(..., help="任务 ID"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """查看任务 spec 完整详情。"""
    from agent_eval.spec import find_tasks_dir, load_task_pack
    tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
    if task_id not in tasks:
        msg = f"错误：找不到任务 {task_id}"
        if json_output:
            typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
        else:
            typer.echo(msg)
        raise typer.Exit(code=EXIT_PARAM)

    t = tasks[task_id]
    if json_output:
        from dataclasses import asdict
        typer.echo(json.dumps(asdict(t), ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"任务详情: {task_id}")
    typer.echo("-" * 50)
    typer.echo(f"  标题: {t.title}")
    typer.echo(f"  级别: {t.level}")
    typer.echo(f"  权重: {t.weight}")
    typer.echo(f"  判定: {t.verifier}")
    typer.echo(f"  描述: {t.description[:100] if t.description else '(无)'}")
    if getattr(t, "risk_level", None):
        typer.echo(f"  风险等级: {t.risk_level} ({getattr(t, 'risk_category', 'normal')})")
    if getattr(t, "scenario_type", None):
        scenario_labels = {"happy_path": "正常路径", "boundary": "边界情况", "error_recovery": "异常恢复", "adversarial": "对抗样本"}
        typer.echo(f"  场景类型: {t.scenario_type} ({scenario_labels.get(t.scenario_type, t.scenario_type)})")
    if getattr(t, "required_skills", None):
        typer.echo(f"  必需 Skill: {', '.join(t.required_skills)}")
    if getattr(t, "required_tools", None):
        typer.echo(f"  必需工具: {', '.join(t.required_tools)}")
    # 按 category 分层统计 checkpoint
    cps = getattr(t, "checkpoints", [])
    cat_counts = {}
    for c in cps:
        cat = getattr(c, "category", "outcome")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    cat_labels = {"outcome": "业务结果", "gate": "硬门禁", "quality": "软质量"}
    cat_summary = " / ".join(f"{cat_labels.get(k, k)}:{v}" for k, v in sorted(cat_counts.items()))
    typer.echo(f"  校验点 ({len(cps)} 个) [{cat_summary}]:")
    for c in cps:
        gate = getattr(c, "gate_mode", "blocking")
        cat = getattr(c, "category", "outcome")
        desc = getattr(c, "desc", "") or getattr(c, "description", "")
        typer.echo(f"    [{c.id}] {c.type} (cat={cat}, gate={gate}): {desc[:50]}")


@app.command("badcase-discover")
def badcase_discover(
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="返回候选数"),
    agent_id: Optional[str] = typer.Option(None, "--agent", help="按 Agent 筛选"),
    days: int = typer.Option(30, "--days", min=1, max=365, help="扫描最近 N 天"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """V4.1 P0：从运行历史自动挖掘 badcase 候选（回归失败/Hard Gate/完全失败/部分失败）。"""
    store = _get_store()
    candidates = store.discover_badcase_candidates(limit=limit, agent_id=agent_id, days=days)

    if json_output:
        typer.echo(json.dumps({
            "total": len(candidates),
            "by_severity": {
                "P0": sum(1 for c in candidates if c["severity"] == "P0"),
                "P1": sum(1 for c in candidates if c["severity"] == "P1"),
                "P2": sum(1 for c in candidates if c["severity"] == "P2"),
            },
            "by_type": {
                t: sum(1 for c in candidates if c["candidate_type"] == t)
                for t in ["regression", "hard_gate", "total_failure", "partial_failure"]
            },
            "candidates": candidates,
        }, ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"挖掘到 {len(candidates)} 个 badcase 候选（最近 {days} 天）")
    typer.echo("=" * 90)
    typer.echo(f"{'严重度':<6}{'类型':<16}{'run_id':<14}{'任务':<8}{'Agent':<16}{'通过率':<8}{'原因'}")
    typer.echo("-" * 90)
    for c in candidates:
        typer.echo(
            f"{c['severity']:<6}{c['candidate_type']:<16}{c['run_id']:<14}"
            f"{c['task_id']:<8}{c['agent_id']:<16}{c['pass_rate']:<8.0%}{c['reason'][:40]}"
        )
    typer.echo("=" * 90)
    typer.echo("提示: 使用 agent-eval badcase-show <run_id> 查看详情，或在 Web 端 badcase 模块一键转化")


@app.command("judge-calibrate")
def judge_calibrate(
    labeled: str = typer.Option(..., "--labeled", "-l", help="人工标注集 JSON 文件路径"),
    rubric: Optional[str] = typer.Option(None, "--rubric", "-r", help="自定义评分标准文本（覆盖默认）"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """V4.3 P1-1：LLM Judge 校准闭环——用人工标注集计算一致率。

    校准流程：50 条人工标注 → judge 跑同样 50 条 → 一致率 <80% 改 rubric 重跑 → >85% 才上岗。
    人工标注集格式：[{"id":"case-001","input":"...","output":"...","human_pass":true,"human_score":85}, ...]
    """
    from agent_eval.judge import LLMJudge, calibrate_judge

    try:
        result = calibrate_judge(labeled, judge=LLMJudge(), rubric=rubric)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"错误: {e}")
        raise typer.Exit(code=EXIT_PARAM)

    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result["passed"]:
            raise typer.Exit(code=EXIT_FAIL)
        return

    typer.echo("=" * 70)
    typer.echo("LLM Judge 校准报告")
    typer.echo("=" * 70)
    typer.echo(f"标注样本数: {result['total']}")
    typer.echo(f"一致数: {result['agree']}")
    typer.echo(f"一致率: {result['agreement_rate']:.1%}（上岗阈值 {result['threshold']:.0%}）")
    status = "✅ 通过，可以上岗" if result["passed"] else "❌ 未通过，需优化 rubric"
    typer.echo(f"校准结果: {status}")
    typer.echo("-" * 70)
    typer.echo(f"建议: {result['recommendation']}")
    if result["conflicts"]:
        typer.echo("-" * 70)
        typer.echo(f"冲突样本（{len(result['conflicts'])} 个，前 10 个）:")
        for c in result["conflicts"][:10]:
            typer.echo(
                f"  {c['id']}: 人工={'PASS' if c['human_pass'] else 'FAIL'} "
                f"vs Judge={'PASS' if c['judge_pass'] else 'FAIL'} "
                f"(score {c.get('human_score', '?')} vs {c['judge_score']})"
            )
            if c.get("judge_reasoning"):
                typer.echo(f"    Judge 理由: {c['judge_reasoning'][:80]}")
    typer.echo("=" * 70)

    if not result["passed"]:
        raise typer.Exit(code=EXIT_FAIL)


@app.command("pairwise")
def pairwise(
    task_desc: str = typer.Option(..., "--task", "-t", help="任务描述"),
    output_a: str = typer.Option(..., "--output-a", help="Agent A 输出文件路径"),
    output_b: str = typer.Option(..., "--output-b", help="Agent B 输出文件路径"),
    label_a: str = typer.Option("Agent A", "--label-a", help="Agent A 标签"),
    label_b: str = typer.Option("Agent B", "--label-b", help="Agent B 标签"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """V4.3 P2-1：A/B Pairwise 对比 + 换位测试。

    两个输出正反各评一次，取一致结果；不一致标注为存疑需人工复核。
    防止位置偏差（先出现的输出容易被偏好）。
    """
    from pathlib import Path
    from agent_eval.judge import LLMJudge, pairwise_compare

    try:
        text_a = Path(output_a).read_text(encoding="utf-8")
        text_b = Path(output_b).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        typer.echo(f"错误: 读取输出文件失败: {e}")
        raise typer.Exit(code=EXIT_PARAM)

    result = pairwise_compare(task_desc, text_a, text_b, label_a, label_b, judge=LLMJudge())

    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get("winner") == "inconclusive":
            raise typer.Exit(code=EXIT_FAIL)
        return

    typer.echo("=" * 60)
    typer.echo("A/B Pairwise 对比 + 换位测试")
    typer.echo("=" * 60)
    typer.echo(f"任务: {task_desc[:60]}")
    typer.echo(f"Agent A: {label_a} (均分 {result.get('avg_score_a', 0)})")
    typer.echo(f"Agent B: {label_b} (均分 {result.get('avg_score_b', 0)})")
    typer.echo("-" * 60)
    typer.echo(f"第一轮 (A在前): 胜者={result['round1']['winner']}  理由: {result['round1']['reasoning'][:60]}")
    typer.echo(f"第二轮 (B在前): 胜者={result['round2']['winner']}  理由: {result['round2']['reasoning'][:60]}")
    typer.echo("-" * 60)
    winner_map = {"A": label_a, "B": label_b, "tie": "平局", "inconclusive": "存疑（需人工复核）"}
    typer.echo(f"最终结果: {winner_map.get(result['winner'], result['winner'])}")
    typer.echo(f"两次一致: {'是' if result.get('agreement') else '否'}")
    if result.get("position_bias_risk"):
        typer.echo("⚠ 位置偏差风险：两次结果相反，建议人工复核或增加第三轮")
    typer.echo("=" * 60)

    if result.get("winner") == "inconclusive":
        raise typer.Exit(code=EXIT_FAIL)


@app.command("badcase-list")
def badcase_list(
    limit: int = typer.Option(20, "--limit", min=1, max=500, help="返回条数"),
    offset: int = typer.Option(0, "--offset", min=0, help="偏移量"),
    task_id: Optional[str] = typer.Option(None, "--task", help="按任务 ID 筛选"),
    agent_id: Optional[str] = typer.Option(None, "--agent", help="按 Agent 筛选"),
    severity: Optional[str] = typer.Option(None, "--severity", help="按严重度筛选 (P0/P1/P2)"),
    status: Optional[str] = typer.Option(None, "--status", help="按状态筛选"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """列出 badcase 记录（分页 + 筛选）。"""
    store = _get_store()
    rows, total = store.list_badcases(limit=limit, offset=offset, task_id=task_id, agent_id=agent_id, severity=severity, status=status)

    if json_output:
        typer.echo(json.dumps({
            "total": total,
            "limit": limit,
            "offset": offset,
            "badcases": rows,
        }, ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"Badcase（共 {total} 条，显示 {offset+1}-{offset+len(rows)}）")
    typer.echo(f"{'id':<10}{'task':<8}{'agent':<14}{'severity':<9}{'status':<10}{'category':<16}title")
    typer.echo("-" * 90)
    for b in rows:
        typer.echo(
            f"{b['id']:<10}{b['task_id']:<8}{b['agent_id']:<14}"
            f"{b['severity']:<9}{b['status']:<10}{b['category']:<16}{b['title'][:30]}"
        )


@app.command("badcase-show")
def badcase_show(
    badcase_id: str = typer.Argument(..., help="Badcase ID"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """查看单个 badcase 完整详情。"""
    store = _get_store()
    b = store.get_badcase(badcase_id)
    if not b:
        msg = f"错误：找不到 badcase {badcase_id}"
        if json_output:
            typer.echo(json.dumps({"error": msg, "exit_code": EXIT_PARAM}, ensure_ascii=False))
        else:
            typer.echo(msg)
        raise typer.Exit(code=EXIT_PARAM)

    if json_output:
        typer.echo(json.dumps(b, ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"Badcase 详情: {badcase_id}")
    typer.echo("-" * 50)
    typer.echo(f"  标题: {b['title']}")
    typer.echo(f"  任务: {b['task_id']}")
    typer.echo(f"  Agent: {b['agent_id']}")
    typer.echo(f"  严重度: {b['severity']}")
    typer.echo(f"  状态: {b['status']}")
    typer.echo(f"  分类: {b['category']}")
    typer.echo(f"  描述: {b['description'][:200]}")
    if b.get("root_cause"):
        typer.echo(f"  根因: {b['root_cause'][:200]}")
    if b.get("fix_plan"):
        typer.echo(f"  修复方案: {b['fix_plan'][:200]}")
    if b.get("regression_task_id"):
        typer.echo(f"  回归用例: {b['regression_task_id']}")
    if b.get("tags"):
        typer.echo(f"  标签: {', '.join(b['tags'])}")
    typer.echo(f"  创建: {b['created_at']}")


@app.command("ci")
def ci(
    gate: str = typer.Option("core", "--gate", help="门禁名称（core / full，见 ci/gate.yaml）"),
    agent: str = typer.Option("minimal-react", "--agent", help="被测后端 Agent 名称"),
    model: str = typer.Option("deepseek-chat", "--model", help="LLM 模型名"),
    runs: Optional[int] = typer.Option(None, "--runs", min=1, max=20, help="覆盖 gate 配置的采样次数"),
    config: str = typer.Option("ci/gate.yaml", "--config", help="门禁配置文件路径"),
    junit_xml: str = typer.Option("results/junit.xml", "--junit-xml", help="JUnit XML 输出路径"),
    allure_dir: str = typer.Option("results/allure-results", "--allure-dir", help="Allure results 输出目录"),
    report_json: str = typer.Option("results/ci-report.json", "--report-json", help="门禁汇总 JSON 输出路径"),
    results_dir: str = typer.Option("results/runs", "--results-dir", help="run.json 落盘目录"),
) -> None:
    """无头运行 CI 质量门禁（M1）。

    按 ci/gate.yaml 的 gate 配置执行评测，输出 JUnit XML + Allure results，
    门禁未通过时退出码非 0（配合 GitHub Actions required check 阻断合并）。
    """
    from agent_eval.ci import run_gate_cli

    try:
        ok = run_gate_cli(
            gate, agent=agent, model=model, runs_override=runs,
            config_path=config, junit_xml=junit_xml, allure_dir=allure_dir,
            report_json=report_json, results_dir=results_dir,
        )
    except (FileNotFoundError, ValueError) as e:
        typer.echo(f"错误: {e}")
        raise typer.Exit(code=EXIT_PARAM)
    if not ok:
        typer.echo(f"\ngate={gate} 未通过，退出码 1（CI 将阻断合并）")
        raise typer.Exit(code=EXIT_FAIL)


@app.command("report")
def report(
    out: str = typer.Option("reports/report.html", "--out", "-o", help="输出 HTML 报告路径"),
) -> None:
    """聚合 results/runs 下全部 run，生成自包含 HTML 评测报告。"""
    from agent_eval.reporter import generate_report

    path = generate_report(out)
    typer.echo(f"报告已生成: {path}")


@app.command("convert")
def convert(
    source: str = typer.Option(..., "--source", "-s", help="源数据文件路径（SWE-bench/GAIA JSON/JSONL）"),
    converter: str = typer.Option("swe-bench", "--converter", "-c", help="转换器名称（swe-bench/gaia）"),
    output: str = typer.Option("tasks", "--output", "-o", help="输出目录（在此创建 tasks/<id>/spec.yaml）"),
    limit: int = typer.Option(0, "--limit", "-n", help="转换数量上限，0 表示全部"),
    start_index: int = typer.Option(0, "--start-index", help="起始任务序号（用于分批转换）"),
    id_prefix: str = typer.Option("", "--id-prefix", help="任务 ID 前缀（默认按转换器自动设置：swe-bench=SW, gaia=GA）"),
) -> None:
    """将其他平台评测用例转换为 ai-agent-eval spec.yaml 格式。

    示例：
      agent-eval convert -s swe-bench-lite.json -c swe-bench -o tasks -n 10
      agent-eval convert --source gaia.json --converter gaia --output tasks --limit 5
    """
    from pathlib import Path

    from agent_eval.converters import CONVERTERS, get_converter

    # 转换器默认 ID 前缀映射
    DEFAULT_PREFIXES = {
        "swe-bench": "SW",
        "gaia": "GA",
    }
    if not id_prefix:
        id_prefix = DEFAULT_PREFIXES.get(converter, "T")

    source_path = Path(source)
    if not source_path.exists():
        typer.echo(f"错误：源文件不存在: {source_path}")
        raise typer.Exit(code=1)

    if converter not in CONVERTERS:
        typer.echo(f"错误：未知转换器 '{converter}'，可用: {sorted(CONVERTERS)}")
        raise typer.Exit(code=1)

    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)

    typer.echo(f"转换器: {converter}")
    typer.echo(f"源文件: {source_path}")
    typer.echo(f"输出目录: {output_dir}")
    typer.echo("-" * 50)

    conv = get_converter(converter)
    tasks = conv.convert(
        source_path, output_dir,
        limit=limit,
        start_index=start_index,
        id_prefix=id_prefix,
    )

    typer.echo("-" * 50)
    typer.echo(f"转换完成: {len(tasks)} 个任务")
    for t in tasks:
        typer.echo(f"  {t.id}  [{t.level}]  {t.title[:50]}")
    typer.echo(f"\n提示: 记得将新任务 ID 添加到 {output_dir}/manifest.yaml 的 tasks 列表中")


# 任务包市场命令组
taskpack_app = typer.Typer(help="任务包市场：安装/列出/卸载评测任务包")
app.add_typer(taskpack_app, name="taskpack")


@taskpack_app.command("install")
def taskpack_install(
    source: str = typer.Argument(..., help="任务包来源（git URL 或本地目录路径）"),
    name: str = typer.Option("", "--name", "-n", help="覆盖包名（默认从 package.yaml 读取）"),
    packages_dir: str = typer.Option("", "--packages-dir", help="任务包安装目录（默认 ~/.agent-eval/packages）"),
) -> None:
    """安装任务包（从 git 仓库或本地目录）。"""
    from pathlib import Path

    from agent_eval.taskpack import install_package

    try:
        pkg = install_package(
            source,
            packages_dir=Path(packages_dir) if packages_dir else None,
            name=name if name else None,
        )
        typer.echo(f"安装成功: {pkg.name} v{pkg.version}")
        typer.echo(f"  作者: {pkg.author or '未知'}")
        typer.echo(f"  描述: {pkg.description or '无'}")
        typer.echo(f"  任务数: {len(pkg.tasks)}")
        typer.echo(f"  安装路径: {pkg.install_path}")
        typer.echo(f"\n提示: 在 tasks/manifest.yaml 中添加 includes: [{pkg.name}] 以启用此任务包")
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        typer.echo(f"错误: {e}")
        raise typer.Exit(code=1)


@taskpack_app.command("list")
def taskpack_list(
    packages_dir: str = typer.Option("", "--packages-dir", help="任务包安装目录"),
) -> None:
    """列出已安装的所有任务包。"""
    from pathlib import Path

    from agent_eval.taskpack import list_packages

    packages = list_packages(Path(packages_dir) if packages_dir else None)
    if not packages:
        typer.echo("未安装任何任务包")
        typer.echo("使用 agent-eval taskpack install <git-url或路径> 安装")
        return
    typer.echo(f"已安装 {len(packages)} 个任务包:")
    typer.echo("-" * 70)
    for pkg in packages:
        typer.echo(f"  {pkg.name:<20} v{pkg.version:<10} {len(pkg.tasks):>3} 任务  {pkg.description[:30]}")


@taskpack_app.command("remove")
def taskpack_remove(
    name: str = typer.Argument(..., help="要卸载的任务包名称"),
    packages_dir: str = typer.Option("", "--packages-dir", help="任务包安装目录"),
) -> None:
    """卸载任务包。"""
    from pathlib import Path

    from agent_eval.taskpack import remove_package

    if remove_package(name, Path(packages_dir) if packages_dir else None):
        typer.echo(f"已卸载: {name}")
    else:
        typer.echo(f"任务包不存在: {name}")
        raise typer.Exit(code=1)


@taskpack_app.command("info")
def taskpack_info(
    name: str = typer.Argument(..., help="任务包名称"),
    packages_dir: str = typer.Option("", "--packages-dir", help="任务包安装目录"),
) -> None:
    """查看任务包详细信息。"""
    from pathlib import Path

    from agent_eval.taskpack import get_package

    pkg = get_package(name, Path(packages_dir) if packages_dir else None)
    if pkg is None:
        typer.echo(f"任务包不存在: {name}")
        raise typer.Exit(code=1)
    typer.echo(f"名称: {pkg.name}")
    typer.echo(f"版本: {pkg.version}")
    typer.echo(f"作者: {pkg.author or '未知'}")
    typer.echo(f"协议: {pkg.license or '未知'}")
    typer.echo(f"描述: {pkg.description or '无'}")
    typer.echo(f"安装路径: {pkg.install_path}")
    typer.echo(f"任务数: {len(pkg.tasks)}")
    if pkg.tasks:
        typer.echo("任务列表:")
        for t in pkg.tasks:
            typer.echo(f"  - {t}")


@app.command("workbench")
def workbench(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(8000, "--port", help="监听端口"),
) -> None:
    """启动本地 Web 评测工作台（V2.1+），浏览器访问 http://<host>:<port>。"""
    import uvicorn

    from agent_eval.web.app import create_app

    typer.echo(f"Web 评测工作台启动中: http://{host}:{port} （Ctrl+C 停止）")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


@app.command("dreaming")
def dreaming(
    days: int = typer.Option(7, "--days", "-d", help="分析最近 N 天的运行记录"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="输出报告文件路径（默认输出到控制台）"),
    min_pattern: int = typer.Option(2, "--min-pattern", help="模式最小出现次数（低于此数不视为系统性模式）"),
    auto_badcase: bool = typer.Option(False, "--auto-badcase", help="自动将发现的系统性模式转化为 badcase"),
) -> None:
    """V2.9：Dreaming 异步进化分析——定期审阅历史运行记录，发现系统性失败模式、低效路径和知识缺口。

    借鉴 Anthropic Dreaming 机制：在空闲时运行，输出结构化的改进建议报告。
    与同步评测互补：同步评测解决"单次任务做得好不好"，Dreaming 解决"跨任务的系统性模式"。

    使用 --auto-badcase 可将发现的模式自动转化为 badcase，进入自进化飞轮回流。
    """
    from pathlib import Path

    from agent_eval.dreaming import analyze_runs, convert_patterns_to_badcases
    from agent_eval.runner import default_results_dir

    results_dir = default_results_dir()
    typer.echo(f"Dreaming 分析中... 分析最近 {days} 天的运行记录 (目录: {results_dir})")

    report = analyze_runs(results_dir, days=days, min_pattern_count=min_pattern)

    typer.echo("")
    typer.echo("=" * 60)
    typer.echo(f"  Dreaming 进化分析报告")
    typer.echo("=" * 60)
    typer.echo(f"  生成时间: {report.generated_at}")
    typer.echo(f"  分析运行数: {report.total_runs}")
    typer.echo(f"  时间范围: {report.time_range}")
    typer.echo("=" * 60)
    typer.echo("")

    if report.failure_patterns:
        typer.echo(f"【系统性失败模式】共 {len(report.failure_patterns)} 个")
        for i, p in enumerate(report.failure_patterns[:5], 1):
            typer.echo(f"  {i}. [{p.severity}] {p.pattern_type}: {p.pattern_value} (出现 {p.count} 次)")
            typer.echo(f"     影响任务: {', '.join(p.affected_tasks[:3])}")
            typer.echo(f"     建议: {p.suggestion}")
        typer.echo("")

    if report.inefficiency_patterns:
        typer.echo(f"【低效执行模式】共 {len(report.inefficiency_patterns)} 个（显示前5个）")
        for p in report.inefficiency_patterns[:5]:
            typer.echo(f"  - {p.task_id} × {p.agent_id}: {p.description}")
        typer.echo("")

    if report.knowledge_gaps:
        typer.echo(f"【知识缺口】共 {len(report.knowledge_gaps)} 个")
        for gap in report.knowledge_gaps[:5]:
            typer.echo(f"  - {gap}")
        typer.echo("")

    if report.top_failing_tasks:
        typer.echo("【失败率最高的任务】（显示前5个）")
        for task_id, fail_count, fail_rate in report.top_failing_tasks[:5]:
            typer.echo(f"  - {task_id}: 失败 {fail_count} 次, 失败率 {fail_rate:.1%}")
        typer.echo("")

    typer.echo("【综合改进建议】")
    for i, s in enumerate(report.suggestions, 1):
        typer.echo(f"  {i}. {s}")
    typer.echo("")

    # 输出到文件
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report.to_markdown(), encoding="utf-8")
        typer.echo(f"完整报告已写入: {output_path}")
    else:
        typer.echo("提示: 使用 --output <文件路径> 可将完整报告写入 Markdown 文件")

    # V2.9.1：自动将发现的模式转化为 badcase（飞轮回流闭环）
    if auto_badcase:
        typer.echo("")
        typer.echo("=" * 60)
        typer.echo("  自动转化为 badcase（飞轮回流）")
        typer.echo("=" * 60)
        try:
            from agent_eval.web.store import RunStore
            store = RunStore(results_dir.parent / "run_history.db")
            convert_results = convert_patterns_to_badcases(report, store, min_count=min_pattern)
            created_count = sum(1 for r in convert_results if r.get("created"))
            skipped_count = len(convert_results) - created_count
            typer.echo(f"  转化完成: 新建 {created_count} 个 badcase，跳过 {skipped_count} 个（已存在）")
            for r in convert_results:
                status = "新建" if r.get("created") else "跳过"
                typer.echo(f"  [{status}] {r['pattern']} → badcase_id={r['badcase_id']}")
        except Exception as e:
            typer.echo(f"  转化失败: {e}")
        typer.echo("=" * 60)


if __name__ == "__main__":
    app()
