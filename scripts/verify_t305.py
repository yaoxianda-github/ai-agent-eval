"""T305 复核：重算"年薪最高且工龄>=5"的部门编号，与 output/answer.txt 精确比对。

GAIA exact-match 判定思路：答案从 answer.txt 提取，数字容差比较。
用法：python @scripts/verify_t305.py <workspace>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from answer_check import check_answer_file


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t305.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    src = ws / "input" / "employees.csv"
    if not src.is_file():
        print("VERIFY FAIL: missing input/employees.csv")
        return 1

    best_dept = None
    best_salary = -1.0
    with src.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            years = int(r["years"])
            salary = float(r["salary"])
            if years >= 5 and salary > best_salary:
                best_salary = salary
                best_dept = r["dept_id"]
    if best_dept is None:
        print("VERIFY FAIL: no employee matches condition")
        return 1

    return check_answer_file(ws, expect_num=float(best_dept))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
