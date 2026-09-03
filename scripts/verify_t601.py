"""T601 复核脚本：比对 input/app.log 的 ERROR 行数与报告中标注的 ERROR_COUNT。

用法（由 spec 的 cmd_exit_zero 校验点调用）：python @scripts/verify_t601.py .
参数 "." 由 verifiers 解析为工作目录绝对路径。

注意：输出统一使用英文，避免 Windows GBK 控制台 + utf-8 捕获导致的中文乱码。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 日志行格式：YYYY-MM-DD HH:MM:SS LEVEL message（ERROR 是第 3 个字段）
_LEVEL_RE = re.compile(r"^\S+\s+\S+\s+ERROR\b")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t601.py <workspace>")
        return 2
    workspace = Path(argv[1]).resolve()
    log = workspace / "input" / "app.log"
    report = workspace / "output" / "errors_report.md"

    if not log.is_file():
        print("VERIFY FAIL: missing input/app.log")
        return 1
    if not report.is_file():
        print("VERIFY FAIL: missing output/errors_report.md")
        return 1

    actual = sum(
        1
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
        if _LEVEL_RE.match(line.strip())
    )
    m = re.search(
        r"ERROR_COUNT\s*=\s*(\d+)",
        report.read_text(encoding="utf-8", errors="replace"),
    )
    if not m:
        print("VERIFY FAIL: ERROR_COUNT not found in report")
        return 1
    if int(m.group(1)) != actual:
        print(f"VERIFY FAIL: count mismatch report={m.group(1)} actual={actual}")
        return 1
    print(f"VERIFY OK: ERROR_COUNT={actual}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
