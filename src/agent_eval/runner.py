"""评测执行编排（Day 2-3）。

流程：加载任务 spec → 干净工作目录（复制 fixtures）→ 调用后端 → 记录轨迹 →
执行判定与评分（verdicts + metrics）→ 落盘 run.json（results/runs/<run_id>/）。

V2.6：统一日志体系——run_one 包裹 run_logger，每次运行同时写 results/runs/<id>/run.log。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_eval.backends import get_backend
from agent_eval.circuit_breaker import CircuitBreaker, CircuitConfig, DailySampler
from agent_eval.confidence import calculate_run_confidence
from agent_eval.data_flywheel import analyze_run_failure, should_auto_create_badcase
from agent_eval.judge import judge_llm
from agent_eval.log import get_logger, run_logger
from agent_eval.mcp_env import MCPEnvironment
from agent_eval.scoring import score_task
from agent_eval.spec import TaskSpec
from agent_eval.traces import tool_category
from agent_eval.verifiers import run_checkpoints

logger = get_logger(__name__)

# V3.3 P3：全局熔断器（单例，跨 run 共享统计）
_circuit_breaker: CircuitBreaker | None = None
_daily_sampler: DailySampler | None = None


def get_circuit_breaker(results_dir: Path | None = None) -> CircuitBreaker:
    """获取全局熔断器单例。"""
    global _circuit_breaker
    if _circuit_breaker is None:
        stats_path = None
        if results_dir:
            stats_path = results_dir.parent / "circuit_breaker.json"
        _circuit_breaker = CircuitBreaker(
            config=CircuitConfig(),
            stats_path=stats_path,
        )
    return _circuit_breaker


def get_daily_sampler(results_dir: Path | None = None) -> DailySampler:
    """获取全局每日采样器单例。"""
    global _daily_sampler
    if _daily_sampler is None:
        storage_path = None
        if results_dir:
            storage_path = results_dir.parent / "daily_samples.json"
        _daily_sampler = DailySampler(sample_count=100, storage_path=storage_path)
    return _daily_sampler


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


def _recall_and_inject_memories(task: TaskSpec, config: dict) -> tuple[TaskSpec, list[str]]:
    """V2.9：根据任务特征召回经验记忆，注入到 system_prompt。

    返回 (修改后的 task, 使用的记忆 ID 列表)。如果记忆注入未启用或无匹配记忆，
    返回原始 task 和空列表。
    """
    mem_cfg = config.get("memory", {}) if config else {}
    if not mem_cfg.get("enabled", False):
        return task, []

    try:
        from agent_eval.web.store import RunStore
        store = RunStore(Path("results") / "run_history.db")
    except Exception as e:  # noqa: BLE001
        logger.debug("记忆系统初始化失败，跳过记忆注入: %s", e)
        return task, []

    # 构建召回关键词：任务 ID + 标签 + 标题关键词
    keywords = [task.id]
    if task.tags:
        keywords.extend(task.tags)
    # 从标题提取简单关键词（前几个词）
    if task.title:
        title_words = [w for w in task.title.split() if len(w) > 1][:3]
        keywords.extend(title_words)

    limit = int(mem_cfg.get("limit", 3))
    try:
        memories = store.recall_memories(task_tags=task.tags or [], keywords=keywords, limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.debug("记忆召回失败: %s", e)
        return task, []

    if not memories:
        logger.debug("未召回相关记忆")
        return task, []

    # 格式化记忆内容
    mem_blocks = []
    mem_ids = []
    for m in memories:
        mem_ids.append(m["id"])
        block = f"【经验记忆 · {m['title']}】\n{m['content']}"
        if m.get("source_badcase_id"):
            block += f"\n（来源: badcase {m['source_badcase_id']}）"
        mem_blocks.append(block)

    memory_text = "\n\n---\n以下是从历史经验中召回的相关记忆，请在执行任务时参考：\n" + "\n\n".join(mem_blocks) + "\n---\n"

    # 创建 task 副本，修改 system_prompt
    import copy
    new_task = copy.deepcopy(task)
    new_task.system_prompt = (task.system_prompt or "") + memory_text

    logger.info("记忆注入: 召回 %d 条记忆 (IDs: %s)", len(memories), ", ".join(mem_ids))
    return new_task, mem_ids


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

        # M2：MCP 工具环境——如果任务声明了 mcp_servers，生成配置文件并注入环境变量
        mcp_env = MCPEnvironment(task.mcp_servers, workspace)
        mcp_env_vars = mcp_env.prepare()
        mcp_health: list[tuple[str, bool, str]] = []
        if mcp_env_vars:
            logger.info("MCP 环境已准备: %d 个 server, 配置文件=%s", len(task.mcp_servers), mcp_env_vars.get("MCP_CONFIG_FILE"))
            # M2 扩展：MCP server 健康检查——执行前验证 server 能否正常启动
            mcp_health = mcp_env.health_check(timeout=8.0)
            healthy = sum(1 for _, ok, _ in mcp_health if ok)
            unhealthy = [(name, detail) for name, ok, detail in mcp_health if not ok]
            if unhealthy:
                logger.warning(
                    "MCP 健康检查: %d/%d 个 server 不健康: %s",
                    len(unhealthy), len(mcp_health),
                    "; ".join(f"{name}: {detail}" for name, detail in unhealthy),
                )
            else:
                logger.info("MCP 健康检查: 全部 %d 个 server 正常", len(mcp_health))
            # 临时注入到当前进程环境，backend 启动的子进程会继承
            _saved_env = {k: os.environ.get(k) for k in mcp_env_vars}
            os.environ.update(mcp_env_vars)
        else:
            _saved_env = {}

        # 后端默认超时取任务 spec 的 timeout_s，可被 config 覆盖；max_steps 同理
        agent_kwargs = dict(config.get("agent", {}))
        agent_kwargs.setdefault("timeout_s", task.timeout_s)
        if task.max_steps:
            agent_kwargs.setdefault("max_steps", task.max_steps)
        backend = get_backend(agent_id, **agent_kwargs)
        logger.debug("后端已初始化: %s v%s", getattr(backend, "name", agent_id), getattr(backend, "version", "dev"))

        # V2.9：经验记忆注入——根据任务特征召回相关记忆，注入到 system_prompt
        task_for_run, used_memory_ids = _recall_and_inject_memories(task, config)

        try:
            start = time.time()
            result = backend.run(task_for_run, workspace)
            duration = round(time.time() - start, 3)
        finally:
            # M2：MCP 环境清理——恢复环境变量，删除配置文件
            if _saved_env:
                for k, v in _saved_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            mcp_env.cleanup()

        logger.info(
            "后端执行完成 | status=%s steps=%d duration=%.1fs traces=%d",
            result.status, len(result.steps), duration, len(result.traces),
        )
        if result.status == "error":
            logger.warning("后端执行报错: %s", result.error or "(无错误信息)")
        if result.status == "timeout":
            logger.warning("后端执行超时 (%ss)", agent_kwargs.get("timeout_s"))

        # V3.3 P3：熔断降级——步数超限检查 + 熔断记录
        cb = get_circuit_breaker(results_dir)
        step_overflow = not cb.check_steps(len(result.steps))
        if step_overflow:
            logger.warning(
                "步数超限防死循环 | steps=%d > max_steps=%d，标记为 step_overflow",
                len(result.steps), cb.config.max_steps_per_task,
            )
        # 记录熔断统计
        if result.status == "error":
            cb.record_failure("error", duration)
        elif result.status == "timeout":
            cb.record_failure("timeout", duration)
        elif step_overflow:
            cb.record_failure("step_overflow", duration)
        else:
            cb.record_success(duration, len(result.steps))
        # 每日采样审计
        sampler = get_daily_sampler(results_dir)
        sampled_for_audit = sampler.should_sample(run_id)
        if sampled_for_audit:
            logger.info("每日采样审计 | run_id=%s 已加入人工复核队列", run_id)

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

        # M2 扩展：MCP server 健康检查结果写入 metrics
        if mcp_health:
            metrics["mcp_health"] = [
                {"name": name, "ok": ok, "detail": detail}
                for name, ok, detail in mcp_health
            ]

        # V2.9：记录使用的经验记忆 ID
        if used_memory_ids:
            metrics["used_memory_ids"] = used_memory_ids

        # V3.3 P3：熔断降级和采样信息写入 metrics
        metrics["circuit_breaker"] = cb.get_status()
        if step_overflow:
            metrics["step_overflow"] = True
            metrics["step_count"] = len(result.steps)
        if sampled_for_audit:
            metrics["sampled_for_audit"] = True

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

        # 计算评测置信度（V3.0）
        try:
            all_runs_dir = run_dir.parent  # results/runs/
            confidence = calculate_run_confidence(
                record.to_dict(),
                all_runs_dir=all_runs_dir,
            )
            metrics["confidence"] = confidence
            logger.info(
                "置信度: %.1f (%s) | %s",
                confidence["score"],
                confidence["level"],
                "; ".join(confidence["suggestions"][:2]),
            )
        except Exception as e:
            logger.warning("置信度计算失败: %s", e)

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

        # V3.4 P4：数据飞轮——失败运行自动创建待标注 badcase
        try:
            run_dict = record.to_dict()
            if should_auto_create_badcase(run_dict):
                analysis = analyze_run_failure(run_dict)
                _auto_create_badcase(run_id, task, agent_id, analysis, results_dir)
        except Exception as e:
            logger.debug("自动创建badcase失败(非致命): %s", e)

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


def _auto_create_badcase(
    run_id: str,
    task: TaskSpec,
    agent_id: str,
    analysis: dict,
    results_dir: Path,
) -> None:
    """V3.4 P4：自动创建待标注 badcase（数据飞轮采样入库）。

    仅当数据库存在时才创建（CLI 环境下可能没有 web store）。
    """
    try:
        from agent_eval.web.store import RunStore
        db_path = results_dir.parent / "run_history.db"
        if not db_path.exists():
            logger.debug("数据库不存在，跳过自动创建badcase: %s", db_path)
            return
        store = RunStore(db_path)
        # 检查是否已存在该 run_id 的 badcase
        existing, _ = store.list_badcases(run_id=run_id, limit=1)
        if existing:
            logger.debug("badcase已存在，跳过: run_id=%s", run_id)
            return
        # 创建 badcase
        bid = store.insert_badcase({
            "run_id": run_id,
            "task_id": task.id,
            "agent_id": agent_id,
            "title": f"[自动采样] {task.id} × {agent_id} - {analysis.get('category', 'failure')}",
            "description": (
                f"自动采样入库：任务{task.id}在{agent_id}上执行失败。\n"
                f"得分: {analysis.get('score', 0):.2f}\n"
                f"校验点: {analysis.get('failed_checkpoints', 0)}/{analysis.get('total_checkpoints', 0)} 未通过\n"
                f"步数: {analysis.get('step_count', 0)}\n"
                f"状态: 待人工标注确认"
            ),
            "category": analysis.get("category", "other"),
            "severity": analysis.get("severity", "P2"),
            "status": "pending",
            "root_cause": analysis.get("root_cause", ""),
            "fix_plan": analysis.get("fix_plan", ""),
            "tags": ["auto_sampled", "data_flywheel"] + analysis.get("tags", []),
        })
        logger.info(
            "数据飞轮 | 自动创建badcase: bid=%s run_id=%s task=%s agent=%s category=%s severity=%s",
            bid, run_id, task.id, agent_id,
            analysis.get("category"), analysis.get("severity"),
        )
    except ImportError:
        logger.debug("web.store不可用，跳过自动创建badcase")
    except Exception as e:
        logger.debug("自动创建badcase失败: %s", e)
