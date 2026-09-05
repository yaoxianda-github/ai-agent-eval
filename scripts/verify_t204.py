"""T204 复核：核对 docs 已按 category 归档到 output/archive/<类别>/，且原目录清空。

每篇文档首行 front-matter：category: <类别>。
用法：python @scripts/verify_t204.py <workspace>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_CAT = re.compile(r"^\s*category\s*:\s*(\S+)\s*$")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t204.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    src = ws / "input" / "docs"
    archive = ws / "output" / "archive"

    # 1) 原目录不应残留 .md
    leftover = list(src.rglob("*.md")) if src.exists() else []
    if leftover:
        print(f"VERIFY FAIL: docs not fully archived, left: {[p.name for p in leftover]}")
        return 1

    # 2) 归档目录中的每篇 md 应位于与其 category 相同的子目录
    if not archive.exists():
        print("VERIFY FAIL: output/archive not created")
        return 1
    archived = list(archive.rglob("*.md"))
    if not archived:
        print("VERIFY FAIL: no archived documents")
        return 1
    for p in archived:
        first = p.read_text(encoding="utf-8", errors="replace").splitlines()
        m = _CAT.match(first[0]) if first else None
        if not m:
            print(f"VERIFY FAIL: missing category front-matter in {p.name}")
            return 1
        cat = m.group(1)
        if p.parent.name != cat:
            print(f"VERIFY FAIL: {p.name} in {p.parent.name}/ but category={cat}")
            return 1

    # 3) 应有 6 篇文档、3 个类别
    if len(archived) != 6:
        print(f"VERIFY FAIL: expected 6 docs, got {len(archived)}")
        return 1
    cats = sorted({p.parent.name for p in archived})
    if cats != sorted(["api", "performance", "security"]):
        print(f"VERIFY FAIL: unexpected category set {cats}")
        return 1

    print(f"VERIFY OK: {len(archived)} docs archived into {len(cats)} categories")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
