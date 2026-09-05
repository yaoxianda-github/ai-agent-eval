# Issue #42: stats 模块统计结果错误

用户反馈 `stats.mean([1,2,3])` 返回 6 而非 2.0；`stats.median([3,1,2])` 返回 2 但那是巧合，
对 `median([1,2,3,4])` 返回 3 而非 2.5。请修复 `repo/stats.py` 中 `mean` 与 `median` 的实现：
- mean 应返回总和除以元素个数；
- median 应先对列表排序：奇数个取中间值，偶数个取中间两数的平均值。
请勿修改 `repo/basic.py`。修复后运行 `python -c "from stats import mean,median; print(mean([1,2,3]))"` 自检。
