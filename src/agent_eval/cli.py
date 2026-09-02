"""agent-eval 命令行入口（MVP 占位实现）。

当前只提供命令骨架：list-tasks / run。后续在 Day 1-2 计划中落地真实逻辑。
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
    agent: str = typer.Option("minimal-agent", "--agent", help="后端 Agent 名称"),
) -> None:
    """对单个任务执行一次评测（MVP）。"""
    # TODO(Day 2-3): 加载任务 spec -> 干净工作目录 -> 调用后端 -> 判定 -> 评分 -> 落盘
    typer.echo(f"TODO: 执行评测尚未实现（Day 2-3）— task={task}, agent={agent}")


if __name__ == "__main__":
    app()
