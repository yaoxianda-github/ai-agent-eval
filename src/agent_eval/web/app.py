"""V2.1 Web 工作台：FastAPI 服务层。

复用现有 CLI 引擎（零重写）：
- spec.load_task_pack / find_tasks_dir   → 任务
- backends.list_backends / _BACKENDS     → 后端
- runner.run_one                         → 执行 + 落盘 run.json
- reporter.load_runs / summarize / render_html → 汇总与报告
- web.store.RunStore                     → SQLite 历史索引
- web.taskgen.generate_task_pack         → 新建任务

说明：run.json 是权威结果；SQLite 仅作历史索引。运行中 run 走内存态 running{}。
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from dataclasses import asdict
from datetime import datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from agent_eval import license as license_mod
from agent_eval.backends import _BACKENDS, list_backends
from agent_eval.log import get_logger, setup_logging
from agent_eval.reporter import load_runs, render_html, summarize
from agent_eval.runner import default_results_dir, run_one
from agent_eval.spec import find_tasks_dir, load_manifest, load_task_pack
from agent_eval.stats import summarize_scores
from agent_eval.traces import tool_category
from agent_eval.web.store import RunStore
from agent_eval.web.taskgen import generate_task_pack

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_READABLE_EXTS = {
    ".md", ".txt", ".csv", ".json", ".log", ".py", ".yaml", ".yml", ".xml", ".html", ".svg",
}
_MAX_FILE_BYTES = 200 * 1024


def _clean_surrogates(v):
    """清洗孤立代理项/异常控制符，防止前端显示 � 乱码（双保险，前端 clip 也已兜底）。"""
    if isinstance(v, dict):
        return {k: _clean_surrogates(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean_surrogates(x) for x in v]
    if isinstance(v, str):
        # 孤立代理项 → 丢弃；其他控制符（除 \n \t \r）→ 空格
        cleaned = v.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")
        return "".join(ch if (ch in "\n\t\r" or ord(ch) >= 32) else " " for ch in cleaned)
    return v


def _app_version() -> str:
    try:
        return _pkg_version("agent-eval")
    except Exception:  # noqa: BLE001 - 未安装时回退
        return "0.1.0"


def _build_regression_description(b: dict) -> str:
    """根据 badcase 信息生成回归评测用例的描述。"""
    lines = [
        f"【回归测试用例】由 badcase {b.get('id', '')} 转化生成",
        f"原始任务: {b.get('task_id', '')} × {b.get('agent_id', '')}",
        f"问题分类: {b.get('category', '')}",
        f"严重程度: {b.get('severity', '')}",
        "",
        "【问题描述】",
        b.get("description", ""),
    ]
    if b.get("root_cause"):
        lines.extend(["", "【根因分析】", b["root_cause"]])
    if b.get("fix_plan"):
        lines.extend(["", "【修复方案】", b["fix_plan"]])
    lines.extend([
        "",
        "【评测要求】",
        "Agent 必须正确处理此场景，避免复现上述问题。",
        "请将最终结果写入 output/result.md 文件。",
    ])
    return "\n".join(lines)


def _auto_diagnose_run(run_data: dict, failed_checkpoints: list, category: str) -> dict:
    """根据运行记录自动归因：生成根因分析和修复建议。

    基于失败的 checkpoint 类型、错误信息、运行状态、token 消耗等信号，
    推断可能的失败原因和修复方向。这是启发式归因，不保证 100% 准确，
    供人工参考和修正。
    """
    status = run_data.get("status", "")
    error = run_data.get("error", "")
    metrics = run_data.get("metrics", {})
    steps = metrics.get("steps", 0)
    max_steps = run_data.get("max_steps", 0)
    duration = metrics.get("duration_s", 0)

    root_cause_parts = []
    fix_parts = []

    # 1. 状态级归因
    if status == "timeout":
        root_cause_parts.append("【超时归因】任务在规定时间内未完成。")
        if steps > 0:
            root_cause_parts.append(f"已执行 {steps} 步仍未完成，可能存在：")
            root_cause_parts.append("- 循环依赖：Agent 在相同步骤间反复跳转")
            root_cause_parts.append("- 工具调用链过长：不必要的多轮工具调用")
            root_cause_parts.append("- 等待外部响应阻塞：某个工具调用耗时过长")
        fix_parts.append("【修复方向】")
        fix_parts.append("- 检查 Agent 的循环检测机制，避免无限重试")
        fix_parts.append("- 优化任务拆解，减少不必要的工具调用轮次")
        fix_parts.append("- 为耗时工具调用设置合理的超时和降级策略")
    elif status == "error":
        root_cause_parts.append("【异常崩溃归因】Agent 执行过程中发生未捕获异常。")
        if error:
            root_cause_parts.append(f"错误信息: {error[:300]}")
        root_cause_parts.append("可能原因：")
        root_cause_parts.append("- 工具调用参数错误：Agent 生成的参数不符合工具 schema")
        root_cause_parts.append("- 后端服务异常：模型 API 或工具服务不可用")
        root_cause_parts.append("- 数据解析失败：Agent 输出格式无法被解析")
        fix_parts.append("【修复方向】")
        fix_parts.append("- 增强工具调用的参数校验和错误恢复机制")
        fix_parts.append("- 检查后端服务的健康状态和重试策略")
        fix_parts.append("- 优化输出格式约束，增加格式容错解析")
    elif status == "max_steps":
        root_cause_parts.append(f"【步数超限归因】达到最大步数限制（{max_steps} 步）。")
        root_cause_parts.append("Agent 在有限步数内未能完成任务，可能存在：")
        root_cause_parts.append("- 规划能力不足：任务拆解不合理，步骤浪费")
        root_cause_parts.append("- 工具使用效率低：单次工具调用获取信息不充分")
        root_cause_parts.append("- 缺乏终止判断：Agent 不知道何时可以结束任务")
        fix_parts.append("【修复方向】")
        fix_parts.append("- 优化 system prompt，强调高效规划和尽早完成")
        fix_parts.append("- 增加任务完成度的自我评估机制")
        fix_parts.append("- 考虑提高 max_steps 或优化任务难度")

    # 2. Checkpoint 级归因
    if failed_checkpoints:
        root_cause_parts.append(f"【校验点失败归因】共 {len(failed_checkpoints)} 个校验点未通过：")
        for v in failed_checkpoints:
            cid = v.get("checkpoint_id", "?")
            ctype = v.get("checkpoint_type", "?")
            detail = v.get("detail", "") or v.get("message", "")
            root_cause_parts.append(f"- {cid} ({ctype}): {detail[:100]}")

        # 按 checkpoint 类型给出修复建议
        content_fails = [v for v in failed_checkpoints if v.get("checkpoint_type", "").startswith("content_")]
        file_fails = [v for v in failed_checkpoints if v.get("checkpoint_type", "") == "file_exists"]
        cmd_fails = [v for v in failed_checkpoints if v.get("checkpoint_type", "") == "cmd_exit_zero"]

        if content_fails:
            fix_parts.append("- 内容校验失败：检查 Agent 的输出理解和生成能力，可能需要：")
            fix_parts.append("  - 增强任务指令的明确性，减少歧义")
            fix_parts.append("  - 优化输出格式约束，确保关键信息不遗漏")
            fix_parts.append("  - 检查 RAG 检索质量，确保 Agent 获取了正确的上下文")
        if file_fails:
            fix_parts.append("- 文件存在校验失败：Agent 未生成预期的输出文件，可能需要：")
            fix_parts.append("  - 在任务描述中明确要求生成特定文件")
            fix_parts.append("  - 检查 Agent 的文件写入工具是否正常工作")
            fix_parts.append("  - 优化终止条件，确保 Agent 在完成后才结束")
        if cmd_fails:
            fix_parts.append("- 命令执行校验失败：Agent 执行的命令返回非零退出码，可能需要：")
            fix_parts.append("  - 检查 Agent 生成的命令参数是否正确")
            fix_parts.append("  - 增强命令执行的错误处理和重试机制")
            fix_parts.append("  - 优化工具使用说明，减少命令生成错误")

    # 3. 效率归因（即使通过了，也可以给出优化建议）
    if not root_cause_parts:
        root_cause_parts.append("【归因】未检测到明显的失败模式，建议人工进一步分析运行轨迹。")

    if not fix_parts:
        fix_parts.append("【修复方向】建议人工查看运行轨迹，定位具体失败原因。")

    # 4. 通用建议
    fix_parts.append("")
    fix_parts.append("【通用建议】")
    fix_parts.append("- 将此 badcase 转化为回归评测用例，持续验证修复效果")
    fix_parts.append("- 记录修复过程中的关键发现，沉淀为团队经验")
    fix_parts.append("- 定期回顾类似 badcase，识别系统性问题")

    return {
        "root_cause": "\n".join(root_cause_parts),
        "fix_plan": "\n".join(fix_parts),
    }


def create_app(
    tasks_dir: Path | None = None,
    results_dir: Path | None = None,
    report_dir: Path | None = None,
    db_path: Path | None = None,
) -> FastAPI:
    setup_logging()
    tasks_dir = Path(tasks_dir) if tasks_dir else find_tasks_dir()
    results_dir = Path(results_dir) if results_dir else default_results_dir()
    report_dir = Path(report_dir) if report_dir else Path("reports")

    app = FastAPI(title="AI Agent 评测工作台", version=_app_version())
    logger.info("工作台启动 | tasks_dir=%s results_dir=%s version=%s", tasks_dir, results_dir, _app_version())

    db_path = db_path if db_path is not None else results_dir.parent / "run_history.db"
    store = RunStore(db_path)
    store.rebuild(results_dir)

    # 启动时清理僵尸 batch：状态为 running 但最后心跳超过 10 分钟（执行线程已死）
    # 用心跳而非创建时间判断：真正执行超过 1 小时的批次每完成一个 run 都会更新心跳，
    # 只有服务中断导致执行线程消失时，心跳才会停止更新。
    try:
        stale = store._conn.execute(
            "SELECT batch_id, label, last_heartbeat FROM batches WHERE status='running' "
            "AND (last_heartbeat='' OR last_heartbeat < datetime('now', 'localtime', '-10 minutes'))"
        ).fetchall()
        for bid, label, hb in stale:
            store._conn.execute(
                "UPDATE batches SET status='done', finished_at=datetime('now','localtime') "
                "WHERE batch_id=?", (bid,)
            )
            logger.warning("清理僵尸批次 | batch=%s label=%s last_heartbeat=%s（执行线程已中断，标记为 done）", bid, label, hb or "无")
        store._conn.commit()
    except Exception as e:  # noqa: BLE001 - 清理失败不影响启动
        logger.warning("僵尸批次清理跳过: %s", e)

    # 运行中 run 的内存态：run_id -> {status, task_id, agent_id, error?}
    running: dict[str, dict] = {}

    def _task_map() -> dict:
        try:
            return {t.id: t for t in load_task_pack(tasks_dir)}
        except FileNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))

    def _load_run(run_id: str) -> dict:
        p = results_dir / run_id / "run.json"
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec["steps"] = [_clean_surrogates(s) for s in (rec.get("steps") or [])]
        # 轨迹回放：V2.4 起 run.json 带 traces；旧数据由 steps 兜底合成
        if not rec.get("traces"):
            task_desc = ""
            try:
                task_desc = _task_map().get(rec.get("task_id", ""), TaskSpec(
                    id="", level="", title="", description="", input_files=[]
                )).description
            except Exception:  # noqa: BLE001 - 取不到任务描述不影响回放
                pass
            synthesized = [
                {
                    "kind": "tool",
                    "category": tool_category(s.get("action")),
                    "ts": s.get("ts", 0.0),
                    "tool": s.get("action"),
                    "args": s.get("args"),
                    "observation": s.get("observation"),
                }
                for s in (rec.get("steps") or [])
            ]
            synthesized.insert(
                0, {"kind": "intent", "ts": 0.0, "content": task_desc, "task_id": rec.get("task_id")}
            )
            rec["traces"] = [_clean_surrogates(t) for t in synthesized]
        return rec

    def _workspace(run_id: str) -> Path:
        ws = results_dir / run_id / "workspace"
        if not ws.is_dir():
            raise HTTPException(status_code=404, detail="workspace 不存在")
        return ws

    def _walk_files(ws: Path) -> list[dict]:
        out = []
        for p in sorted(ws.rglob("*")):
            if p.is_file():
                out.append(
                    {
                        "path": str(p.relative_to(ws)).replace("\\", "/"),
                        "name": p.name,
                        "size": p.stat().st_size,
                    }
                )
        return out

    def _execute_run(run_ids: list[str], task_id: str, agent_id: str, config: dict) -> None:
        try:
            task = _task_map()[task_id]
        except KeyError:
            logger.error("运行失败: 未知任务 %s (run_ids=%s)", task_id, run_ids)
            for rid in run_ids:
                running[rid] = {"status": "error", "error": f"未知任务: {task_id}"}
            return
        for rid in run_ids:
            running[rid] = {"status": "running", "task_id": task_id, "agent_id": agent_id}
            logger.info("运行开始 | run_id=%s task=%s agent=%s", rid, task_id, agent_id)
            try:
                rec = run_one(
                    task,
                    agent_id,
                    config=config,
                    results_dir=results_dir,
                    run_id=rid,
                )
                store.insert_run(rec.to_dict())
                logger.info(
                    "运行完成 | run_id=%s status=%s score=%.3f duration=%.1fs",
                    rid, rec.status, rec.metrics.get("score", 0), rec.duration_s,
                )
            except Exception as e:  # noqa: BLE001 - 单个 run 失败不中断整批
                logger.error("运行异常 | run_id=%s | %s: %s", rid, type(e).__name__, e, exc_info=True)
                running[rid] = {
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "task_id": task_id,
                    "agent_id": agent_id,
                }
                continue
            running.pop(rid, None)

    # ---------- 多 Agent 对比批次（V2.7） ----------
    def _resolve_scope_tasks(scope: str, task_ids: list[str] | None) -> list[str]:
        """把任务集选择（core/full 或显式 id 列表）解析为有序任务 id。"""
        tasks = _task_map()
        if task_ids:
            ids = [str(t) for t in task_ids]
        elif scope == "core":
            from agent_eval.ci import load_gate_config

            g = load_gate_config()["gates"]["core"]
            raw = g["tasks"]
            ids = sorted(tasks) if raw in ("*", ["*"]) else [str(t) for t in raw]
        else:  # full / all：全量
            ids = sorted(tasks)
        missing = [t for t in ids if t not in tasks]
        if missing:
            raise HTTPException(status_code=400, detail=f"存在未知任务: {missing}")
        return ids

    def _run_cost_cny(rec: dict, price: dict) -> tuple[float, int, int]:
        """从 run.json 的 usage 算 (成本元, prompt_tokens, completion_tokens)。"""
        usage = (rec.get("metrics") or {}).get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        cost = pt / 1e6 * price["input_cny_per_m"] + ct / 1e6 * price["output_cny_per_m"]
        return round(cost, 4), pt, ct

    def _build_matrix(batch: dict) -> dict:
        """按批次聚合 run.json，产出彩色矩阵 + agent 汇总 + 自动结论。"""
        from agent_eval.confidence import calculate_batch_confidence
        from agent_eval.costing import pricing_for

        price = pricing_for()
        tasks = _task_map()
        agents: list[str] = list(batch["agents"])
        task_ids: list[str] = list(batch["task_ids"])
        grid: dict[tuple[str, str], list[dict]] = {
            (a, t): [] for a in agents for t in task_ids
        }
        for rid in store.list_run_ids_by_batch(batch["batch_id"]):
            p = results_dir / rid / "run.json"
            if not p.exists():
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            key = (rec.get("agent_id", ""), rec.get("task_id", ""))
            if key not in grid:
                continue
            cost, pt, ct = _run_cost_cny(rec, price)
            grid[key].append(
                {
                    "run_id": rid,
                    "score": float((rec.get("metrics") or {}).get("score", 0.0)),
                    "pass_rate": float((rec.get("metrics") or {}).get("pass_rate", 0.0)),
                    "status": rec.get("status", ""),
                    "duration_s": float(rec.get("duration_s", 0.0)),
                    "cost_cny": cost,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "verifier": rec.get("verifier", "deterministic"),
                    "task_level": rec.get("task_level", "L2"),
                    "task_id": rec.get("task_id", ""),
                }
            )

        cells: dict[str, dict] = {}
        for (a, t), lst in grid.items():
            scores = [x["score"] for x in lst]
            st = summarize_scores(scores)
            weight = float(getattr(tasks.get(t), "weight", 1.0) or 1.0)
            cells[f"{a}|{t}"] = {
                "n": len(lst),
                "runs": sorted(lst, key=lambda x: x["run_id"]),
                "best": st["best"],
                "mean": st["mean"],
                "std": st["std"],
                "pass_rate": st["pass_rate"],
                "cost_cny": round(sum(x["cost_cny"] for x in lst), 4),
                "duration_s": round(
                    sum(x["duration_s"] for x in lst) / len(lst), 2
                ) if lst else 0.0,
                "weight": weight,
            }

        totals: dict[str, dict] = {}
        for a in agents:
            weighted_num = wsum = cost = dur = 0.0
            passed = 0
            stds: list[float] = []
            covered = 0
            for t in task_ids:
                c = cells[f"{a}|{t}"]
                if c["n"] == 0:
                    continue
                covered += 1
                weighted_num += c["best"] * c["weight"]
                wsum += c["weight"]
                if c["pass_rate"] >= 0.999:
                    passed += 1
                cost += c["cost_cny"]
                dur += sum(x["duration_s"] for x in c["runs"])
                stds.append(c["std"])
            totals[a] = {
                "weighted_score": round(weighted_num / wsum, 3) if wsum else 0.0,
                "task_pass_rate": round(passed / len(task_ids), 3) if task_ids else 0.0,
                "tasks_passed": passed,
                "tasks_total": len(task_ids),
                "covered": covered,
                "cost_cny": round(cost, 4),
                "duration_s": round(dur, 1),
                "avg_std": round(sum(stds) / len(stds), 3) if stds else 0.0,
            }

        # 批次置信度计算（V3.0）
        all_batch_runs = []
        for lst in grid.values():
            all_batch_runs.extend(lst)
        batch_confidence = calculate_batch_confidence(
            all_batch_runs,
            task_count_total=len(tasks),
            task_specs=list(tasks.values()),
        )

        # V3.2 P2：6个blocking指标门禁评估（每个agent独立评估）
        from agent_eval.gate import evaluate_gate, GateThreshold
        gate_threshold = GateThreshold()
        gate_evals = []
        for a in agents:
            # 构造 task_results 格式
            agent_task_results = []
            for t in task_ids:
                c = cells[f"{a}|{t}"]
                if c["n"] == 0:
                    continue
                agent_task_results.append({
                    "task_id": t,
                    "task_passed": c["pass_rate"] >= 0.999,
                    "duration_s": c["duration_s"],
                    "tokens": {
                        "prompt_tokens": sum(x["prompt_tokens"] for x in c["runs"]),
                        "completion_tokens": sum(x["completion_tokens"] for x in c["runs"]),
                    },
                })
            ge = evaluate_gate(
                agent_task_results,
                threshold=gate_threshold,
                task_specs=[tasks.get(t) for t in task_ids if tasks.get(t)],
                confidence_score=batch_confidence.get("score"),
            )
            gate_evals.append({"agent": a, **ge.to_dict()})
        gate_all_passed = all(ge["passed"] for ge in gate_evals)

        return {
            "agents": agents,
            "tasks": task_ids,
            "cells": cells,
            "totals": totals,
            "conclusion": _matrix_conclusion(totals),
            "confidence": batch_confidence,
            "gate_evaluation": {  # V3.2：6个blocking指标门禁
                "passed": gate_all_passed,
                "per_agent": gate_evals,
                "threshold": {
                    "golden_pass_rate": gate_threshold.golden_pass_rate,
                    "overall_accuracy": gate_threshold.overall_accuracy,
                    "p95_latency_s": gate_threshold.p95_latency_s,
                    "max_tokens_per_task": gate_threshold.max_tokens_per_task,
                    "security_pass_rate": gate_threshold.security_pass_rate,
                    "min_confidence": gate_threshold.min_confidence,
                },
            },
        }

    def _matrix_conclusion(totals: dict[str, dict]) -> list[str]:
        """根据 agent 汇总自动生成对比结论。"""
        rows = [(a, t) for a, t in totals.items() if t["covered"]]
        if not rows:
            return []
        notes: list[str] = []
        by_score = sorted(rows, key=lambda x: x[1]["weighted_score"], reverse=True)
        top_a, top = by_score[0]
        notes.append(
            f"{top_a} 加权总分最高 {top['weighted_score']}，任务通过 {top['tasks_passed']}/{top['tasks_total']}"
        )
        if len(by_score) > 1:
            gap = round(top["weighted_score"] - by_score[1][1]["weighted_score"], 3)
            if gap > 0:
                notes.append(f"领先第二名 {by_score[1][0]} {gap} 分")
            elif gap == 0:
                notes.append(f"与 {by_score[1][0]} 并列第一")
        # 成本只在"有 token 计费"的 agent 之间比较；外部黑盒后端 usage 为 0 不计入
        paid = sorted(
            [(a, t) for a, t in rows if t["cost_cny"] > 0],
            key=lambda x: x[1]["cost_cny"],
        )
        if paid:
            cheap_a, cheap = paid[0]
            line = f"{cheap_a} 计费成本最低 ¥{cheap['cost_cny']}"
            if len(paid) > 1:
                pricey_a, pricey = paid[-1]
                ratio = round(pricey["cost_cny"] / max(cheap["cost_cny"], 1e-9), 1)
                if pricey_a != cheap_a and ratio > 1:
                    line += f"，{pricey_a} 为其 {ratio} 倍"
            zero_agents = [a for a, t in rows if t["cost_cny"] == 0]
            if zero_agents:
                line += f"（{'、'.join(zero_agents)} 为外部后端，未计 token 成本）"
            notes.append(line)
        by_std = sorted(rows, key=lambda x: x[1]["avg_std"])
        stable_a, stable = by_std[0]
        notes.append(f"{stable_a} 平均波动 σ={stable['avg_std']}（越小越稳定）")
        return notes

    def _execute_batch(batch_id: str, agents: list[str], task_ids: list[str],
                       runs: int, model: str) -> None:
        plan = [(a, t, i) for a in agents for t in task_ids for i in range(runs)]
        total = len(plan)
        done = 0
        logger.info("对比批次开始 | batch=%s agents=%s tasks=%d runs=%d 共%d次",
                    batch_id, agents, len(task_ids), runs, total)
        for agent_id, task_id, _i in plan:
            rid = uuid.uuid4().hex[:12]
            try:
                task = _task_map()[task_id]
                config: dict = {"agent": {}}
                if model:
                    config["agent"]["model"] = model
                rec = run_one(
                    task, agent_id, config=config,
                    results_dir=results_dir, run_id=rid,
                )
                store.insert_run(rec.to_dict(), batch_id=batch_id)
            except Exception as e:  # noqa: BLE001 - 单次失败不中断批次
                logger.error("批次内运行失败 | batch=%s %s/%s: %s",
                             batch_id, agent_id, task_id, e, exc_info=True)
                # 插入 error 状态的 run 记录，确保失败任务在矩阵中可追踪（不再显示 "—"）
                try:
                    _t = _task_map().get(task_id)
                    store.insert_run({
                        "run_id": rid,
                        "agent_id": agent_id,
                        "agent_ver": "",
                        "task_id": task_id,
                        "task_level": _t.level if _t else "",
                        "status": "error",
                        "metrics": {"score": 0.0, "weight": 0.0, "pass_rate": 0.0},
                        "duration_s": 0.0,
                        "steps": [],
                        "error": str(e)[:500],
                    }, batch_id=batch_id)
                except Exception:  # noqa: BLE001 - 记录失败不影响主流程
                    pass
            done += 1
            store.update_batch(batch_id, done_runs=done)
        batch = store.get_batch(batch_id)
        matrix = _build_matrix(batch) if batch else {}
        store.update_batch(
            batch_id, status="done",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            summary=matrix,
        )
        logger.info("对比批次完成 | batch=%s", batch_id)
        # 社区版只保留最近 N 个批次（Pro retain=0 不限）
        ent = license_mod.get_entitlements()
        retain = int(ent.get("retain_batches", 1) or 0)
        if retain:
            older = store.list_batches(500)[retain:]
            for ob in older:
                if ob["batch_id"] != batch_id:
                    store.delete_batch(ob["batch_id"])

    # ---------- 元信息 / 任务 / 后端 ----------
    @app.get("/api/meta")
    def api_meta() -> dict:
        return {
            "version": _app_version(),
            "tasks_dir": str(tasks_dir),
            "results_dir": str(results_dir),
            "report_dir": str(report_dir),
        }

    @app.get("/api/tasks")
    def api_tasks() -> dict:
        from agent_eval.costing import estimate_cost, load_benchmark

        tasks = _task_map()
        bench = load_benchmark()
        out = []
        for t in tasks.values():
            d = _task_to_dict(t)
            est = estimate_cost(
                "minimal-react", t.id, level=t.level, verifier=t.verifier, runs=1, benchmark=bench
            )
            d["cost_estimate"] = est
            out.append(d)
        # V3.1：返回 tier 配置和 core 包定义
        manifest = load_manifest(tasks_dir)
        return {
            "tasks": out,
            "tiers": manifest.get("tiers", {}),
            "core_pack": manifest.get("core_pack", ["golden", "regression"]),
        }

    @app.get("/api/tiers")
    def api_tiers() -> dict:
        """V3.1：返回数据集4层分层配置。"""
        manifest = load_manifest(tasks_dir)
        return {
            "tiers": manifest.get("tiers", {}),
            "core_pack": manifest.get("core_pack", ["golden", "regression"]),
        }

    @app.get("/api/circuit-status")
    def api_circuit_status() -> dict:
        """V3.3 P3：返回熔断器和监控状态。"""
        from agent_eval.circuit_breaker import CircuitBreaker, CircuitConfig, DailySampler, GrayReleaseConfig

        runs_dir = results_dir / "runs"
        stats_path = results_dir.parent / "circuit_breaker.json"
        cb = CircuitBreaker(config=CircuitConfig(), stats_path=stats_path)
        sampler = DailySampler(sample_count=100, storage_path=results_dir.parent / "daily_samples.json")
        gray = GrayReleaseConfig()

        # 统计最近运行的错误率
        recent_runs, _ = store.list_runs(limit=100)
        error_count = sum(1 for r in recent_runs if r.get("status") in ("error", "timeout"))
        recent_error_rate = error_count / len(recent_runs) if recent_runs else 0.0

        return {
            "circuit_breaker": cb.get_status(),
            "daily_sampler": sampler.get_status(),
            "gray_release": gray.get_status(),
            "recent_stats": {
                "recent_runs": len(recent_runs),
                "recent_error_rate": round(recent_error_rate, 3),
                "recent_errors": error_count,
            },
        }

    @app.get("/api/costs")
    def api_costs() -> dict:
        """成本核算数据：定价 + (agent, task) 实测 token 基准 + 级别估算。"""
        from agent_eval.costing import LEVEL_ESTIMATE, load_benchmark, pricing_for

        return {
            "pricing": pricing_for(),
            "benchmark": load_benchmark().get("agents", {}),
            "default_agent": "minimal-react",
            "level_estimate": {
                k: {"prompt_tokens": v[0], "completion_tokens": v[1]}
                for k, v in LEVEL_ESTIMATE.items()
            },
        }

    @app.get("/api/backends")
    def api_backends() -> dict:
        return {
            "backends": [
                {"id": n, "version": getattr(cls, "version", "dev"),
                 "default_model": getattr(cls, "default_model", "deepseek-chat")}
                for n, cls in sorted(_BACKENDS.items())
            ]
        }

    # ---------- 运行 ----------
    @app.post("/api/runs")
    def create_run(payload: dict = Body(...)) -> dict:
        task_id = str(payload.get("task_id", ""))
        agent_id = str(payload.get("agent_id", "minimal-react"))
        runs = max(1, min(int(payload.get("runs", 1)), 20))
        if task_id not in _task_map():
            raise HTTPException(status_code=400, detail=f"未知任务: {task_id}")
        if agent_id not in list_backends():
            raise HTTPException(status_code=400, detail=f"未知后端: {agent_id}")

        config: dict = {"agent": {}}
        if payload.get("model"):
            config["agent"]["model"] = str(payload["model"])
        if payload.get("timeout_s"):
            config["agent"]["timeout_s"] = int(payload["timeout_s"])

        run_ids = [uuid.uuid4().hex[:12] for _ in range(runs)]
        for rid in run_ids:
            running[rid] = {"status": "pending", "task_id": task_id, "agent_id": agent_id}
        t = threading.Thread(
            target=_execute_run, args=(run_ids, task_id, agent_id, config), daemon=True
        )
        t.start()
        return {"run_ids": run_ids, "last_run_id": run_ids[-1]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        state = running.get(run_id)
        if state and state.get("status") in ("pending", "running"):
            return {"run_id": run_id, "running": True, "status": state["status"]}
        if state and state.get("status") == "error":
            return {
                "run_id": run_id,
                "running": False,
                "status": "error",
                "error": state.get("error", ""),
            }
        rec = _load_run(run_id)
        return {**rec, "running": False}

    @app.get("/api/runs")
    def list_run_history(
        limit: int = Query(20, ge=1, le=500),
        offset: int = Query(0, ge=0),
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        from agent_eval.costing import pricing_for

        runs, total = store.list_runs(
            limit=limit, offset=offset, task_id=task_id, agent_id=agent_id, status=status
        )
        price = pricing_for()
        for r in runs:
            # 实际成本：从 run.json 的 metrics.usage 读取（无 usage 时为 None）
            r["actual_cost_cny"] = None
            r["tokens"] = None
            r["confidence_score"] = None
            r["confidence_level"] = None
            p = results_dir / r["run_id"] / "run.json"
            if p.exists():
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    usage = (d.get("metrics") or {}).get("usage") or {}
                    pt = usage.get("prompt_tokens") or 0
                    ct = usage.get("completion_tokens") or 0
                    if pt or ct:
                        r["actual_cost_cny"] = round(
                            pt / 1e6 * price["input_cny_per_m"]
                            + ct / 1e6 * price["output_cny_per_m"],
                            4,
                        )
                        r["tokens"] = {"prompt_tokens": pt, "completion_tokens": ct}
                    # 置信度（V3.0）
                    conf = (d.get("metrics") or {}).get("confidence") or {}
                    if conf:
                        r["confidence_score"] = conf.get("score")
                        r["confidence_level"] = conf.get("level")
                except Exception:  # noqa: BLE001
                    pass
        return {"runs": runs, "total": total, "limit": limit, "offset": offset}

    # ---------- 运行产物 ----------
    @app.get("/api/runs/{run_id}/files")
    def list_run_files(run_id: str) -> dict:
        ws = _workspace(run_id)
        return {"files": _walk_files(ws)}

    @app.get("/api/runs/{run_id}/file")
    def read_run_file(run_id: str, path: str = Query(...)) -> dict:
        ws = _workspace(run_id)
        ws_abs = ws.resolve()
        target = (ws / path).resolve()
        try:
            rel = target.relative_to(ws_abs)
        except ValueError:
            raise HTTPException(status_code=400, detail="路径越界")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if target.suffix.lower() not in _READABLE_EXTS:
            raise HTTPException(status_code=400, detail=f"不支持预览该类型: {target.suffix}")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail="文件过大，仅支持预览 <=200KB")
        return {
            "path": str(rel).replace("\\", "/"),
            "name": target.name,
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }

    # ---------- 多 Agent 对比批次 / 矩阵（V2.7） ----------
    @app.get("/api/license")
    def api_license() -> dict:
        return license_mod.get_entitlements(refresh=True)

    @app.post("/api/batches")
    def create_batch(payload: dict = Body(...)) -> dict:
        agents = [str(a) for a in payload.get("agents", []) if a]
        if not agents:
            raise HTTPException(status_code=400, detail="请至少选择一个 Agent")
        # 去重且保持顺序
        seen: set[str] = set()
        agents = [a for a in agents if not (a in seen or seen.add(a))]
        valid = list_backends()
        bad = [a for a in agents if a not in valid]
        if bad:
            raise HTTPException(status_code=400, detail=f"未知后端: {bad}（可用 {valid}）")
        # license 档位卡口：社区版限制对比 Agent 数
        ok, reason = license_mod.can(len(agents))
        if not ok:
            raise HTTPException(status_code=403, detail=reason)

        scope = str(payload.get("scope", "core"))
        task_ids = _resolve_scope_tasks(scope, payload.get("task_ids") or None)
        if not task_ids:
            raise HTTPException(status_code=400, detail="任务集为空")
        runs = max(1, min(int(payload.get("runs", 1)), 10))
        model = str(payload.get("model", "deepseek-chat"))
        label = str(payload.get("label", "")).strip() or f"{scope} × {len(agents)}agent × runs{runs}"

        batch_id = uuid.uuid4().hex[:12]
        total = len(agents) * len(task_ids) * runs
        store.insert_batch(
            {
                "batch_id": batch_id,
                "label": label,
                "agents": agents,
                "task_ids": task_ids,
                "scope": scope,
                "runs": runs,
                "status": "running",
                "total_runs": total,
                "done_runs": 0,
                "summary": {},
            }
        )
        t = threading.Thread(
            target=_execute_batch,
            args=(batch_id, agents, task_ids, runs, model),
            daemon=True,
        )
        t.start()
        logger.info("对比批次已创建 | batch=%s label=%s total=%d", batch_id, label, total)
        return {"batch_id": batch_id, "total_runs": total}

    @app.get("/api/batches")
    def list_batches() -> dict:
        rows = store.list_batches(100)
        # 列表不带大 summary，只给元信息与进度
        for r in rows:
            r.pop("summary", None)
        return {"batches": rows}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        b = store.get_batch(batch_id)
        if not b:
            raise HTTPException(status_code=404, detail="批次不存在")
        # running 时实时聚合已完成部分，done 时用落库 summary
        if b["status"] != "done":
            b["summary"] = _build_matrix(b)
        return b

    @app.get("/api/matrix")
    def get_matrix(batch_id: str = Query(...)) -> dict:
        b = store.get_batch(batch_id)
        if not b:
            raise HTTPException(status_code=404, detail="批次不存在")
        if b["status"] == "done" and b.get("summary"):
            return b["summary"]
        return _build_matrix(b)

    @app.get("/api/matrix/export")
    def export_matrix(batch_id: str = Query(...)) -> Response:
        ent = license_mod.get_entitlements()
        if not ent.get("export_csv"):
            raise HTTPException(status_code=403, detail="CSV 导出为 Pro 功能，导入 License 后解锁")
        b = store.get_batch(batch_id)
        if not b:
            raise HTTPException(status_code=404, detail="批次不存在")
        m = b["summary"] if b["status"] == "done" and b.get("summary") else _build_matrix(b)
        buf = io.StringIO()
        buf.write("﻿")  # UTF-8 BOM，Excel 打开中文不乱码
        w = csv.writer(buf)
        agents, tasks, cells, totals = m["agents"], m["tasks"], m["cells"], m["totals"]
        header = ["Agent"] + tasks + ["加权总分", "任务通过率", "总成本(元)", "总耗时(s)", "平均波动σ"]
        w.writerow(header)
        for a in agents:
            row = [a]
            for t in tasks:
                c = cells.get(f"{a}|{t}")
                row.append(
                    f"{c['best']} (mean {c['mean']}, σ {c['std']}, {round(c['pass_rate']*100)}%)"
                    if c and c["n"] else "-"
                )
            tt = totals.get(a, {})
            row += [
                tt.get("weighted_score", 0),
                f"{round(tt.get('task_pass_rate', 0)*100)}%",
                tt.get("cost_cny", 0),
                tt.get("duration_s", 0),
                tt.get("avg_std", 0),
            ]
            w.writerow(row)
        fname = f"matrix-{batch_id}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # ---------- 汇总 / 报告 ----------
    @app.get("/api/summary")
    def api_summary() -> dict:
        return summarize(load_runs(results_dir))

    @app.post("/api/report")
    def api_report(out_name: str = Body("report.html", embed=True)) -> dict:
        report_dir.mkdir(parents=True, exist_ok=True)
        name = Path(out_name).name  # 防路径穿越
        out = report_dir / name
        runs = load_runs(results_dir)
        s = summarize(runs)
        out.write_text(
            render_html(s, datetime.now().strftime("%Y-%m-%d %H:%M")), encoding="utf-8"
        )
        return {"path": str(out), "url": f"/reports/{name}"}

    @app.get("/reports/{name}")
    def get_report(name: str) -> FileResponse:
        target = (report_dir / name).resolve()
        try:
            target.relative_to(report_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="路径越界")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="报告不存在")
        return FileResponse(target)

    # ---------- 任务生成 ----------
    @app.post("/api/tasks/generate")
    def api_generate_task(payload: dict = Body(...)) -> dict:
        try:
            return generate_task_pack(tasks_dir, payload)
        except (ValueError, FileExistsError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ---------- 任务包市场（M3 Web 集成） ----------
    @app.get("/api/packages")
    def api_list_packages() -> dict:
        """列出已安装的所有任务包。"""
        from agent_eval.taskpack import list_packages
        packages = list_packages()
        return {
            "packages": [
                {
                    "name": p.name,
                    "version": p.version,
                    "author": p.author,
                    "description": p.description,
                    "license": p.license,
                    "task_count": len(p.tasks),
                    "tasks": p.tasks,
                    "install_path": str(p.install_path) if p.install_path else None,
                }
                for p in packages
            ],
            "total": len(packages),
        }

    @app.get("/api/packages/{name}")
    def api_get_package(name: str) -> dict:
        """查看任务包详情。"""
        from agent_eval.taskpack import get_package
        pkg = get_package(name)
        if pkg is None:
            raise HTTPException(status_code=404, detail=f"任务包不存在: {name}")
        return {
            "name": pkg.name,
            "version": pkg.version,
            "author": pkg.author,
            "description": pkg.description,
            "license": pkg.license,
            "task_count": len(pkg.tasks),
            "tasks": pkg.tasks,
            "install_path": str(pkg.install_path) if pkg.install_path else None,
        }

    @app.post("/api/packages/install")
    def api_install_package(payload: dict = Body(...)) -> dict:
        """安装任务包（从 git URL 或本地目录）。"""
        from agent_eval.taskpack import install_package
        source = payload.get("source", "")
        name = payload.get("name") or None
        if not source:
            raise HTTPException(status_code=400, detail="缺少 source 参数")
        try:
            pkg = install_package(source, name=name)
            return {
                "status": "installed",
                "name": pkg.name,
                "version": pkg.version,
                "task_count": len(pkg.tasks),
                "message": f"任务包 {pkg.name} v{pkg.version} 安装成功（{len(pkg.tasks)} 个任务）",
            }
        except (FileNotFoundError, RuntimeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/packages/{name}")
    def api_remove_package(name: str) -> dict:
        """卸载任务包。"""
        from agent_eval.taskpack import remove_package
        if remove_package(name):
            return {"status": "removed", "name": name, "message": f"任务包 {name} 已卸载"}
        raise HTTPException(status_code=404, detail=f"任务包不存在: {name}")

    # ---------- Badcase 管理（V2.8 评测 badcase 积累） ----------
    @app.get("/api/badcases")
    def api_list_badcases(
        task_id: str = Query(None),
        agent_id: str = Query(None),
        category: str = Query(None),
        severity: str = Query(None),
        status: str = Query(None),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ) -> dict:
        """分页列出 badcase，支持按任务/Agent/分类/严重程度/状态筛选。"""
        offset = (page - 1) * page_size
        items, total = store.list_badcases(
            limit=page_size, offset=offset,
            task_id=task_id, agent_id=agent_id,
            category=category, severity=severity, status=status,
        )
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "categories": store.BADCASE_CATEGORIES,
            "severities": store.BADCASE_SEVERITIES,
            "statuses": store.BADCASE_STATUSES,
            "stats": store.badcase_stats(),
        }

    @app.get("/api/badcases/{bid}")
    def api_get_badcase(bid: str) -> dict:
        """查看 badcase 详情。"""
        b = store.get_badcase(bid)
        if not b:
            raise HTTPException(status_code=404, detail=f"badcase 不存在: {bid}")
        # 如果有关联的 run_id，附带运行记录摘要
        if b.get("run_id"):
            try:
                run_path = results_dir / b["run_id"] / "run.json"
                if run_path.exists():
                    run_data = json.loads(run_path.read_text(encoding="utf-8"))
                    b["run_summary"] = {
                        "status": run_data.get("status"),
                        "score": run_data.get("metrics", {}).get("score"),
                        "pass_rate": run_data.get("metrics", {}).get("pass_rate"),
                        "duration_s": run_data.get("duration_s"),
                        "steps": len(run_data.get("steps", [])),
                    }
            except Exception:  # noqa: BLE001
                pass
        return b

    @app.post("/api/badcases")
    def api_create_badcase(payload: dict = Body(...)) -> dict:
        """手动创建 badcase。"""
        required = ["title"]
        for f in required:
            if not payload.get(f):
                raise HTTPException(status_code=400, detail=f"缺少必填字段: {f}")
        bid = store.insert_badcase(payload)
        return {"id": bid, "message": "badcase 已创建"}

    @app.put("/api/badcases/{bid}")
    def api_update_badcase(bid: str, payload: dict = Body(...)) -> dict:
        """更新 badcase（分类/严重程度/状态/根因/修复方案等）。"""
        if not store.get_badcase(bid):
            raise HTTPException(status_code=404, detail=f"badcase 不存在: {bid}")
        # 允许更新的字段
        allowed = {"title", "description", "category", "severity", "status",
                   "root_cause", "fix_plan", "tags", "run_id", "task_id", "agent_id"}
        update_fields = {k: v for k, v in payload.items() if k in allowed}
        if update_fields:
            store.update_badcase(bid, **update_fields)
        return {"id": bid, "message": "badcase 已更新", "updated": list(update_fields.keys())}

    @app.delete("/api/badcases/{bid}")
    def api_delete_badcase(bid: str) -> dict:
        """删除 badcase。"""
        if not store.get_badcase(bid):
            raise HTTPException(status_code=404, detail=f"badcase 不存在: {bid}")
        store.delete_badcase(bid)
        return {"id": bid, "message": "badcase 已删除"}

    @app.post("/api/badcases/import-from-run")
    def api_import_badcase_from_run(payload: dict = Body(...)) -> dict:
        """从运行记录导入 badcase：自动识别未通过的任务并创建 badcase。

        请求体: {"run_id": "xxx", "category": "reasoning", "severity": "P1", "title": "可选标题"}
        """
        run_id = str(payload.get("run_id", ""))
        if not run_id:
            raise HTTPException(status_code=400, detail="缺少 run_id")
        run_path = results_dir / run_id / "run.json"
        if not run_path.exists():
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        try:
            run_data = json.loads(run_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"运行记录解析失败: {e}")

        # 自动判断 badcase 分类
        status = run_data.get("status", "")
        verdicts = run_data.get("verdicts", [])
        failed_checkpoints = [v for v in verdicts if not v.get("passed", True)]

        category = payload.get("category", "other")
        if status == "timeout":
            category = "timeout"
        elif status == "error":
            category = "crash"
        elif failed_checkpoints:
            # 根据失败的 checkpoint 类型推断分类
            for v in failed_checkpoints:
                ctype = v.get("checkpoint_type", "")
                if ctype in ("content_contains", "content_not_contains"):
                    category = "reasoning"
                    break
                elif ctype == "cmd_exit_zero":
                    category = "tool_use"
                    break

        severity = payload.get("severity", "P2")
        if status in ("error", "timeout"):
            severity = "P0"
        elif failed_checkpoints and len(failed_checkpoints) >= len(verdicts) * 0.5:
            severity = "P1"

        title = payload.get("title") or f"{run_data.get('task_id', '?')} × {run_data.get('agent_id', '?')} 未通过"
        description = payload.get("description", "")
        if not description:
            parts = [f"运行状态: {status}"]
            if failed_checkpoints:
                parts.append(f"失败校验点: {', '.join(v.get('checkpoint_id', '?') for v in failed_checkpoints)}")
            if run_data.get("error"):
                parts.append(f"错误信息: {run_data['error'][:200]}")
            description = "\n".join(parts)

        # 自动归因：生成根因分析和修复建议
        diagnosis = _auto_diagnose_run(run_data, failed_checkpoints, category)
        root_cause = payload.get("root_cause", diagnosis["root_cause"])
        fix_plan = payload.get("fix_plan", diagnosis["fix_plan"])

        bid = store.insert_badcase({
            "run_id": run_id,
            "task_id": run_data.get("task_id", ""),
            "agent_id": run_data.get("agent_id", ""),
            "title": title,
            "description": description,
            "category": category,
            "severity": severity,
            "status": "pending",
            "root_cause": root_cause,
            "fix_plan": fix_plan,
        })
        return {"id": bid, "message": "已从运行记录导入 badcase", "category": category, "severity": severity,
                "root_cause": root_cause, "fix_plan": fix_plan}

    @app.post("/api/badcases/{bid}/convert-to-task")
    def api_convert_badcase_to_task(bid: str, payload: dict = Body(...)) -> dict:
        """将 badcase 转化为回归评测用例：生成 spec.yaml 并更新 badcase 关联。

        请求体: {"new_task_id": "T-REG-001", "title": "...", "description": "...", "add_to_manifest": true}
        """
        b = store.get_badcase(bid)
        if not b:
            raise HTTPException(status_code=404, detail=f"badcase 不存在: {bid}")

        new_task_id = str(payload.get("new_task_id", "")).strip()
        if not new_task_id:
            raise HTTPException(status_code=400, detail="缺少 new_task_id")

        # 检查任务 ID 是否已存在
        task_dir = tasks_dir / new_task_id
        if task_dir.exists():
            raise HTTPException(status_code=400, detail=f"任务 ID 已存在: {new_task_id}")

        title = payload.get("title") or f"[回归] {b.get('title', '')}"
        description = payload.get("description") or _build_regression_description(b)
        add_to_manifest = bool(payload.get("add_to_manifest", True))

        # 生成 spec.yaml
        import yaml
        task_dir.mkdir(parents=True, exist_ok=True)
        spec = {
            "id": new_task_id,
            "title": title,
            "level": "L2",
            "description": description,
            "tags": ["regression", b.get("category", "other")],
            "fixtures": {"source": "fixtures/"},
            "ground_truth": {
                "checkpoints": [
                    {
                        "id": "c1",
                        "type": "file_exists",
                        "path": "output/result.md",
                        "desc": "Agent 必须输出结果文件"
                    }
                ]
            },
            "verifier": "deterministic",
            "weight": 1.0,
            "cost_budget_usd": 0.2,
            "timeout_s": 300,
            "capabilities": ["tool_use", "reasoning"],
        }
        spec_path = task_dir / "spec.yaml"
        with open(spec_path, "w", encoding="utf-8") as f:
            yaml.dump(spec, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        # 创建 fixtures 目录（占位）
        fixtures_dir = task_dir / "fixtures"
        fixtures_dir.mkdir(exist_ok=True)
        (fixtures_dir / ".gitkeep").touch()

        # 可选：加入 manifest.yaml
        manifest_updated = False
        if add_to_manifest:
            manifest_path = tasks_dir / "manifest.yaml"
            if manifest_path.exists():
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = yaml.safe_load(f)
                    tasks_list = manifest.get("tasks", [])
                    if new_task_id not in tasks_list:
                        tasks_list.append(new_task_id)
                        manifest["tasks"] = tasks_list
                        with open(manifest_path, "w", encoding="utf-8") as f:
                            yaml.dump(manifest, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                        manifest_updated = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("更新 manifest.yaml 失败: %s", e)

        # 更新 badcase：记录 regression_task_id，状态改为 fixed（如果还是 pending）
        update_fields = {"regression_task_id": new_task_id}
        if b.get("status") == "pending":
            update_fields["status"] = "fixed"
        store.update_badcase(bid, **update_fields)

        return {
            "message": "badcase 已转化为回归评测用例",
            "badcase_id": bid,
            "new_task_id": new_task_id,
            "spec_path": str(spec_path),
            "manifest_updated": manifest_updated,
        }

    @app.get("/api/regression-badcases")
    def api_list_regression_badcases() -> dict:
        """获取所有已转化为回归评测用例的 badcase 列表。"""
        items = store.list_regression_badcases()
        return {"items": items, "total": len(items)}

    # ---------- 经验记忆（V2.9） ----------
    @app.get("/api/memories")
    def api_list_memories(
        page: int = 1, page_size: int = 20,
        status: str = "", keyword: str = "", task_tag: str = "",
    ) -> dict:
        """记忆列表（分页+筛选）。"""
        return store.list_memories(page=page, page_size=page_size,
                                    status=status, keyword=keyword, task_tag=task_tag)

    @app.get("/api/memories/{mid}")
    def api_get_memory(mid: str) -> dict:
        """记忆详情。"""
        m = store.get_memory(mid)
        if not m:
            raise HTTPException(status_code=404, detail=f"记忆不存在: {mid}")
        return m

    @app.post("/api/memories")
    def api_create_memory(payload: dict = Body(...)) -> dict:
        """创建记忆。"""
        mid = store.insert_memory(payload)
        return {"id": mid, "message": "记忆已创建"}

    @app.put("/api/memories/{mid}")
    def api_update_memory(mid: str, payload: dict = Body(...)) -> dict:
        """更新记忆。"""
        ok = store.update_memory(mid, **payload)
        if not ok:
            raise HTTPException(status_code=404, detail=f"记忆不存在: {mid}")
        return {"id": mid, "message": "记忆已更新"}

    @app.delete("/api/memories/{mid}")
    def api_delete_memory(mid: str) -> dict:
        """删除记忆。"""
        ok = store.delete_memory(mid)
        if not ok:
            raise HTTPException(status_code=404, detail=f"记忆不存在: {mid}")
        return {"id": mid, "message": "记忆已删除"}

    @app.post("/api/memories/from-badcase")
    def api_create_memory_from_badcase(payload: dict = Body(...)) -> dict:
        """从 badcase 转化为经验记忆。

        请求体: {"badcase_id": "xxx", "title": "可选", "content": "可选",
                 "trigger_keywords": ["关键词1"], "task_tags": ["file"], "confidence": 0.8}
        """
        bid = str(payload.get("badcase_id", ""))
        if not bid:
            raise HTTPException(status_code=400, detail="缺少 badcase_id")
        b = store.get_badcase(bid)
        if not b:
            raise HTTPException(status_code=404, detail=f"badcase 不存在: {bid}")

        # 从 badcase 生成记忆内容
        title = payload.get("title") or f"[经验] {b.get('title', '')}"
        content = payload.get("content")
        if not content:
            parts = [f"【经验来源】badcase {bid} - {b.get('title', '')}"]
            parts.append(f"【问题场景】任务 {b.get('task_id', '')} × 后端 {b.get('agent_id', '')}")
            if b.get("description"):
                parts.append(f"【问题描述】{b['description']}")
            if b.get("root_cause"):
                parts.append(f"【根因分析】{b['root_cause']}")
            if b.get("fix_plan"):
                parts.append(f"【修复方案】{b['fix_plan']}")
            parts.append("【适用场景】遇到类似问题时，参考此经验避免重复犯错。")
            content = "\n".join(parts)

        trigger_keywords = payload.get("trigger_keywords", [])
        if not trigger_keywords:
            # 从任务 ID 和标题提取关键词
            kw = [b.get("task_id", ""), b.get("category", "")]
            trigger_keywords = [k for k in kw if k]

        task_tags = payload.get("task_tags", [])
        confidence = float(payload.get("confidence", 0.7))

        mid = store.insert_memory({
            "title": title,
            "content": content,
            "trigger_keywords": trigger_keywords,
            "task_tags": task_tags,
            "source_badcase_id": bid,
            "confidence": confidence,
            "status": "active",
        })

        # 更新 badcase 状态为 fixed（如果还是 pending）
        if b.get("status") == "pending":
            store.update_badcase(bid, status="fixed")

        return {"id": mid, "message": "已从 badcase 转化为经验记忆", "badcase_id": bid}

    @app.post("/api/memories/recall")
    def api_recall_memories(payload: dict = Body(...)) -> dict:
        """召回记忆：根据任务特征返回最相关的记忆列表（供运行时注入使用）。

        请求体: {"task_tags": ["file", "text"], "keywords": ["日期", "格式"], "limit": 5}
        """
        task_tags = payload.get("task_tags", [])
        keywords = payload.get("keywords", [])
        limit = int(payload.get("limit", 5))
        items = store.recall_memories(task_tags=task_tags, keywords=keywords, limit=limit)
        return {"items": items, "total": len(items)}

    @app.post("/api/memory-auto-manage")
    def api_auto_manage_memories(payload: dict = Body(...)) -> dict:
        """V2.9.1：记忆质量自动评估与主动遗忘。

        治理策略：
        1. 低成功率自动停用：usage_count >= 3 且 success_rate < 30% → inactive
        2. 高成功率自动提升：usage_count >= 5 且 success_rate >= 80% → confidence=0.9
        3. 长期未使用降级：created_at > 30天 且 usage_count == 0 → confidence=0.3

        请求体: {"min_usage": 3, "low_success": 0.3, "high_success": 0.8, "unused_days": 30}
        """
        result = store.auto_manage_memories(
            min_usage_for_eval=int(payload.get("min_usage", 3)),
            low_success_threshold=float(payload.get("low_success", 0.3)),
            high_success_threshold=float(payload.get("high_success", 0.8)),
            unused_days=int(payload.get("unused_days", 30)),
        )
        return result

    @app.get("/api/memory-quality-stats")
    def api_memory_quality_stats() -> dict:
        """获取记忆质量统计，用于前端展示治理效果。"""
        return store.get_memory_quality_stats()

    # ---------- 静态页 ----------
    @app.middleware("http")
    async def _request_logging(request, call_next):
        import time as _time
        start = _time.time()
        response = await call_next(request)
        duration_ms = int((_time.time() - start) * 1000)
        path = request.url.path
        # 静态资源和轮询接口降级为 debug，避免日志刷屏
        if path.startswith("/static/") or path == "/api/runs" and request.method == "GET":
            logger.debug("%s %s -> %s (%dms)", request.method, path, response.status_code, duration_ms)
        else:
            logger.info("%s %s -> %s (%dms)", request.method, path, response.status_code, duration_ms)
        # 本地评测工作台：静态资源不缓存，前端改动即时生效
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        return response

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> HTMLResponse:
        # 静态资源 URL 注入文件 mtime 版本号：前端改动后浏览器强制拉新，不再受缓存干扰
        try:
            js_v = int((_STATIC_DIR / "app.js").stat().st_mtime)
            css_v = int((_STATIC_DIR / "style.css").stat().st_mtime)
        except OSError:
            js_v = css_v = 0
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        html = (
            html.replace("/static/app.js", f"/static/app.js?v={js_v}")
            .replace("/static/style.css", f"/static/style.css?v={css_v}")
        )
        return HTMLResponse(html)

    return app


def _task_to_dict(t) -> dict:
    d = asdict(t)
    d["spec_path"] = str(t.spec_path) if t.spec_path else None
    return d


app = create_app()
