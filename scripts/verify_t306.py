"""T306 复核：重算订单总额（sum qty*price，跨文件关联），与 answer.txt 精确比对。

用法：python @scripts/verify_t306.py <workspace>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from answer_check import check_answer_file


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t306.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    prod_file = ws / "input" / "products.csv"
    order_file = ws / "input" / "orders.csv"
    if not prod_file.is_file() or not order_file.is_file():
        print("VERIFY FAIL: missing products.csv or orders.csv")
        return 1

    price: dict[str, float] = {}
    with prod_file.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            price[r["product_id"]] = float(r["price"])

    total = 0.0
    with order_file.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            total += price[r["product_id"]] * float(r["qty"])

    return check_answer_file(ws, expect_num=round(total, 2))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
