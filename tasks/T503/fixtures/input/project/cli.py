# -*- coding: utf-8 -*-
"""命令行入口。

用法：python cli.py add 1 2 -> 3
"""
import argparse
from calculator import Calculator

def main():
    p = argparse.ArgumentParser(prog="calc")
    p.add_argument("op", choices=["add", "sub", "mul", "div"])
    p.add_argument("a", type=float)
    p.add_argument("b", type=float)
    args = p.parse_args()
    c = Calculator()
    print(getattr(c, args.op)(args.a, args.b))

if __name__ == "__main__":
    main()
