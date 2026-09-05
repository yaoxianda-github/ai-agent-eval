"""T308 复核：重算华东区 2026 年 Q1（1-3 月）总销售额，与 answer.txt 精确比对。

用法：python @scripts/verify_t308.py <workspace>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from answer_check import check_answer_file


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t308.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    src = ws / "input" / "sales.csv"
    if not src.is_file():
        print("VERIFY FAIL: missing input/sales.csv")
        return 1

    total = 0.0
    with src.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            month = int(r["date"].split("-")[1])
            if r["region"] == "华东" and 1 <= month <= 3:
                total += float(r["amount"])

    return check_answer_file(ws, expect_num=round(total, 2))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
