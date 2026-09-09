"""CLI 入口：读取输入文件，逐行解析并求值，输出结果。

用法：python main.py <input_file>
输入文件每行一条语句（表达式或赋值），输出最后一行表达式的求值结果。
"""
from __future__ import annotations

import sys

from evaluator import Evaluator
from lexer import Lexer
from parser import Parser


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python main.py <input_file>", file=sys.stderr)
        sys.exit(1)

    input_file = sys.argv[1]
    with open(input_file, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]

    evaluator = Evaluator()
    last_result = 0.0
    for line in lines:
        tokens = Lexer(line).tokenize()
        stmts = Parser(tokens).parse()
        last_result = evaluator.eval_program(stmts)

    # 输出最后结果（整数则去掉小数部分）
    if last_result == int(last_result):
        print(int(last_result))
    else:
        print(last_result)


if __name__ == "__main__":
    main()
