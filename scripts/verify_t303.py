"""T303 复核：加载 input/money.py 并运行 parse_amount 全部断言。

用法：python @scripts/verify_t303.py <workspace>
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("USAGE: python verify_t303.py <workspace>")
        return 2
    ws = Path(argv[1]).resolve()
    input_dir = ws / "input"
    money_file = input_dir / "money.py"
    if not money_file.is_file():
        print("VERIFY FAIL: missing input/money.py")
        return 1
    sys.path.insert(0, str(input_dir))
    try:
        from money import parse_amount
    except Exception as e:  # noqa: BLE001
        print(f"VERIFY FAIL: cannot import money.py: {type(e).__name__}: {e}")
        return 1

    cases = [
        ("1,234.56", 1234.56),
        ("$100", 100.0),
        ("-50", -50.0),
        ("0.99", 0.99),
        ("1,000", 1000.0),
        ("-$50.5", -50.5),
        ("123", 123.0),
    ]
    for text, want in cases:
        try:
            got = parse_amount(text)
        except Exception as e:  # noqa: BLE001
            print(f"VERIFY FAIL: parse_amount({text!r}) raised {type(e).__name__}: {e}")
            return 1
        if abs(float(got) - want) > 1e-9:
            print(f"VERIFY FAIL: parse_amount({text!r}) = {got}, expected {want}")
            return 1

    print(f"VERIFY OK: parse_amount passed {len(cases)} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
