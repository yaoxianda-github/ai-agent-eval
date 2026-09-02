"""从 input/data.csv 找出金额最高的记录，写入 output/result.txt。

故意注入的缺陷（请修复）：
  1) 读取路径错误：读的是 "./data.csv"，应为 "input/data.csv"
  2) 取最大值用错了列：比较的是 "date"，应为 "amount"
"""

import csv
import os

os.makedirs("output", exist_ok=True)

with open("./data.csv", encoding="utf-8") as f:  # BUG 1: 路径错误
    rows = list(csv.DictReader(f))

best = max(rows, key=lambda r: r["date"])  # BUG 2: 应比较 amount
line = f"{best['date']},{best['category']},{best['amount']}\n"

with open("output/result.txt", "w", encoding="utf-8") as f:
    f.write(line)

print("done")
