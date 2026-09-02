"""T401 判定：result.txt 内容等于 data.csv 中金额最高的一行。

用法：python scripts/verify_t401.py <工作目录>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ws = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

result = (ws / "output" / "result.txt").read_text(encoding="utf-8").strip()

rows = list(csv.DictReader((ws / "input" / "data.csv").open(encoding="utf-8")))
best = max(rows, key=lambda r: float(r["amount"]))
expected = f"{best['date']},{best['category']},{best['amount']}"

if result != expected:
    print("FAIL: result.txt 与预期不一致")
    print("  expected:", expected)
    print("  got     :", result)
    sys.exit(1)

print("PASS: result.txt 校验通过")
