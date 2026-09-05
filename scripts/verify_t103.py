"""T103 复核：重算多源订单合并去重，与 output/merged.csv、output/stats.md 比对。

去重规则：order_id 唯一，重复时以 orders_a 为准；合并后按 order_id 排序。
用法：python @scripts/verify_t103.py <workspace>
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t103.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    merged = ws / "output" / "merged.csv"
    stats = ws / "output" / "stats.md"

    if not merged.is_file() or not stats.is_file():
        print("VERIFY FAIL: missing output/merged.csv or output/stats.md")
        return 1

    # 重算期望结果（a 优先）
    expected: dict[str, tuple[str, str]] = {}
    for fname in ("orders_a.csv", "orders_b.csv"):
        with (ws / "input" / fname).open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                oid = r["order_id"]
                if oid not in expected:
                    expected[oid] = (r["customer"], r["amount"])
    expected = {k: expected[k] for k in sorted(expected)}

    # 读取 Agent 产物
    got: dict[str, tuple[str, str]] = {}
    with merged.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            got[r["order_id"]] = (r["customer"], r["amount"])
    got = {k: got[k] for k in sorted(got)}

    if got != expected:
        print("VERIFY FAIL: merged.csv mismatch")
        print("  expected:", expected)
        print("  got     :", got)
        return 1

    total_amount = round(sum(float(v[1]) for v in expected.values()), 2)
    txt = stats.read_text(encoding="utf-8", errors="replace")
    m_ord = re.search(r"TOTAL_ORDERS\s*=\s*(\d+)", txt)
    m_amt = re.search(r"TOTAL_AMOUNT\s*=\s*([\d.]+)", txt)
    if not m_ord or not m_amt:
        print("VERIFY FAIL: TOTAL_ORDERS/TOTAL_AMOUNT not found in stats.md")
        return 1
    if int(m_ord.group(1)) != len(expected):
        print(f"VERIFY FAIL: TOTAL_ORDERS mismatch report={m_ord.group(1)} actual={len(expected)}")
        return 1
    if abs(float(m_amt.group(1)) - total_amount) > 0.01:
        print(f"VERIFY FAIL: TOTAL_AMOUNT mismatch report={m_amt.group(1)} actual={total_amount}")
        return 1

    print(f"VERIFY OK: {len(expected)} unique orders, total amount {total_amount}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
