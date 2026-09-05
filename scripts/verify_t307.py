"""T307 复核：谜题答案应为一个整数（抽屉原理：取 3 次必有两球同色）。

用法：python @scripts/verify_t307.py <workspace>
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from answer_check import check_answer_file


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t307.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    return check_answer_file(ws, expect_num=3)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
