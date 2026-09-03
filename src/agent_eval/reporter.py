"""报告生成器（Day 5）：聚合 results/runs/*/run.json，输出自包含 HTML 报告。

报告内容：
- 概览统计（run 数 / 后端数 / 任务数 / 平均得分）
- 后端 × 任务得分矩阵（含 pass_rate）
- 每个后端汇总（总分、平均通过率、平均时长）
- 任务通过率条形图（纯 CSS，离线可用）
- 轨迹口径说明（白盒步骤级 / 黑盒单次调用）

生成文件为独立 HTML，可直接浏览器打开，用于演示与面试展示。
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

RESULTS_DIR = Path("results") / "runs"


def load_runs(results_dir: Path = RESULTS_DIR) -> list[dict]:
    runs: list[dict] = []
    if not results_dir.is_dir():
        return runs
    for p in sorted(results_dir.glob("*/run.json")):
        runs.append(json.loads(p.read_text(encoding="utf-8")))
    return runs


def summarize(runs: list[dict]) -> dict:
    """把 run 列表聚合成报告数据。"""
    agents = sorted({r.get("agent_id") for r in runs})
    tasks = sorted({r.get("task_id") for r in runs})

    # (agent, task) -> best score / best pass_rate
    best: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"score": -1.0, "pass_rate": -1.0, "status": "", "duration": 0.0}
    )
    # agent -> 汇总
    agent_agg: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "score_sum": 0.0, "pass_sum": 0.0, "duration_sum": 0.0, "status_ok": 0}
    )
    # task -> 通过率（取所有 run 平均）
    task_pass: dict[str, list[float]] = defaultdict(list)

    for r in runs:
        agent = r.get("agent_id", "?")
        task = r.get("task_id", "?")
        score = float(r.get("metrics", {}).get("score", 0.0))
        pr = float(r.get("metrics", {}).get("pass_rate", 0.0))
        dur = float(r.get("duration_s", 0.0))

        key = (agent, task)
        if score > best[key]["score"]:
            best[key]["score"] = score
            best[key]["pass_rate"] = pr
            best[key]["status"] = r.get("status", "")
            best[key]["duration"] = dur

        agg = agent_agg[agent]
        agg["runs"] += 1
        agg["score_sum"] += score
        agg["pass_sum"] += pr
        agg["duration_sum"] += dur
        if r.get("status") == "completed":
            agg["status_ok"] += 1

        task_pass[task].append(pr)

    # 每个后端的任务得分（按任务排序）
    agent_rows: dict[str, list[dict]] = {}
    for agent in agents:
        row = []
        for task in tasks:
            k = (agent, task)
            if k in best:
                row.append(
                    {
                        "task": task,
                        "score": best[k]["score"],
                        "pass_rate": best[k]["pass_rate"],
                        "status": best[k]["status"],
                        "duration": round(best[k]["duration"], 1),
                    }
                )
        agent_rows[agent] = row

    return {
        "agents": agents,
        "tasks": tasks,
        "best": {f"{a}|{t}": v for (a, t), v in best.items()},
        "agent_rows": agent_rows,
        "agent_agg": {a: dict(agent_agg[a]) for a in agents},
        "task_pass": {t: (sum(v) / len(v)) for t, v in task_pass.items()},
        "total_runs": len(runs),
    }


def _bar(rate: float) -> str:
    pct = max(2, round(rate * 100))
    color = "#52C41A" if rate >= 0.999 else ("#FAAD14" if rate >= 0.5 else "#EA6668")
    return (
        f'<div style="height:8px;border-radius:4px;background:rgba(0,0,0,0.06);width:100%;">'
        f'<div style="height:8px;border-radius:4px;width:{pct}%;background:{color};"></div></div>'
    )


def render_html(summary: dict, generated_at: str) -> str:
    e = html.escape
    agents = summary["agents"]
    tasks = summary["tasks"]

    # 概览卡
    total_score = 0.0
    total_pr = 0.0
    cnt = 0
    for a in agents:
        agg = summary["agent_agg"][a]
        total_score += agg["score_sum"]
        total_pr += agg["pass_sum"]
        cnt += agg["runs"]
    avg_score = round(total_score / cnt, 2) if cnt else 0.0
    avg_pr = round(total_pr / cnt * 100, 1) if cnt else 0.0

    # 得分矩阵
    table_rows = ""
    for a in agents:
        cells = ""
        for row in summary["agent_rows"][a]:
            pr = row["pass_rate"]
            cls = "ok" if pr >= 0.999 else ("half" if pr >= 0.5 else "bad")
            cells += f'<td class="{cls}">{row["score"]:g}</td>'
        table_rows += f"<tr><td class='ag'>{e(a)}</td>{cells}</tr>"

    header_cells = "".join(f"<th>{e(t)}</th>" for t in tasks)

    # 后端汇总卡
    agent_cards = ""
    for a in agents:
        agg = summary["agent_agg"][a]
        avg_dur = round(agg["duration_sum"] / agg["runs"], 1) if agg["runs"] else 0.0
        avg_a_pr = round(agg["pass_sum"] / agg["runs"] * 100, 1) if agg["runs"] else 0.0
        agent_cards += f"""
        <div class="card">
          <div class="card-title">{e(a)}</div>
          <div class="kpi"><b>{avg_a_pr}%</b><span>平均通过率</span></div>
          <div class="kpi"><b>{agg["runs"]}</b><span>run 数</span></div>
          <div class="kpi"><b>{avg_dur}s</b><span>平均时长</span></div>
        </div>"""

    # 任务通过率条形
    task_bars = ""
    for t in tasks:
        rate = summary["task_pass"].get(t, 0.0)
        task_bars += f"""
        <div class="bar-row">
          <div class="bar-label">{e(t)}</div>
          <div class="bar-track">{_bar(rate)}</div>
          <div class="bar-val">{round(rate*100,0):g}%</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Agent 评测报告</title>
<style>
  body {{ margin:0; font-family:'Segoe UI','PingFang SC',Arial,sans-serif; background:#F4F3EE; color:#1A1B1C; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:28px 20px 48px; }}
  .hero {{ background:linear-gradient(135deg,#1F3A5F,#2E7D7A); color:#fff; border-radius:16px; padding:22px 24px; }}
  .hero h1 {{ margin:0 0 6px; font-size:22px; }}
  .hero p {{ margin:2px 0; font-size:12.5px; color:rgba(255,255,255,0.82); }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ flex:1 1 150px; background:#fff; border-radius:12px; padding:14px 16px; box-shadow:0 1px 3px rgba(0,0,0,0.05); }}
  .kpi b {{ font-size:24px; display:block; }}
  .kpi span {{ font-size:12px; color:#6B7280; }}
  h2 {{ font-size:16px; margin:26px 0 10px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:12px; overflow:hidden; font-size:13px; }}
  th,td {{ padding:9px 12px; text-align:center; border-bottom:1px solid rgba(0,0,0,0.06); }}
  th {{ background:rgba(0,0,0,0.03); font-weight:600; }}
  td.ag {{ text-align:left; font-weight:600; }}
  td.ok {{ color:#2E7D32; }} td.half {{ color:#B26A00; }} td.bad {{ color:#C0392B; }}
  .sub {{ font-size:10px; color:#9AA0A6; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; }}
  .card {{ flex:1 1 180px; background:#fff; border-radius:12px; padding:14px 16px; box-shadow:0 1px 3px rgba(0,0,0,0.05); }}
  .card-title {{ font-weight:600; font-size:14px; margin-bottom:8px; }}
  .bar-row {{ display:flex; align-items:center; gap:12px; margin:8px 0; }}
  .bar-label {{ flex:0 0 64px; font-size:12.5px; }}
  .bar-track {{ flex:1 1 auto; }} .bar-val {{ flex:0 0 46px; text-align:right; font-size:12.5px; color:#4A5568; }}
  .note {{ font-size:12px; color:#6B7280; background:rgba(0,0,0,0.03); border-radius:10px; padding:12px 14px; margin-top:18px; line-height:1.6; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <h1>AI Agent 评测报告</h1>
    <p>生成时间：{e(generated_at)} · 后端 {len(agents)} 个 · 任务 {len(tasks)} 个 · run {summary['total_runs']} 次</p>
    <p>框架：ai-agent-eval · task-spec@v1 · 判定=确定性校验点，得分=权重×通过率</p>
  </div>

  <div class="kpis">
    <div class="kpi"><b>{len(agents)}</b><span>后端（Agent）</span></div>
    <div class="kpi"><b>{len(tasks)}</b><span>任务（L1-L5）</span></div>
    <div class="kpi"><b>{avg_score}</b><span>平均得分（权重分）</span></div>
    <div class="kpi"><b>{avg_pr}%</b><span>平均通过率</span></div>
  </div>

  <h2>后端 × 任务 得分矩阵（取各组合最好成绩）</h2>
  <table>
    <tr><th>后端</th>{header_cells}</tr>
    {table_rows}
  </table>

  <h2>后端汇总</h2>
  <div class="cards">{agent_cards}</div>

  <h2>任务通过率（全部 run 平均）</h2>
  {task_bars}

  <div class="note">
    <b>轨迹口径说明：</b>不同后端可观测性不同——minimal-react（自建 ReAct）为步骤级轨迹（每步 tool/args/observation），
    aider（第三方 CLI）为黑盒单次调用（仅 stdout）。得分基于产物判定（校验点），不受 Agent 自报影响。
    LLM Agent 具有非确定性，同一任务多次运行结果可能不同，故保留多 run 证据。
    <br><b>适配度观察：</b>aider 在"需自主创建数据产物"类任务（T102）上黑盒调用陷入循环而超时（记 0 分）；
    结对编程 Agent 擅长改已有代码，未必适合从零生成产物——这是 Agent × 任务类型适配度的真实体现。
  </div>
</div>
</body>
</html>"""


def generate_report(out: str = "reports/report.html") -> str:
    runs = load_runs()
    summary = summarize(runs)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(summary, generated_at), encoding="utf-8")
    return str(path)
