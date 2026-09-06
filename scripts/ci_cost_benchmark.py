#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实测 minimal-react 全 21 任务的真实耗时与 LLM token 用量（CI 成本核算基准）。

输出：results/ci_cost_benchmark.json（按任务统计 + 汇总）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_eval.runner import run_one
from agent_eval.spec import find_tasks_dir, load_task_pack

RESULTS = Path("results")
OUT = RESULTS / "ci_cost_benchmark.json"

if __name__ == "__main__":
    tasks = load_task_pack(find_tasks_dir())
    rows = []
    grand = {"prompt_tokens": 0, "completion_tokens": 0, "duration_s": 0.0, "runs": 0}
    t0 = time.time()
    for t in sorted(tasks, key=lambda x: x.id):
        rec = run_one(t, "minimal-react", config={"agent": {"model": "deepseek-chat"}})
        u = rec.metrics.get("usage", {}) or {}
        pt = u.get("prompt_tokens", 0) or 0
        ct = u.get("completion_tokens", 0) or 0
        row = {
            "task": t.id,
            "level": t.level,
            "verifier": t.verifier,
            "status": rec.status,
            "duration_s": rec.duration_s,
            "score": rec.metrics.get("score", 0.0),
            "prompt_tokens": pt,
            "completion_tokens": ct,
        }
        rows.append(row)
        grand["prompt_tokens"] += pt
        grand["completion_tokens"] += ct
        grand["duration_s"] += rec.duration_s
        grand["runs"] += 1
        print(
            f"{t.id}  {rec.status:<10} {rec.duration_s:>7.1f}s  "
            f"in={pt:>6} out={ct:>5}  score={rec.metrics.get('score',0):.2f}"
        )
    grand["wall_s"] = round(time.time() - t0, 1)
    payload = {"rows": rows, "grand": grand}
    RESULTS.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总: {grand['runs']} 次执行, 墙钟 {grand['wall_s']}s, "
          f"prompt={grand['prompt_tokens']}, completion={grand['completion_tokens']}")
    print(f"已写入 {OUT}")
