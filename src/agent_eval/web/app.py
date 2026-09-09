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
from agent_eval.spec import find_tasks_dir, load_task_pack
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

        return {
            "agents": agents,
            "tasks": task_ids,
            "cells": cells,
            "totals": totals,
            "conclusion": _matrix_conclusion(totals),
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
        return {"tasks": out}

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
