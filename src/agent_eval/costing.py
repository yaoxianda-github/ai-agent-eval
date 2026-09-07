"""LLM 成本核算模块（V2.3）。

为 CLI 与 Web 工作台提供"任务预计成本"，让用户在发起评测前看到真实成本预期。

数据来源（按优先级）：
1. measured：聚合 results/runs/*/run.json 的 metrics.usage（每次评测自动落盘），
   按 (agent, task) 求 token 均值，写入 results/cost_benchmark.json；
2. estimate：无实测数据时，按任务级别默认 token 估算，明确标注 source=estimate。

定价：默认 deepseek-chat 官方口径（输入 ¥2/百万、输出 ¥3/百万，缓存未命中口径），
可用环境变量 LLM_INPUT_CNY_PER_M / LLM_OUTPUT_CNY_PER_M 覆盖；
DeepSeek 自 2026-08-17 起实行峰谷定价，实际以账单为准。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_RESULTS_DIR = Path("results") / "runs"
BENCHMARK_PATH = Path("results") / "cost_benchmark.json"

# 默认定价（每百万 token，人民币；官方 api-docs.deepseek.com deepseek-chat 口径）
# Anthropic 模型按官方美元定价 × 汇率 7.2 换算（Sonnet 4.5 $3/$15，Opus 4.5 $15/$75 per M）
DEFAULT_PRICING = {
    "deepseek-chat": {
        "input_cny_per_m": 2.0,
        "output_cny_per_m": 3.0,
    },
    "claude-sonnet-4-5": {
        "input_cny_per_m": 21.6,
        "output_cny_per_m": 108.0,
    },
    "claude-opus-4-5": {
        "input_cny_per_m": 108.0,
        "output_cny_per_m": 540.0,
    },
    "claude-opus-4-8": {
        # 暂按 Opus 4.5 同价（$15/$75 per M × 汇率7.2），官方定价待校准
        "input_cny_per_m": 108.0,
        "output_cny_per_m": 540.0,
    },
}

# 无实测数据时的分级估算（prompt, completion），基于 minimal-react 全量实测归纳
LEVEL_ESTIMATE: dict[str, tuple[int, int]] = {
    "L1": (3000, 400),
    "L2": (4500, 500),
    "L3": (7000, 700),
    "L4": (12000, 1400),
    "L5": (16000, 2600),  # 含 llm_judge 判分调用
}


def pricing_for(model: str = "deepseek-chat") -> dict:
    """读取模型单价；支持环境变量覆盖输入/输出单价。"""
    p = dict(DEFAULT_PRICING.get(model, {"input_cny_per_m": 2.0, "output_cny_per_m": 3.0}))
    try:
        p["input_cny_per_m"] = float(os.environ.get("LLM_INPUT_CNY_PER_M", p["input_cny_per_m"]))
        p["output_cny_per_m"] = float(os.environ.get("LLM_OUTPUT_CNY_PER_M", p["output_cny_per_m"]))
    except ValueError:
        pass
    return p


def build_benchmark(results_dir: Path | str = DEFAULT_RESULTS_DIR,
                    out: Path | str = BENCHMARK_PATH) -> dict:
    """扫描 results/runs/*/run.json，聚合 (agent, task) 的 token 均值。"""
    results_dir = Path(results_dir)
    agents: dict[str, dict[str, dict]] = {}
    if results_dir.is_dir():
        for run_json in sorted(results_dir.glob("*/run.json")):
            try:
                d = json.loads(run_json.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            usage = (d.get("metrics") or {}).get("usage") or {}
            pt = usage.get("prompt_tokens") or 0
            ct = usage.get("completion_tokens") or 0
            if not pt and not ct:
                continue
            agent_id = d.get("agent_id", "?")
            task_id = d.get("task_id", "?")
            cell = agents.setdefault(agent_id, {}).setdefault(task_id, {"runs": 0, "prompt_tokens": 0, "completion_tokens": 0})
            cell["runs"] += 1
            cell["prompt_tokens"] += pt
            cell["completion_tokens"] += ct
    # 转均值
    for agent_id, tasks in agents.items():
        for task_id, cell in tasks.items():
            n = max(1, cell["runs"])
            cell["prompt_tokens"] = round(cell["prompt_tokens"] / n)
            cell["completion_tokens"] = round(cell["completion_tokens"] / n)
    payload = {
        "generated_by": "agent_eval.costing.build_benchmark",
        "note": "token 为 (agent, task) 历史 run 均值；定价见 pricing_for()",
        "agents": agents,
    }
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def load_benchmark(path: Path | str = BENCHMARK_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"agents": {}}


def estimate_cost(
    agent_id: str,
    task_id: str,
    *,
    level: str = "L2",
    verifier: str = "deterministic",
    runs: int = 1,
    model: str = "deepseek-chat",
    benchmark: dict | None = None,
) -> dict:
    """估算一次评测（runs 次采样）的 LLM 成本。

    返回含 source：measured（实测基准均值）或 estimate（分级估算）。
    """
    bench = benchmark if benchmark is not None else load_benchmark()
    pricing = pricing_for(model)
    pin = pricing["input_cny_per_m"]
    pout = pricing["output_cny_per_m"]

    cell = (bench.get("agents") or {}).get(agent_id, {}).get(task_id)
    if cell and (cell.get("prompt_tokens") or cell.get("completion_tokens")):
        pt = int(cell["prompt_tokens"])
        ct = int(cell["completion_tokens"])
        source = "measured"
        note = f"历史 {cell.get('runs', 1)} 次 run 实测均值"
    else:
        est = LEVEL_ESTIMATE.get(level, LEVEL_ESTIMATE["L2"])
        pt, ct = est
        if verifier == "llm_judge":
            pt += 1500  # 判分调用的输入
            ct += 500
        source = "estimate"
        note = "无该 (agent, task) 实测数据，按级别估算"

    total_pt = pt * runs
    total_ct = ct * runs
    cost = round(total_pt / 1e6 * pin + total_ct / 1e6 * pout, 4)
    return {
        "agent": agent_id,
        "task": task_id,
        "runs": runs,
        "model": model,
        "input_price_cny_per_m": pin,
        "output_price_cny_per_m": pout,
        "prompt_tokens": total_pt,
        "completion_tokens": total_ct,
        "cost_cny": cost,
        "source": source,
        "note": note,
    }
