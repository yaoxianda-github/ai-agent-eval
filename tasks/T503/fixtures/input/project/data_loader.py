# -*- coding: utf-8 -*-
"""CSV 数据加载。"""
import csv

def load_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))
