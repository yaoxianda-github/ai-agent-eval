#!/usr/bin/env python3
"""需要第三方依赖的数据获取脚本。

用于评测 Agent 的容错与自我修复能力：
- 脚本依赖 `requests` 库（评测环境中故意不预装）
- 直接运行会报 ImportError: No module named 'requests'
- Agent 应该识别错误并执行 pip install requests，然后重新运行

注意：为了离线评测环境可用，脚本在安装 requests 后会用模拟数据
替代真实 HTTP 请求（不实际联网）。
"""
import sys

try:
    import requests
except ImportError:
    print("ImportError: No module named 'requests'", file=sys.stderr)
    print("提示：请先运行 pip install requests 安装依赖，然后重新执行本脚本", file=sys.stderr)
    sys.exit(1)

import os
import json

def main():
    # 模拟 API 响应（离线环境不实际联网）
    mock_response = {
        "status": "success",
        "data": [
            {"id": 1, "name": "Alice", "score": 95},
            {"id": 2, "name": "Bob", "score": 87},
            {"id": 3, "name": "Charlie", "score": 92},
        ]
    }

    os.makedirs("output", exist_ok=True)
    with open("output/scores.json", "w") as f:
        json.dump(mock_response, f, indent=2, ensure_ascii=False)

    # 计算平均分并写入文本结果
    avg = sum(d["score"] for d in mock_response["data"]) / len(mock_response["data"])
    with open("output/result.txt", "w") as f:
        f.write(f"数据获取成功\n")
        f.write(f"记录数: {len(mock_response['data'])}\n")
        f.write(f"平均分: {avg:.1f}\n")

    print(f"SUCCESS: 数据获取完成，平均分 {avg:.1f}")

if __name__ == "__main__":
    main()
