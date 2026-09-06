"""评测执行编排（Day 2-3）。

流程：加载任务 spec → 干净工作目录（复制 fixtures）→ 调用后端 → 记录轨迹 →
执行判定与评分（verdicts + metrics）→ 落盘 run.json（results/runs/<run_id>/）。

V2.6：统一日志体系——run_one 包裹 run_logger，每次运行同时写 results/runs/<id>/run.log。
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_eval.backends import get_backend
from agent_eval.judge import judge_llm
from agent_eval.log import get_logger, run_logger
from agent_eval.scoring import score_task
from agent_eval.spec import TaskSpec
from agent_eval.traces import tool_category
from agent_eval.verifiers import run_checkpoints

logger = get_logger(__name__)


@dataclass
class RunRecord:
    run_id: str
    agent_id: str
    agent_ver: str
    task_id: str
    task_level: str
    status: str
    steps: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)
    duration_s: float = 0.0
    metrics: dict = field(default_factory=dict)
    verdicts: list[dict] = field(default_factory=list)
    error: str = ""
    workspace: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def default_results_dir() -> Path:
    return Path("results") / "runs"


def run_one(
    task: TaskSpec,
    agent_id: str,
    *,
    config: dict | None = None,
    results_dir: Path | None = None,
    keep_workspace: bool = True,
    run_id: str | None = None,
) -> RunRecord:
    """对单个任务执行一次评测，返回并落盘 RunRecord。

    run_id：可选。不传时自动生成（CLI 默认行为）；Web 工作台传入预生成的
    run_id，保证 API 返回的 run_id 与实际落盘目录一致。

    运行级日志：整个执行体包裹在 run_logger 中，日志同时写入
    results/runs/<run_id>/run.log，便于在运行详情页查看单次运行的完整日志。
    """
    config = config or {}
    run_id = run_id or uuid.uuid4().hex[:12]
    results_dir = results_dir or default_results_dir()
    run_dir = results_dir / run_id

    # 运行级日志：with 块内所有 logger 输出同时写入 run_dir/run.log
    with run_logger(run_id, run_dir):
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        logger.info(
            "run 开始 | run_id=%s task=%s(%s) agent=%s timeout=%ss max_steps=%s",
            run_id, task.id, task.level, agent_id,
            config.get("agent", {}).get("timeout_s", task.timeout_s),
            config.get("agent", {}).get("max_steps") or task.max_steps,
        )

        _copy_fixtures(task, workspace)
        logger.debug("fixtures 已复制到 workspace=%s", workspace)

        # 后端默认超时取任务 spec 的 timeout_s，可被 config 覆盖；max_steps 同理
        agent_kwargs = dict(config.get("agent", {}))
        agent_kwargs.setdefault("timeout_s", task.timeout_s)
        if task.max_steps:
            agent_kwargs.setdefault("max_steps", task.max_steps)
        backend = get_backend(agent_id, **agent_kwargs)
        logger.debug("后端已初始化: %s v%s", getattr(backend, "name", agent_id), getattr(backend, "version", "dev"))
        start = time.time()
        result = backend.run(task, workspace)
        duration = round(time.time() - start, 3)
        logger.info(
            "后端执行完成 | status=%s steps=%d duration=%.1fs traces=%d",
            result.status, len(result.steps), duration, len(result.traces),
        )
        if result.status == "error":
            logger.warning("后端执行报错: %s", result.error or "(无错误信息)")
        if result.status == "timeout":
            logger.warning("后端执行超时 (%ss)", agent_kwargs.get("timeout_s"))

        # Day 3：执行判定与评分（仅当后端未发生 error 时）
        # V2.2：verifier=llm_judge 的任务在确定性校验点之外，追加一次 LLM 语义判分
        verdicts: list[dict] = []
        metrics: dict = {}
        if result.status != "error":
            verdicts = run_checkpoints(task, workspace)
            passed = sum(1 for v in verdicts if v.get("passed"))
            logger.info("确定性校验点完成: %d/%d 通过", passed, len(verdicts))
            if task.verifier == "llm_judge":
                verdicts.append(judge_llm(task, workspace))
            metrics = score_task(task, verdicts)
            logger.info("评分完成: score=%.3f (权重=%.1f)", metrics.get("score", 0), task.weight)

        # V2.3：LLM token 用量汇总（CI 成本核算；黑盒后端/无 key 时为 0）
        usage: dict = {"prompt_tokens": 0, "completion_tokens": 0}
        if result.usage:
            usage["prompt_tokens"] += result.usage.get("prompt_tokens", 0) or 0
            usage["completion_tokens"] += result.usage.get("completion_tokens", 0) or 0
        for v in verdicts:
            u = v.get("usage") if isinstance(v, dict) else None
            if u:
                usage["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
                usage["completion_tokens"] += u.get("completion_tokens", 0) or 0
        metrics["usage"] = usage

        # V2.4：全链路回放轨迹（输入意图 → 检索/工具 → 模型生成）
        # 后端自报 traces（如 minimal-react 的 llm/tool 节点）优先；否则由 steps 兜底合成
        traces: list[dict] = []
        if result.traces:
            traces = list(result.traces)
        else:
            for s in result.steps:
                traces.append(
                    {
                        "kind": "tool",
                        "category": tool_category(s.get("action")),
                        "ts": s.get("ts", 0.0),
                        "tool": s.get("action"),
                        "args": s.get("args"),
                        "observation": s.get("observation"),
                    }
                )
        traces.insert(
            0, {"kind": "intent", "ts": 0.0, "content": task.description, "task_id": task.id}
        )
        traces.sort(key=lambda t: t.get("ts", 0.0))

        record = RunRecord(
            run_id=run_id,
            agent_id=agent_id,
            agent_ver=getattr(backend, "version", "dev"),
            task_id=task.id,
            task_level=task.level,
            status=result.status,
            steps=result.steps,
            traces=traces,
            duration_s=duration,
            metrics=metrics,
            verdicts=verdicts,
            error=result.error or "",
            workspace=_rel_or_abs(workspace) if keep_workspace else "",
        )

        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "run 完成 | run_id=%s status=%s score=%.3f duration=%.1fs tokens=%d/%d | %s",
            run_id, record.status, metrics.get("score", 0), duration,
            usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
            run_dir,
        )
        return record


def _copy_fixtures(task: TaskSpec, workspace: Path) -> None:
    base = task.spec_path.parent
    src = (base / task.fixtures.get("source", "fixtures")).resolve()
    if src.exists():
        shutil.copytree(src, workspace, dirs_exist_ok=True)


def _rel_or_abs(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
