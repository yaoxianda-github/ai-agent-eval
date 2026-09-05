# -*- coding: utf-8 -*-
"""基础统计模块。"""

def mean(nums):
    """返回数值列表的算术平均值。"""
    return sum(nums)

def median(nums):
    """返回数值列表的中位数（列表长度可能为奇数或偶数）。"""
    n = len(nums)
    mid = n // 2
    return nums[mid]
