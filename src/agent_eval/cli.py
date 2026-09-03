"""agent-eval 命令行入口。

命令：list-tasks（已实现）/ run（MVP 执行，判定 Day 3 接入）。
"""

from __future__ import annotations

import typer

app = typer.Typer(help="通用 AI Agent 评测框架")


@app.command("list-tasks")
def list_tasks() -> None:
    """列出任务包中的所有任务（读取 manifest + 各 spec.yaml）。"""
    from agent_eval.spec import find_tasks_dir, load_task_pack

    tasks_dir = find_tasks_dir()
    try:
        tasks = load_task_pack(tasks_dir)
    except FileNotFoundError as exc:
        typer.echo(f"错误：{exc}")
        raise typer.Exit(code=1)

    typer.echo(f"任务包: {tasks_dir}")
    typer.echo(f"{'ID':<6}{'级别':<5}{'权重':<7}{'判定':<13}标题")
    typer.echo("-" * 64)
    for t in sorted(tasks, key=lambda x: x.id):
        typer.echo(
            f"{t.id:<6}{t.level:<5}{t.weight:<7.1f}{t.verifier:<13}{t.title}"
        )
    typer.echo(f"\n共 {len(tasks)} 个任务")


@app.command("run")
def run(
    task: str = typer.Option(..., "--task", help="任务 ID，如 T001"),
    agent: str = typer.Option("minimal-react", "--agent", help="后端 Agent 名称"),
    model: str = typer.Option("deepseek-chat", "--model", help="LLM 模型名（minimal-react 用）"),
) -> None:
    """对单个任务执行一次评测（MVP）。"""
    from agent_eval.runner import run_one
    from agent_eval.spec import find_tasks_dir, load_task_pack

    tasks = {t.id: t for t in load_task_pack(find_tasks_dir())}
    if task not in tasks:
        typer.echo(f"错误：找不到任务 {task}，可用: {sorted(tasks)}")
        raise typer.Exit(code=1)

    config = {"agent": {"model": model}}
    record = run_one(tasks[task], agent, config=config)

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


@app.command("report")
def report(
    out: str = typer.Option("reports/report.html", "--out", "-o", help="输出 HTML 报告路径"),
) -> None:
    """聚合 results/runs 下全部 run，生成自包含 HTML 评测报告。"""
    from agent_eval.reporter import generate_report

    path = generate_report(out)
    typer.echo(f"报告已生成: {path}")


if __name__ == "__main__":
    app()
