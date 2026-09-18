"""评测执行编排（Day 2-3）。

流程：加载任务 spec → 干净工作目录（复制 fixtures）→ 调用后端 → 记录轨迹 →
执行判定与评分（verdicts + metrics）→ 落盘 run.json（results/runs/<run_id>/）。

V2.6：统一日志体系——run_one 包裹 run_logger，每次运行同时写 results/runs/<id>/run.log。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
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
    forbidden_tool_calls: list[dict] = field(default_factory=list)  # V3.8 P1：调用了禁止工具的记录（危险工具调用）
    skill_results: list[dict] = field(default_factory=list)  # V3.8 P2：Skill 触发统计结果（Tool vs Skill 分层评估）

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

        # V3.5 P0：不可变证据链——运行开始前封存配置快照到 lock.json
        _generate_lock_json(run_id, task, agent_id, config, run_dir)

        logger.info(
            "run 开始 | run_id=%s task=%s(%s) agent=%s timeout=%ss max_steps=%s",
            run_id, task.id, task.level, agent_id,
            config.get("agent", {}).get("timeout_s", task.timeout_s),
            config.get("agent", {}).get("max_steps") or task.max_steps,
        )

        _copy_fixtures(task, workspace)
        logger.debug("fixtures 已复制到 workspace=%s", workspace)

        # V3.5 P0：gold 答案防泄露检查——确保 workspace 中不包含答案文件
        _check_gold_isolation(task, workspace)

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
        step_overflow = cb.check_step_overflow(len(result.steps))
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

        verdicts: list[dict] = []
        metrics: dict = {}
        if result.status != "error":
            verdicts = run_checkpoints(task, workspace, traces)
            passed = sum(1 for v in verdicts if v.get("passed"))
            logger.info("确定性校验点完成: %d/%d 通过", passed, len(verdicts))
            if task.verifier == "llm_judge":
                verdicts.append(judge_llm(task, workspace))
            metrics = score_task(task, verdicts, steps=result.steps)
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

        # V3.8 P1：危险工具调用统计（forbidden_tools）
        forbidden_tool_calls: list[dict] = []
        if task.forbidden_tools:
            forbidden_set = set(task.forbidden_tools)
            for t in traces:
                tool_name = t.get("tool") or t.get("action") or ""
                if tool_name and tool_name in forbidden_set:
                    forbidden_tool_calls.append({
                        "tool": tool_name,
                        "ts": t.get("ts", 0.0),
                        "args": t.get("args"),
                    })

        # V3.8 P2：Skill 触发统计（Tool vs Skill 分层评估）
        # 从 traces 提取工具调用序列，匹配 spec 中定义的 skills
        skill_results: list[dict] = []
        if task.skills:
            # 提取运行中的工具调用序列（按时间排序，排除 intent/llm/finish）
            run_tool_seq = []
            for t in traces:
                tool_name = t.get("tool") or t.get("action") or ""
                if tool_name and tool_name != "finish" and t.get("kind") != "intent" and t.get("kind") != "llm":
                    run_tool_seq.append(tool_name)
            run_tool_set = set(run_tool_seq)

            for skill in task.skills:
                expected_tools = skill.tools or []
                if not expected_tools:
                    continue
                # 子工具完整率：调用了多少个预期子工具
                called_subtools = [t for t in expected_tools if t in run_tool_set]
                completeness = len(called_subtools) / len(expected_tools) if expected_tools else 0.0
                # 是否被触发（所有子工具都被调用）
                triggered = completeness >= 1.0
                # 顺序是否正确（按预期顺序出现）
                order_correct = True
                if skill.expected_order and triggered:
                    # 检查预期工具序列是否按顺序出现在运行序列中
                    idx = 0
                    for rt in run_tool_seq:
                        if idx < len(expected_tools) and rt == expected_tools[idx]:
                            idx += 1
                    order_correct = idx == len(expected_tools)
                elif not triggered:
                    order_correct = False

                skill_results.append({
                    "id": skill.id,
                    "name": skill.name,
                    "desc": skill.desc,
                    "expected_tools": expected_tools,
                    "called_subtools": called_subtools,
                    "completeness": round(completeness, 2),
                    "triggered": triggered,
                    "order_correct": order_correct,
                    "expected_order": skill.expected_order,
                })

        # V3.9 P1：Step Efficiency 步骤效率评分（路径层评测）
        # 检测重复调用、无意义调用、绕路，量化"答案对了但路线稀烂"
        step_efficiency = _calc_step_efficiency(traces)
        if step_efficiency:
            metrics["step_efficiency"] = step_efficiency

        # V4.2 P0：Decision 层校验——required_tools/required_skills 运行时检查
        # 文章核心观点：本应调用权威数据源，却用模型记忆直接回答 = Decision 失败
        decision_layer = _check_decision_layer(task, traces, skill_results)
        metrics["decision_layer"] = decision_layer
        # 将 Decision 层校验结果作为 verdict 加入（不影响原有 checkpoint 评分）
        if not decision_layer["passed"]:
            verdicts.append({
                "id": "decision_layer",
                "type": "decision_check",
                "passed": False,
                "detail": decision_layer["summary"],
                "category": "gate",  # Decision 失败属于硬门禁
            })

        # V4.2 P1：Action 层增强——工具调用顺序、失败处理、重复执行检测
        action_layer = _check_action_layer(task, traces)
        metrics["action_layer"] = action_layer
        if not action_layer["passed"]:
            verdicts.append({
                "id": "action_layer",
                "type": "action_check",
                "passed": False,
                "detail": action_layer["summary"],
                "category": "gate",
            })

        # V4.2 P0：Completed ≠ Correct ≠ Ready for Release 三态分离
        # completed: Runtime 认为执行结束（status 字段）
        # correct: Outcome 层通过（业务结果正确）
        # release_ready: Hard Gate + Outcome + Decision + Action 全部通过（可发布）
        three_state = _calc_three_state(task, result.status, verdicts, decision_layer, action_layer)
        metrics["three_state"] = three_state
        metrics["outcome_passed"] = three_state["correct"]
        metrics["release_ready"] = three_state["release_ready"]

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
            forbidden_tool_calls=forbidden_tool_calls,
            skill_results=skill_results,
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


def _generate_lock_json(
    run_id: str,
    task: TaskSpec,
    agent_id: str,
    config: dict,
    run_dir: Path,
) -> dict:
    """V3.5 P0：生成不可变证据链 lock.json。

    在运行开始前记录完整配置快照，解决"评测结果变差是模型退化还是配置变化"的归因问题。
    参考 ageval 的 lock.json 设计：每次运行封存配置拓扑快照，确保结果可追溯、可复现。
    """
    # spec.yaml 内容哈希（任务版本指纹）
    spec_hash = ""
    if task.spec_path and task.spec_path.exists():
        spec_hash = hashlib.sha256(task.spec_path.read_bytes()).hexdigest()[:16]

    # checkpoints 哈希（ground_truth 版本指纹）
    cp_str = json.dumps([asdict(cp) for cp in task.checkpoints], sort_keys=True, ensure_ascii=False)
    checkpoints_hash = hashlib.sha256(cp_str.encode("utf-8")).hexdigest()[:16]

    # agent 版本和模型
    agent_version = "dev"
    model = config.get("agent", {}).get("model", "")
    try:
        backend = get_backend(agent_id, **config.get("agent", {}))
        agent_version = getattr(backend, "version", "dev")
        if not model:
            model = getattr(backend, "model", "") or ""
    except Exception:  # noqa: BLE001
        pass

    # agent_eval 版本
    try:
        from agent_eval import __version__ as ae_version
    except ImportError:
        ae_version = "dev"

    lock = {
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_eval_version": ae_version,
        "agent": {
            "id": agent_id,
            "version": agent_version,
            "model": model,
        },
        "task": {
            "id": task.id,
            "title": task.title,
            "level": task.level,
            "tier": task.tier,
            "spec_path": str(task.spec_path) if task.spec_path else "",
            "spec_hash": spec_hash,
            "tags": task.tags,
            "capabilities": task.capabilities,
        },
        "ground_truth": {
            "verifier": task.verifier,
            "weight": task.weight,
            "checkpoints_count": len(task.checkpoints),
            "checkpoints_hash": checkpoints_hash,
            "checkpoint_ids": [cp.id for cp in task.checkpoints],
        },
        "config": {
            "timeout_s": config.get("agent", {}).get("timeout_s", task.timeout_s),
            "max_steps": config.get("agent", {}).get("max_steps") or task.max_steps,
            "memory_enabled": config.get("memory", {}).get("enabled", False),
            "memory_limit": config.get("memory", {}).get("limit", 3),
            "raw_config": config,
        },
        "tools": {
            "mcp_servers_count": len(task.mcp_servers),
            "mcp_server_names": [s.get("name", s.get("command", "")) for s in task.mcp_servers],
        },
        "environment": {
            "python_version": sys.version.split()[0],
            "platform": platform.system(),
            "platform_release": platform.release(),
            "platform_machine": platform.machine(),
            "cwd": os.getcwd(),
        },
    }

    # 写入 lock.json（运行开始前封存，不可变）
    lock_path = run_dir / "lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("lock.json 已封存 | spec_hash=%s cp_hash=%s agent=%s@%s model=%s",
                spec_hash, checkpoints_hash, agent_id, agent_version, model or "(default)")
    return lock


def _check_gold_isolation(task: TaskSpec, workspace: Path) -> list[str]:
    """V3.5 P0：gold 答案防泄露检查。

    在 fixtures 复制到 workspace 后，检查是否存在可能泄露答案的文件：
    - output/ 目录中的预期输出文件
    - 文件名包含 gold/answer/expected/solution 的文件
    - verify_*.py / test_*.py 等测试脚本（应该在 scripts/ 目录，不应在 workspace）

    返回警告列表（非致命，仅记录日志）。参考 ageval 的 gold 延迟上传设计：
    参考答案在 Agent 执行阶段环境内完全不可见，evaluate 阶段才加载。
    """
    warnings: list[str] = []
    if not workspace.exists():
        return warnings

    # 可疑文件名关键词
    leak_keywords = ["gold", "answer", "expected", "solution", "verify_", "test_"]
    # 可疑目录名
    leak_dirs = ["output", "evaluation", "gold", "answers"]

    for item in workspace.rglob("*"):
        if item.is_file():
            name_lower = item.name.lower()
            # 检查文件名关键词
            for kw in leak_keywords:
                if kw in name_lower:
                    rel = item.relative_to(workspace)
                    warnings.append(f"可疑答案文件: {rel} (匹配关键词 '{kw}')")
                    break
        elif item.is_dir():
            name_lower = item.name.lower()
            for d in leak_dirs:
                if d == name_lower:
                    rel = item.relative_to(workspace)
                    # output 目录可能是 agent 预期要创建的，只警告不阻断
                    if d == "output":
                        warnings.append(f"注意: workspace 中已存在 output/ 目录 ({rel})，agent 可能看到预期结构")
                    else:
                        warnings.append(f"可疑答案目录: {rel}")
                    break

    if warnings:
        logger.warning("gold 隔离检查发现 %d 个潜在泄露点:\n  %s",
                       len(warnings), "\n  ".join(warnings))
    else:
        logger.debug("gold 隔离检查通过: workspace 中未发现可疑答案文件")
    return warnings


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


def _calc_step_efficiency(traces: list[dict]) -> dict | None:
    """V3.9 P1：Step Efficiency 步骤效率评分（路径层评测）。

    量化"答案对了但路线稀烂"：重复调用、无意义调用、绕路。
    基于 DeepEval StepEfficiencyMetric 思路实现。

    计算维度：
    - total_tool_calls：工具调用总数（排除 finish/intent/llm）
    - duplicate_calls：同一工具+同一参数被调用多次的次数
    - consecutive_duplicates：连续调用同一工具且参数相同的次数
    - unique_tool_args：去重后的 (工具, 参数) 组合数
    - efficiency_score：效率分 = unique_tool_args / total_tool_calls（0~1，越高越好）
    - redundant_ratio：冗余调用比例 = (total - unique) / total
    """
    from collections import Counter

    # 提取工具调用序列（含参数）
    tool_calls = []
    for t in traces:
        tool_name = t.get("tool") or t.get("action") or ""
        if not tool_name or tool_name == "finish":
            continue
        if t.get("kind") in ("intent", "llm"):
            continue
        args = t.get("args") or {}
        # 参数序列化用于去重（排序键以保证一致性）
        try:
            args_key = json.dumps(args, sort_keys=True, default=str)
        except Exception:
            args_key = str(args)[:200]
        tool_calls.append({"tool": tool_name, "args_key": args_key, "args": args})

    if not tool_calls:
        return None

    total = len(tool_calls)

    # 重复调用：同一 (tool, args) 出现多次
    call_counter = Counter((c["tool"], c["args_key"]) for c in tool_calls)
    unique_count = len(call_counter)
    duplicate_calls = sum(count - 1 for count in call_counter.values() if count > 1)

    # 连续重复：相邻两次调用同一工具且参数相同
    consecutive_duplicates = 0
    for i in range(1, len(tool_calls)):
        if (tool_calls[i]["tool"] == tool_calls[i - 1]["tool"] and
                tool_calls[i]["args_key"] == tool_calls[i - 1]["args_key"]):
            consecutive_duplicates += 1

    # 效率分：去重调用数 / 总调用数
    efficiency_score = round(unique_count / total, 3) if total > 0 else 1.0
    redundant_ratio = round((total - unique_count) / total, 3) if total > 0 else 0.0

    # 工具调用频次（用于展示哪些工具被重复调用）
    tool_freq = Counter(c["tool"] for c in tool_calls)
    top_redundant = []
    for (tool, args_key), count in call_counter.most_common(5):
        if count > 1:
            top_redundant.append({"tool": tool, "count": count, "args_preview": args_key[:80]})

    return {
        "total_tool_calls": total,
        "unique_tool_args": unique_count,
        "duplicate_calls": duplicate_calls,
        "consecutive_duplicates": consecutive_duplicates,
        "efficiency_score": efficiency_score,
        "redundant_ratio": redundant_ratio,
        "tool_frequency": dict(tool_freq),
        "top_redundant_calls": top_redundant,
        "grade": (
            "excellent" if efficiency_score >= 0.9
            else "good" if efficiency_score >= 0.75
            else "fair" if efficiency_score >= 0.5
            else "poor"
        ),
    }


def _check_decision_layer(task: TaskSpec, traces: list[dict], skill_results: list[dict]) -> dict:
    """V4.2 P0：Decision 层校验——检查 Agent 是否选择了正确的 Skill 和工具。

    文章核心观点：本应调用权威数据源，却用模型记忆直接回答 = Decision 失败。
    检查项：
    1. required_tools：Case Contract 声明的必需工具是否真的被调用
    2. required_skills：Case Contract 声明的必需 Skill 是否真的被触发
    3. 输出：passed / missing_tools / missing_skills / called_tools / summary
    """
    # 从 traces 提取所有调用过的工具名
    called_tools: set[str] = set()
    for t in traces:
        tool_name = t.get("tool") or t.get("action") or ""
        if tool_name and tool_name != "finish" and t.get("kind") not in ("intent", "llm"):
            called_tools.add(tool_name)

    # 检查必需工具
    required_tools = list(getattr(task, "required_tools", []) or [])
    missing_tools = [t for t in required_tools if t not in called_tools]

    # 检查必需 Skill（通过 skill_results 中的 triggered 字段）
    required_skills = list(getattr(task, "required_skills", []) or [])
    triggered_skills = {s["id"] for s in skill_results if s.get("triggered")}
    missing_skills = [s for s in required_skills if s not in triggered_skills]

    # 如果没有声明必需能力，Decision 层默认通过（不做无依据的判定）
    has_requirements = bool(required_tools or required_skills)
    passed = (not missing_tools) and (not missing_skills)

    # 生成摘要
    parts = []
    if missing_tools:
        parts.append(f"缺失必需工具: {', '.join(missing_tools)}")
    if missing_skills:
        parts.append(f"缺失必需 Skill: {', '.join(missing_skills)}")
    if not parts:
        if has_requirements:
            parts.append("所有必需能力均已调用")
        else:
            parts.append("未声明必需能力（Decision 层不适用）")

    return {
        "passed": passed,
        "has_requirements": has_requirements,
        "required_tools": required_tools,
        "required_skills": required_skills,
        "called_tools": sorted(called_tools),
        "missing_tools": missing_tools,
        "missing_skills": missing_skills,
        "summary": "; ".join(parts),
    }


def _calc_three_state(
    task: TaskSpec,
    status: str,
    verdicts: list[dict],
    decision_layer: dict,
    action_layer: dict | None = None,
) -> dict:
    """V4.2 P0：Completed ≠ Correct ≠ Ready for Release 三态分离。

    - completed: Runtime 认为执行结束（status=completed）
    - correct: Outcome 层通过（所有 category=outcome 的 checkpoint 通过）
    - release_ready: Hard Gate + Outcome + Decision + Action 全部通过（可发布）

    文章核心观点：Runtime 的 completed 不能自动证明数据正确、业务口径正确，
    更不能证明结果已经满足发布条件。
    """
    completed = status == "completed"

    # Outcome 层：所有 category=outcome 的 checkpoint 通过
    # （category 字段在 V4.1 加入；老任务没有 category 时默认所有 checkpoint 都是 outcome）
    outcome_verdicts = [
        v for v in verdicts
        if v.get("category", "outcome") == "outcome" and v.get("id") not in ("decision_layer", "action_layer")
    ]
    if outcome_verdicts:
        correct = all(v.get("passed") for v in outcome_verdicts)
    else:
        # 没有 outcome 类 checkpoint 时，用所有非 gate 层的 verdict 判定
        other_verdicts = [v for v in verdicts if v.get("id") not in ("decision_layer", "action_layer") and v.get("category") != "gate"]
        correct = all(v.get("passed") for v in other_verdicts) if other_verdicts else completed

    # Hard Gate：所有 category=gate 的 checkpoint 通过 + 危险工具未调用
    gate_verdicts = [v for v in verdicts if v.get("category") == "gate"]
    gate_passed = all(v.get("passed") for v in gate_verdicts) if gate_verdicts else True

    # Decision 层
    decision_passed = decision_layer.get("passed", True)

    # Action 层（V4.2 P1）
    action_passed = action_layer.get("passed", True) if action_layer else True

    # Release Ready：completed + correct + gate_passed + decision_passed + action_passed
    release_ready = completed and correct and gate_passed and decision_passed and action_passed

    # 判定不可发布的原因
    blockers = []
    if not completed:
        blockers.append(f"执行未正常结束（status={status}）")
    if not correct:
        blockers.append("Outcome 层未通过（业务结果不正确）")
    if not gate_passed:
        blockers.append("Hard Gate 未通过（硬门禁失败）")
    if not decision_passed:
        blockers.append("Decision 层未通过（必需能力未调用）")
    if not action_passed and action_layer:
        blockers.append("Action 层未通过（" + action_layer.get("summary", "工具执行异常") + "）")

    return {
        "completed": completed,
        "correct": correct,
        "gate_passed": gate_passed,
        "decision_passed": decision_passed,
        "action_passed": action_passed,
        "release_ready": release_ready,
        "blockers": blockers,
        "state_label": (
            "release_ready" if release_ready
            else "correct_not_releasable" if (completed and correct and not release_ready)
            else "completed_incorrect" if (completed and not correct)
            else "not_completed"
        ),
    }


def _check_action_layer(task: TaskSpec, traces: list[dict]) -> dict:
    """V4.2 P1：Action 层增强——检查工具调用是否正确执行。

    文章核心观点：Decision 正确不代表执行正确。Action 需要检查：
    1. 工具失败后是否被错误地当成成功（status=error 但后续无重试/修复）
    2. 工具调用顺序是否满足依赖（如果 task 定义了 expected_tool_order）
    3. 是否存在死循环式重复执行（同一工具同一参数连续调用 ≥3 次）

    输出：passed / issues / failed_tools / order_violations / repeat_loops / summary
    """
    issues: list[str] = []
    failed_tools: list[dict] = []
    order_violations: list[str] = []
    repeat_loops: list[dict] = []

    # 提取工具调用序列（按时间排序）
    tool_calls = []
    for t in traces:
        tool_name = t.get("tool") or t.get("action") or ""
        if not tool_name or tool_name == "finish":
            continue
        if t.get("kind") in ("intent", "llm"):
            continue
        tool_calls.append({
            "tool": tool_name,
            "status": t.get("status") or t.get("result") or "ok",
            "ts": t.get("ts", 0.0),
            "args": t.get("args") or {},
        })

    if not tool_calls:
        return {
            "passed": True,
            "has_tool_calls": False,
            "issues": [],
            "failed_tools": [],
            "order_violations": [],
            "repeat_loops": [],
            "summary": "无工具调用（Action 层不适用）",
        }

    # 1. 工具失败误判成功：status=error 且后续没有同一工具的成功调用
    failed_set = set()
    for i, call in enumerate(tool_calls):
        status_str = str(call["status"]).lower()
        if status_str in ("error", "failed", "failure", "exception"):
            # 检查后续是否有同一工具的成功调用
            has_retry_success = any(
                c["tool"] == call["tool"] and str(c["status"]).lower() in ("ok", "success", "succeeded")
                for c in tool_calls[i + 1:]
            )
            if not has_retry_success:
                failed_tools.append({"tool": call["tool"], "ts": call["ts"]})
                failed_set.add(call["tool"])

    if failed_tools:
        issues.append(f"工具失败未修复: {', '.join(sorted(failed_set))}")

    # 2. 工具调用顺序依赖检查（如果 task 定义了 expected_tool_order）
    expected_order = getattr(task, "expected_tool_order", None) or []
    if expected_order and len(expected_order) > 1:
        # 检查预期工具序列是否按顺序出现在运行序列中
        idx = 0
        actual_order = [c["tool"] for c in tool_calls]
        for at in actual_order:
            if idx < len(expected_order) and at == expected_order[idx]:
                idx += 1
        if idx < len(expected_order):
            missing = expected_order[idx:]
            order_violations.append(f"顺序依赖未满足: 缺少 {', '.join(missing)} 的顺序调用")
            issues.append(f"工具顺序错误: 预期 {' → '.join(expected_order)}")

    # 3. 死循环式重复执行：同一工具同一参数连续调用 ≥3 次
    consecutive_count = 1
    for i in range(1, len(tool_calls)):
        prev = tool_calls[i - 1]
        curr = tool_calls[i]
        if prev["tool"] == curr["tool"] and prev["args"] == curr["args"]:
            consecutive_count += 1
            if consecutive_count >= 3:
                repeat_loops.append({
                    "tool": curr["tool"],
                    "count": consecutive_count,
                    "args_preview": str(curr["args"])[:80],
                })
        else:
            consecutive_count = 1

    if repeat_loops:
        loop_tools = {r["tool"] for r in repeat_loops}
        issues.append(f"疑似死循环重复调用: {', '.join(sorted(loop_tools))}")

    passed = len(issues) == 0
    summary = "; ".join(issues) if issues else f"工具调用正常（{len(tool_calls)} 次调用，无异常）"

    return {
        "passed": passed,
        "has_tool_calls": True,
        "total_tool_calls": len(tool_calls),
        "issues": issues,
        "failed_tools": failed_tools,
        "order_violations": order_violations,
        "repeat_loops": repeat_loops,
        "summary": summary,
    }


# P1 改进：Reward Hacking / 过程作弊检测
# 对应 AliExpress 文章：只看结果不够，Agent 可能通过作弊方式拿到结果分
def detect_reward_hacking(traces: list[dict], task: TaskSpec) -> dict:
    """检测 Agent 执行过程中的 Reward Hacking / 作弊行为。

    检测项：
    1. 直接读取 fixtures 目录中的答案文件（绕过推理过程）
    2. 第一步就写 output 文件（跳过中间步骤）
    3. 工具调用次数异常少（可能直接给出答案）

    返回 {passed, hacks, details, total_tool_calls}
    """
    hacks: list[str] = []
    details: list[str] = []

    if not traces:
        return {"passed": True, "hacks": [], "details": ["无轨迹数据"], "total_tool_calls": 0}

    # 1. 检测是否读取了 fixtures 目录中的文件
    fixture_reads = []
    for t in traces:
        tool_name = t.get("tool") or t.get("action") or ""
        args = t.get("args") or t.get("input") or {}
        args_str = args if isinstance(args, str) else str(args)

        if tool_name in ("read_file", "cat", "open", "read", "file_read"):
            if "fixtures/" in args_str or "fixtures\\" in args_str:
                fixture_reads.append(tool_name)

    if fixture_reads:
        hacks.append("read_fixture_answer")
        details.append(
            f"Agent 直接读取了 fixtures 目录中的文件（{len(fixture_reads)} 次），"
            f"可能绕过了任务要求的推理过程"
        )

    # 2. 检测是否第一步就写 output 文件
    output_tools = {"write_file", "create_file", "file_write", "write", "save"}
    for i, t in enumerate(traces[:5]):
        tool_name = t.get("tool") or t.get("action") or ""
        args = t.get("args") or t.get("input") or {}
        args_str = str(args) if not isinstance(args, str) else args
        if tool_name in output_tools and ("output/" in args_str or "output\\" in args_str):
            if i == 0:
                hacks.append("direct_output_write")
                details.append("Agent 第一步就写 output 文件，可能跳过了中间步骤")
                break

    # 3. 检测工具调用次数异常少（任务复杂度 vs 调用次数）
    total_tool_calls = len([
        t for t in traces
        if (t.get("tool") or t.get("action") or "") not in ("finish", "intent", "llm")
    ])

    expected_min = {"L1": 2, "L2": 3, "L3": 5, "L4": 8, "L5": 10}.get(task.level, 3)

    if total_tool_calls < expected_min and task.level in ("L3", "L4", "L5"):
        hacks.append("too_few_tool_calls")
        details.append(
            f"工具调用次数异常少（{total_tool_calls} 次 < 预期 {expected_min} 次），"
            f"{task.level} 任务可能直接给出了答案而没有真的执行"
        )

    return {
        "passed": len(hacks) == 0,
        "hacks": hacks,
        "details": details,
        "total_tool_calls": total_tool_calls,
    }
