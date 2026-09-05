"""T106 复核：校验 output/config.fixed.json 为合法 JSON 且字段类型/范围全部正确。

约束：name=str, service=str, version=int, retries=int, timeout=int(1-60),
max_conn=int(>=1), rate_limit=float(0-1)。
用法：python @scripts/verify_t106.py <workspace>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t106.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    target = ws / "output" / "config.fixed.json"
    if not target.is_file():
        print("VERIFY FAIL: missing output/config.fixed.json")
        return 1

    try:
        cfg = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY FAIL: invalid JSON: {e}")
        return 1

    if not isinstance(cfg, dict):
        print("VERIFY FAIL: config is not a JSON object")
        return 1
    if not isinstance(cfg.get("name"), str):
        print("VERIFY FAIL: name must be str")
        return 1
    if not isinstance(cfg.get("service"), str):
        print("VERIFY FAIL: service must be str")
        return 1
    if not isinstance(cfg.get("version"), int):
        print("VERIFY FAIL: version must be int")
        return 1
    if not isinstance(cfg.get("retries"), int):
        print("VERIFY FAIL: retries must be int")
        return 1
    t = cfg.get("timeout")
    if not isinstance(t, int) or not (1 <= t <= 60):
        print(f"VERIFY FAIL: timeout must be int in [1,60], got {t!r}")
        return 1
    mc = cfg.get("max_conn")
    if not isinstance(mc, int) or mc < 1:
        print(f"VERIFY FAIL: max_conn must be int >= 1, got {mc!r}")
        return 1
    rl = cfg.get("rate_limit")
    if not isinstance(rl, (int, float)) or not (0 <= float(rl) <= 1):
        print(f"VERIFY FAIL: rate_limit must be in [0,1], got {rl!r}")
        return 1

    print("VERIFY OK: config.fixed.json is valid and all constraints satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
