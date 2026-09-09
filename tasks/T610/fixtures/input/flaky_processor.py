#!/usr/bin/env python3
"""Flaky 数据处理脚本：第一次运行失败，第二次运行成功。

用于评测 Agent 的容错与重试能力：
- 第一次运行：模拟临时故障（网络超时/资源锁），返回非0退出码
- 第二次运行：正常执行，生成结果文件

Agent 应该检测到失败并重试，而不是直接放弃。
"""
import os
import sys

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flaky_state")

def main():
    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w") as f:
            f.write("attempted")
        print("ERROR: 连接数据服务器超时（临时故障，稍后重试可能成功）", file=sys.stderr)
        print("提示：这是一个间歇性错误，请重试命令", file=sys.stderr)
        sys.exit(1)

    os.makedirs("output", exist_ok=True)
    result = "date,category,amount\n2026-01-15,Electronics,1299.00\n2026-01-16,Books,89.50\n2026-01-17,Electronics,2499.00"
    with open("output/result.csv", "w") as f:
        f.write(result)
    print("SUCCESS: 数据处理完成，已生成 output/result.csv（3 条记录）")
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass

if __name__ == "__main__":
    main()
