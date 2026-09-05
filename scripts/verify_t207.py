"""T207 复核：重算日志级别计数与 ERROR 模块 Top3，与 output/log_report.md 比对。

行格式：YYYY-MM-DD HH:MM:SS LEVEL [module] message
ERROR_TOP3：按 ERROR 数降序，并列按模块名升序。
用法：python @scripts/verify_t207.py <workspace>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_LINE = re.compile(r"^\S+\s+\S+\s+([A-Z]+)\s+\[([^\]]+)\]")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t207.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    log = ws / "input" / "app.log"
    report = ws / "output" / "log_report.md"

    if not log.is_file():
        print("VERIFY FAIL: missing input/app.log")
        return 1
    if not report.is_file():
        print("VERIFY FAIL: missing output/log_report.md")
        return 1

    counts: dict[str, int] = {}
    err_mod: dict[str, int] = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        lvl, mod = m.group(1), m.group(2)
        counts[lvl] = counts.get(lvl, 0) + 1
        if lvl == "ERROR":
            err_mod[mod] = err_mod.get(mod, 0) + 1
    top3 = sorted(err_mod, key=lambda m: (-err_mod[m], m))[:3]

    total = sum(counts.values())
    expected = {
        "TOTAL": total,
        "INFO": counts.get("INFO", 0),
        "ERROR": counts.get("ERROR", 0),
        "WARN": counts.get("WARN", 0),
    }
    txt = report.read_text(encoding="utf-8", errors="replace")
    for key, val in expected.items():
        m = re.search(rf"^{key}\s*=\s*(\d+)", txt, re.MULTILINE)
        if not m:
            print(f"VERIFY FAIL: {key}= not found in report")
            return 1
        if int(m.group(1)) != val:
            print(f"VERIFY FAIL: {key} mismatch report={m.group(1)} actual={val}")
            return 1
    m_top = re.search(r"^ERROR_TOP3\s*=\s*([^\n]+)", txt, re.MULTILINE)
    if not m_top:
        print("VERIFY FAIL: ERROR_TOP3 not found in report")
        return 1
    got_top = [x.strip() for x in m_top.group(1).split(",") if x.strip()]
    if got_top != top3:
        print(f"VERIFY FAIL: ERROR_TOP3 mismatch report={got_top} actual={top3}")
        return 1

    print(f"VERIFY OK: counts={expected} top3={top3}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
