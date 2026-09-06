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

import json
import threading
import uuid
from dataclasses import asdict
from datetime import datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent_eval.backends import _BACKENDS, list_backends
from agent_eval.reporter import load_runs, render_html, summarize
from agent_eval.runner import default_results_dir, run_one
from agent_eval.spec import find_tasks_dir, load_task_pack
from agent_eval.web.store import RunStore
from agent_eval.web.taskgen import generate_task_pack

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_READABLE_EXTS = {
    ".md", ".txt", ".csv", ".json", ".log", ".py", ".yaml", ".yml", ".xml", ".html", ".svg",
}
_MAX_FILE_BYTES = 200 * 1024


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
    tasks_dir = Path(tasks_dir) if tasks_dir else find_tasks_dir()
    results_dir = Path(results_dir) if results_dir else default_results_dir()
    report_dir = Path(report_dir) if report_dir else Path("reports")

    app = FastAPI(title="AI Agent 评测工作台", version=_app_version())

    db_path = db_path if db_path is not None else results_dir.parent / "run_history.db"
    store = RunStore(db_path)
    store.rebuild(results_dir)

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
        return json.loads(p.read_text(encoding="utf-8"))

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
            for rid in run_ids:
                running[rid] = {"status": "error", "error": f"未知任务: {task_id}"}
            return
        for rid in run_ids:
            running[rid] = {"status": "running", "task_id": task_id, "agent_id": agent_id}
            try:
                rec = run_one(
                    task,
                    agent_id,
                    config=config,
                    results_dir=results_dir,
                    run_id=rid,
                )
                store.insert_run(rec.to_dict())
            except Exception as e:  # noqa: BLE001 - 单个 run 失败不中断整批
                running[rid] = {
                    "status": "error",
                    "error": f"{type(e).__name__}: {e}",
                    "task_id": task_id,
                    "agent_id": agent_id,
                }
                continue
            running.pop(rid, None)

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
                {"id": n, "version": getattr(cls, "version", "dev")}
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
        target = (ws / path).resolve()
        try:
            target.relative_to(ws.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="路径越界")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if target.suffix.lower() not in _READABLE_EXTS:
            raise HTTPException(status_code=400, detail=f"不支持预览该类型: {target.suffix}")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail="文件过大，仅支持预览 <=200KB")
        return {
            "path": str(target.relative_to(ws)).replace("\\", "/"),
            "name": target.name,
            "content": target.read_text(encoding="utf-8", errors="replace"),
        }

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
    async def _no_cache_static(request, call_next):
        # 本地评测工作台：静态资源不缓存，前端改动即时生效
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
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
