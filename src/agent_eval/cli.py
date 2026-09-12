"""agent-eval 命令行入口。

命令：list-tasks（已实现）/ run（MVP 执行，判定 Day 3 接入）。
"""

from __future__ import annotations

from typing import Optional

import typer

from agent_eval.log import setup_logging

app = typer.Typer(help="通用 AI Agent 评测框架")

# CLI 入口统一初始化日志（控制台 + results/logs/ 文件）；幂等，重复调用安全
setup_logging()


@app.command("list-tasks")
def list_tasks() -> None:
    """列出任务包中的所有任务（读取 manifest + 各 spec.yaml）。"""
    from agent_eval.spec import find_tasks_dir, load_task_pack
    from agent_eval.costing import estimate_cost, load_benchmark

    tasks_dir = find_tasks_dir()
    try:
        tasks = load_task_pack(tasks_dir)
    except FileNotFoundError as exc:
        typer.echo(f"错误：{exc}")
        raise typer.Exit(code=1)

    bench = load_benchmark()
    default_agent = "minimal-react"
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
    task: str = typer.Option(..., "--task", help="任务 ID，如 T001"),
    agent: str = typer.Option("minimal-react", "--agent", help="后端 Agent 名称"),
    model: str = typer.Option("deepseek-chat", "--model", help="LLM 模型名"),
    timeout: Optional[int] = typer.Option(None, "--timeout", help="覆盖任务默认超时（秒）"),
    runs: int = typer.Option(1, "--runs", min=1, max=20, help="运行次数（采样，对抗非确定性）"),
) -> None:
    """对单个任务执行评测；--runs N 时输出采样统计（best/mean/std/pass_rate）。"""
    from agent_eval.runner import run_one
    from agent_eval.spec import find_tasks_dir, load_task_pack
    from agent_eval.stats import summarize_scores
    from agent_eval.costing import estimate_cost

    tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
    if task not in tasks:
        typer.echo(f"错误：找不到任务 {task}，可用: {sorted(tasks)}")
        raise typer.Exit(code=1)

    # 执行前提示预计 LLM 成本（实测基准 / 分级估算）
    est = estimate_cost(
        agent, task, level=tasks[task].level, verifier=tasks[task].verifier, runs=runs
    )
    src = "实测基准" if est["source"] == "measured" else "分级估算"
    typer.echo(
        f"预计成本: ¥{est['cost_cny']:.4f}（{agent} × {runs} run · {src} · {est['note']}）"
    )

    config: dict = {"agent": {"model": model}}
    if timeout is not None:
        config["agent"]["timeout_s"] = timeout

    if runs <= 1:
        _print_record(run_one(tasks[task], agent, config=config))
        return

    records = []
    for i in range(1, runs + 1):
        rec = run_one(tasks[task], agent, config=config)
        records.append(rec)
        score = rec.metrics.get("score", 0.0)
        typer.echo(f"run {i}/{runs}: {rec.run_id}  {rec.status}  {rec.duration_s}s  score={score}")
    stats = summarize_scores([r.metrics.get("score", 0.0) for r in records])
    typer.echo(
        f"统计 (N={stats['n']}): best={stats['best']} mean={stats['mean']} std={stats['std']} pass_rate={stats['pass_rate']}"
    )


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
        raise typer.Exit(code=2)
    if not ok:
        typer.echo(f"\ngate={gate} 未通过，退出码 1（CI 将阻断合并）")
        raise typer.Exit(code=1)


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


if __name__ == "__main__":
    app()
