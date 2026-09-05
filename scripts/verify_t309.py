"""T309 复核：SWE-bench 式隐藏测试——加载修复后的 repo/stats.py 并断言统计正确。

mean: 平均值；median: 排序后中位数（偶数取中间两数平均）。
用法：python @scripts/verify_t309.py <workspace>
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t309.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    repo = ws / "input" / "repo"
    if not (repo / "stats.py").is_file():
        print("VERIFY FAIL: missing input/repo/stats.py")
        return 1
    sys.path.insert(0, str(repo))
    try:
        from stats import mean, median
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY FAIL: cannot import stats: {type(e).__name__}: {e}")
        return 1

    cases_mean = [([1, 2, 3], 2.0), ([5], 5.0), ([1.5, 2.5], 2.0), ([-1, 0, 1], 0.0)]
    for nums, want in cases_mean:
        got = mean(nums)
        if abs(float(got) - want) > 1e-9:
            print(f"VERIFY FAIL: mean({nums}) = {got}, expected {want}")
            return 1

    cases_median = [([3, 1, 2], 2.0), ([1, 2, 3, 4], 2.5), ([9, 7, 5], 7.0),
                    ([5, 3, 1, 7, 9], 5.0), ([2.0, 1.0], 1.5)]
    for nums, want in cases_median:
        got = median(nums)
        if abs(float(got) - want) > 1e-9:
            print(f"VERIFY FAIL: median({nums}) = {got}, expected {want}")
            return 1

    print(f"VERIFY OK: mean {len(cases_mean)} + median {len(cases_median)} assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
