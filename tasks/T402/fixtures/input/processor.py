# -*- coding: utf-8 -*-
"""订单排名处理器：按 amount 降序输出排名。"""
import csv

def rank(rows):
    # 冒泡排序：功能正确但 O(n^2)，大数据量下很慢
    n = len(rows)
    for i in range(n):
        for j in range(0, n - i - 1):
            if rows[j][1] < rows[j + 1][1]:
                rows[j], rows[j + 1] = rows[j + 1], rows[j]
    return rows

def main():
    rows = []
    with open("input/orders.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append([r["order_id"], float(r["amount"])])
    ranked = rank(rows)
    with open("output/rank.txt", "w", encoding="utf-8") as f:
        for i, row in enumerate(ranked):
            f.write(f"{i},{row[0]},{row[1]:.2f}\n")

if __name__ == "__main__":
    main()
