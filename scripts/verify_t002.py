"""T002 复核：检查 input/*.txt 已去除 BOM 与全角标点，且报告存在。

用法：python @scripts/verify_t002.py <workspace>
注意：输出统一使用英文，避免 Windows GBK 控制台 + utf-8 捕获导致的中文乱码。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_FULLWIDTH = re.compile(r"[，。！？；：]")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t002.py <workspace>")
        return 2
    workspace = Path(argv[1]).resolve()
    report = workspace / "output" / "report.md"

    if not report.is_file():
        print("VERIFY FAIL: missing output/report.md")
        return 1

    files = sorted(workspace.glob("input/*.txt"))
    if not files:
        print("VERIFY FAIL: no input/*.txt files")
        return 1

    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        if "\ufeff" in text:
            print(f"VERIFY FAIL: BOM still present in {p.name}")
            return 1
        m = _FULLWIDTH.search(text)
        if m:
            print(f"VERIFY FAIL: fullwidth char {m.group()!r} in {p.name}")
            return 1

    print(f"VERIFY OK: {len(files)} file(s) normalized (no BOM, no fullwidth)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
