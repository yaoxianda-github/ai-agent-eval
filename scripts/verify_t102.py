"""T102 判定：重算 sales.csv 汇总，与 output/summary.csv 比对，并检查 top3.md。

用法：python scripts/verify_t102.py <工作目录>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ws = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

data = ws / "input" / "sales.csv"
summary = ws / "output" / "summary.csv"
top3 = ws / "output" / "top3.md"

totals: dict[str, float] = {}
with data.open(encoding="utf-8") as f:
    for r in csv.DictReader(f):
        totals[r["category"]] = totals.get(r["category"], 0.0) + float(r["amount"])
expected = {k: round(v, 2) for k, v in totals.items()}

got: dict[str, float] = {}
with summary.open(encoding="utf-8") as f:
    for r in csv.DictReader(f):
        got[r["category"]] = round(float(r["total"]), 2)

if got != expected:
    print("FAIL: summary.csv 与重算结果不一致")
    print("  expected:", expected)
    print("  got     :", got)
    sys.exit(1)

top_cats = [k for k, _ in sorted(expected.items(), key=lambda kv: -kv[1])[:3]]
txt = top3.read_text(encoding="utf-8")
for cat in top_cats:
    if cat not in txt:
        print(f"FAIL: top3.md 缺少类别 {cat}")
        sys.exit(1)

print(f"PASS: summary 与 top3 校验通过（top3={top_cats}）")
