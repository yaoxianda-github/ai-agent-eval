"""T003 复核：核对 input/ 下文件已按规范重命名，且 rename_map 完整准确。

规则：1) 小写；2) 空格 -> "-"；3) 移除 ( 和 )；4) 扩展名小写。
用法：python @scripts/verify_t003.py <workspace>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_BAD = re.compile(r"[A-Z ()]")


def normalize(name: str) -> str:
    s = name.lower().replace(" ", "-")
    s = s.replace("(", "").replace(")", "")
    return s


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t003.py <workspace>")
        return 2
    workspace = Path(argv[1]).resolve()
    indir = workspace / "input"
    report = workspace / "output" / "rename_map.md"

    if not report.is_file():
        print("VERIFY FAIL: missing output/rename_map.md")
        return 1

    # 1) 当前 input/ 下不应存在违反规范的文件名
    for p in indir.iterdir():
        if p.is_file() and _BAD.search(p.name):
            print(f"VERIFY FAIL: file still not normalized: {p.name}")
            return 1

    # 2) 解析映射，核对旧名 -> 期望新名
    pairs: list[tuple[str, str]] = []
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "->" in line:
            old, new = [x.strip() for x in line.split("->", 1)]
            pairs.append((old, new))
    if not pairs:
        print("VERIFY FAIL: no mapping rows in rename_map.md")
        return 1

    seen_new: set[str] = set()
    for old, new in pairs:
        if new != normalize(old):
            print(f"VERIFY FAIL: bad mapping {old!r} -> {new!r} (expected {normalize(old)!r})")
            return 1
        target = indir / new
        if not target.is_file():
            print(f"VERIFY FAIL: renamed file missing: {new}")
            return 1
        seen_new.add(new)

    # 3) input/ 下不应有未登记的规范化文件
    for p in indir.iterdir():
        if p.is_file() and p.name not in seen_new:
            print(f"VERIFY FAIL: unregistered normalized file: {p.name}")
            return 1

    print(f"VERIFY OK: {len(pairs)} file(s) renamed and mapped correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
