# -*- coding: utf-8 -*-
"""通用：从 answer.txt 提取首个数字与期望比对（GAIA exact-match 判定思路）。

期望值以字符串传入；数值型按 float 容差比较，文本型按规范化（去空白/小写/引号）比较。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", "", s.strip().lower().strip("\"'“”‘’"))


def first_number(s: str) -> str | None:
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return m.group(0) if m else None


def check_answer_file(ws: Path, expect_text: str | None = None,
                      expect_num: float | None = None) -> int:
    ans = ws / "output" / "answer.txt"
    if not ans.is_file():
        print("VERIFY FAIL: missing output/answer.txt")
        return 1
    content = ans.read_text(encoding="utf-8", errors="replace")

    if expect_num is not None:
        got = first_number(content)
        if got is None:
            print(f"VERIFY FAIL: no number found in answer.txt: {content.strip()!r}")
            return 1
        if abs(float(got) - expect_num) > 1e-6:
            print(f"VERIFY FAIL: answer={got} expected={expect_num}")
            return 1
        return 0

    if expect_text is not None:
        if normalize_text(content) != normalize_text(expect_text):
            print(f"VERIFY FAIL: answer={content.strip()!r} expected={expect_text!r}")
            return 1
        return 0

    print("VERIFY FAIL: no expected value provided")
    return 1


if __name__ == "__main__":
    # 冒烟测试
    t = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp")
    sys.exit(check_answer_file(t, expect_text="20"))
