"""T402 复核：核对排名结果正确（rank 从 1 起），并验证 rank 大数据量性能（<10s）。

用法：python @scripts/verify_t402.py <workspace>
注意：性能测试用 signal 闹钟保护，防止未优化的 O(n^2) 实现挂起整个评测。
"""

from __future__ import annotations

import importlib.util
import signal
import sys
import time
from pathlib import Path

_PERF_N = 20000
_PERF_LIMIT_S = 10.0


def _load_rank(ws: Path):
    proc_file = ws / "input" / "processor.py"
    if not proc_file.is_file():
        raise FileNotFoundError("missing input/processor.py")
    spec = importlib.util.spec_from_file_location("t402_processor", proc_file)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load processor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.rank


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t402.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    rank_file = ws / "output" / "rank.txt"
    if not rank_file.is_file():
        print("VERIFY FAIL: missing output/rank.txt")
        return 1

    # 1) 正确性：重算期望排名
    rows: list[list] = []
    with (ws / "input" / "orders.csv").open(encoding="utf-8") as f:
        import csv

        for r in csv.DictReader(f):
            rows.append([r["order_id"], float(r["amount"])])
    expected = sorted(rows, key=lambda x: (-x[1], x[0]))
    lines = [ln.strip() for ln in rank_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if len(lines) != len(expected):
        print(f"VERIFY FAIL: rank.txt rows {len(lines)} != expected {len(expected)}")
        return 1
    for i, ln in enumerate(lines):
        parts = ln.split(",")
        if len(parts) != 3:
            print(f"VERIFY FAIL: bad line {ln!r}")
            return 1
        rank, oid, amt = parts[0], parts[1], parts[2]
        want_rank = str(i + 1)  # rank 必须从 1 开始
        if rank != want_rank:
            print(f"VERIFY FAIL: line {i+1} rank={rank} (must start from 1)")
            return 1
        if oid != expected[i][0] or abs(float(amt) - expected[i][1]) > 0.001:
            print(f"VERIFY FAIL: line {i+1} order/value mismatch")
            return 1

    # 2) 性能：rank 在 2 万行下 < 10s
    try:
        rank = _load_rank(ws)
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY FAIL: cannot load processor.rank: {e}")
        return 1

    def _timeout(*_):
        print(f"VERIFY FAIL: rank too slow (>{_PERF_LIMIT_S:.0f}s on {_PERF_N} rows)")
        sys.exit(1)

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(int(_PERF_LIMIT_S) + 1)
    big = [[f"o{i}", float((i * 7919) % 1000)] for i in range(_PERF_N)]
    t0 = time.monotonic()
    rank(big)
    elapsed = time.monotonic() - t0
    signal.alarm(0)
    if elapsed > _PERF_LIMIT_S:
        print(f"VERIFY FAIL: rank took {elapsed:.1f}s, limit {_PERF_LIMIT_S:.0f}s")
        return 1

    print(f"VERIFY OK: {len(expected)} rows ranked correctly, rank() {elapsed:.2f}s on {_PERF_N} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
