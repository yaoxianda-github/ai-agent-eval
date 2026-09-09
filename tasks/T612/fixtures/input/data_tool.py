#!/usr/bin/env python3
"""数据处理 CLI 工具：参数错误时返回有用的错误信息。

用于评测 Agent 的容错与自我纠正能力：
- 工具需要 --input 参数指定输入文件
- 不带参数或参数错误时，返回明确的错误信息和用法提示
- Agent 应该从错误信息中学习正确用法，而不是反复用错误参数调用

用法：
  python data_tool.py --input <文件路径> [--output <输出路径>]
"""
import argparse
import os
import sys

def main():
    parser = argparse.ArgumentParser(
        description="数据处理工具：统计输入文件中的记录数和关键字段",
        epilog="示例：python data_tool.py --input data.csv --output result.txt"
    )
    parser.add_argument("--input", required=True, help="输入文件路径（必需）")
    parser.add_argument("--output", default="output/result.txt", help="输出文件路径")
    parser.add_argument("--delimiter", default=",", help="字段分隔符（默认逗号）")

    args = parser.parse_args()

    # 检查输入文件是否存在
    if not os.path.exists(args.input):
        print(f"ERROR: 输入文件不存在: {args.input}", file=sys.stderr)
        print(f"提示：请检查文件路径，使用 --input 参数指定正确的输入文件", file=sys.stderr)
        print(f"用法：python data_tool.py --input <文件路径>", file=sys.stderr)
        sys.exit(2)

    # 读取并处理文件
    with open(args.input, "r") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        print("ERROR: 输入文件为空", file=sys.stderr)
        sys.exit(3)

    # 统计
    header = lines[0].split(args.delimiter)
    data_lines = lines[1:]
    record_count = len(data_lines)

    # 找金额列并计算总和
    amount_idx = None
    for i, col in enumerate(header):
        if "amount" in col.lower() or "金额" in col:
            amount_idx = i
            break

    total_amount = 0.0
    if amount_idx is not None:
        for line in data_lines:
            fields = line.split(args.delimiter)
            if amount_idx < len(fields):
                try:
                    total_amount += float(fields[amount_idx])
                except ValueError:
                    pass

    # 生成输出
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        f.write(f"数据处理报告\n")
        f.write(f"输入文件: {args.input}\n")
        f.write(f"字段数: {len(header)}\n")
        f.write(f"记录数: {record_count}\n")
        if amount_idx is not None:
            f.write(f"金额总和: {total_amount:.2f}\n")
        f.write(f"字段列表: {', '.join(header)}\n")

    print(f"SUCCESS: 处理完成，{record_count} 条记录，结果已写入 {args.output}")

if __name__ == "__main__":
    main()
