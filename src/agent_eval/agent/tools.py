"""评测 Agent 工具封装。

把现有评测平台的能力包装成 Agent 可调用的工具：
- list_tasks: 列出任务（可按标签/难度筛选）
- list_backends: 列出可评测的 Agent
- run_eval: 发起单次评测
- run_batch: 发起批量评测
- get_batch_status: 查询批次状态
- compare_agents: 多 Agent 对比
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from agent_eval.log import get_logger
from agent_eval.spec import load_task_pack, find_tasks_dir

logger = get_logger(__name__)


def _serialize(obj: Any) -> Any:
    """将 dataclass / Path 等对象序列化为 JSON 可序列化结构。"""
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


# ========== 工具定义 ==========

TOOLS_SCHEMA = [
    {
        "name": "list_tasks",
        "description": "列出评测平台上的所有任务，可按标签（如 file/data/rag）或难度（L1-L5）筛选。返回任务 ID、标题、难度、标签。",
        "parameters": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "按标签筛选，如 file/data/rag/security"},
                "level": {"type": "string", "description": "按难度筛选，如 L1/L2/L3/L4/L5"},
            },
        },
    },
    {
        "name": "list_backends",
        "description": "列出所有可评测的 Agent 后端，如 minimal-react、claude-code、deepseek-harness 等。返回 Agent ID、名称、模型。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "run_eval",
        "description": "对单个任务执行一次评测。返回运行 ID、通过率、得分、耗时。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID，如 T001/T204"},
                "agent_id": {"type": "string", "description": "Agent 后端 ID"},
            },
            "required": ["task_id", "agent_id"],
        },
    },
    {
        "name": "run_batch",
        "description": "批量执行多个任务 × 多个 Agent 的评测组合。返回批次 ID，用于后续查询状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {"type": "array", "items": {"type": "string"}, "description": "任务 ID 列表"},
                "agents": {"type": "array", "items": {"type": "string"}, "description": "Agent ID 列表"},
                "runs": {"type": "integer", "description": "每个组合跑几次，默认 1", "default": 1},
            },
            "required": ["tasks", "agents"],
        },
    },
    {
        "name": "get_batch_status",
        "description": "查询批次运行状态。返回已完成数、总数、通过率、失败任务列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "批次 ID"},
            },
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_run_detail",
        "description": "查询单次运行详情，包括步骤轨迹、校验点结果、产物文件、成本。",
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "运行 ID"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "list_badcases",
        "description": "列出平台上的 badcase 库，可按状态筛选（open/closed/fixed）。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "按状态筛选：open/closed/fixed"},
            },
        },
    },
    {
        "name": "get_summary",
        "description": "获取平台总体统计：总任务数、总运行数、平均通过率、成本统计。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_matrix",
        "description": "获取多 Agent 对比矩阵数据：各 Agent 在各任务上的得分、通过率、成本。",
        "parameters": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "可选，指定批次 ID，不传用最新"},
            },
        },
    },
    {
        "name": "cancel_batch",
        "description": "取消正在运行的批次。",
        "parameters": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "批次 ID"},
            },
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_costs",
        "description": "获取成本统计：各模型、各 Agent 的 token 消耗和成本汇总。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
]


# ========== 工具实现 ==========

def tool_list_tasks(tag: str | None = None, level: str | None = None) -> dict:
    """实现 list_tasks 工具。"""
    tasks_dir = find_tasks_dir()
    tasks = load_task_pack(tasks_dir)

    if tag:
        tasks = [t for t in tasks if tag in (t.tags or [])]
    if level:
        tasks = [t for t in tasks if t.level == level]

    result = []
    for t in tasks[:20]:  # 最多返回 20 个，避免太长
        info = {}; result.append({
            "id": t.id,
            "title": t.title,
            "level": t.level,
            "tags": t.tags,
            "tier": getattr(t, 'tier', ''),
        })

    return {
        "count": len(tasks),
        "tasks": result,
        "truncated": len(tasks) > 20,
    }


def tool_list_backends() -> dict:
    """实现 list_backends 工具。"""
    from agent_eval.backends import list_backends

    backend_names = list_backends()
    result = []
    for bid in backend_names:
        info = {}; result.append({
            "id": bid,
            "name": info.get("name", bid),
            "model": info.get("model", "unknown"),
            "description": info.get("description", ""),
        })

    return {"count": len(result), "backends": result}


def tool_run_eval(task_id: str, agent_id: str) -> dict:
    """实现 run_eval 工具。"""
    from agent_eval.runner import run_one
    from agent_eval.spec import load_task

    task = load_task(task_id)
    if task is None:
        return {"error": f"任务 {task_id} 不存在"}

    record = run_one(task, agent_id, keep_workspace=False)
    return {
        "run_id": record.run_id,
        "task_id": record.task_id,
        "agent_id": record.agent_id,
        "passed": record.passed,
        "score": record.score,
        "duration_s": record.duration_s,
        "cost_usd": record.cost_usd,
    }


def tool_run_batch(tasks: list[str], agents: list[str], runs: int = 1) -> dict:
    """实现 run_batch 工具。"""
    import uuid
    from agent_eval.web.store import RunStore

    store = RunStore()
    batch_id = uuid.uuid4().hex[:12]

    # 创建批次（实际执行在后台异步进行）
    store.create_batch(batch_id, tasks, agents, runs=runs)

    return {
        "batch_id": batch_id,
        "total_runs": len(tasks) * len(agents) * runs,
        "tasks": tasks,
        "agents": agents,
        "runs": runs,
        "status": "created",
    }


def tool_get_batch_status(batch_id: str) -> dict:
    """实现 get_batch_status 工具。"""
    from agent_eval.web.store import RunStore

    store = RunStore()
    batch = store.get_batch(batch_id)
    if batch is None:
        return {"error": f"批次 {batch_id} 不存在"}

    return {
        "batch_id": batch_id,
        "status": batch.status,
        "total_runs": batch.total_runs,
        "done_runs": batch.done_runs,
        "passed": batch.passed,
        "failed": batch.failed,
        "pass_rate": batch.pass_rate,
    }


def tool_get_run_detail(run_id: str) -> dict:
    """实现 get_run_detail 工具。"""
    from agent_eval.web.store import RunStore

    store = RunStore()
    record = store.get_run(run_id)
    if record is None:
        return {"error": f"运行 {run_id} 不存在"}

    return {
        "run_id": record.run_id,
        "task_id": record.task_id,
        "agent_id": record.agent_id,
        "passed": record.passed,
        "score": record.score,
        "duration_s": record.duration_s,
        "cost_usd": record.cost_usd,
        "error": record.error,
    }


def tool_list_badcases(status: str | None = None) -> dict:
    """实现 list_badcases 工具。"""
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8000/api/badcases", timeout=5)
        data = r.json()
        items = data.get("items", data) if isinstance(data, dict) else data
        if status and isinstance(items, list):
            items = [b for b in items if b.get("status") == status]
        return {"count": len(items) if isinstance(items, list) else 0, "items": items[:20]}
    except Exception as e:
        return {"error": str(e)}


def tool_get_summary() -> dict:
    """实现 get_summary 工具。"""
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8000/api/summary", timeout=5)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def tool_get_matrix(batch_id: str | None = None) -> dict:
    """实现 get_matrix 工具。"""
    import httpx
    try:
        url = "http://127.0.0.1:8000/api/matrix"
        if batch_id:
            url += f"?batch_id={batch_id}"
        r = httpx.get(url, timeout=5)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def tool_cancel_batch(batch_id: str) -> dict:
    """实现 cancel_batch 工具。"""
    import httpx
    try:
        r = httpx.post(f"http://127.0.0.1:8000/api/batches/{batch_id}/cancel", timeout=5)
        return {"batch_id": batch_id, "status": "cancelled"}
    except Exception as e:
        return {"error": str(e)}


def tool_get_costs() -> dict:
    """实现 get_costs 工具。"""
    import httpx
    try:
        r = httpx.get("http://127.0.0.1:8000/api/costs", timeout=5)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ========== 工具分发 ==========

TOOL_MAP = {
    "list_tasks": tool_list_tasks,
    "list_backends": tool_list_backends,
    "run_eval": tool_run_eval,
    "run_batch": tool_run_batch,
    "get_batch_status": tool_get_batch_status,
    "get_run_detail": tool_get_run_detail,
    "list_badcases": tool_list_badcases,
    "get_summary": tool_get_summary,
    "get_matrix": tool_get_matrix,
    "cancel_batch": tool_cancel_batch,
    "get_costs": tool_get_costs,
}


def call_tool(name: str, args: dict) -> dict:
    """按名称调用工具，返回 JSON 可序列化结果。"""
    if name not in TOOL_MAP:
        return {"error": f"未知工具: {name}"}

    logger.info("评测 Agent 调用工具: %s args=%s", name, json.dumps(args, ensure_ascii=False))
    try:
        result = TOOL_MAP[name](**args)
        return _serialize(result)
    except Exception as e:
        logger.error("工具 %s 执行失败: %s", name, e)
        return {"error": f"工具执行失败: {type(e).__name__}: {e}"}
