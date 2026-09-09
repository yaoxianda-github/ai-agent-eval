"""验证 app.py 三个 bug 的修复。

用法：python test_fix.py
启动 app.py 服务，发送测试请求，验证修复后的行为。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8899"


def request(method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
    url = BASE + path
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main() -> int:
    # 启动服务
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.5)

    failures = []
    try:
        # 测试 1: 正常创建用户
        status, data = request("POST", "/users", {"name": "Alice", "email": "alice@example.com"})
        if status != 201:
            failures.append(f"正常创建用户期望 201，实际 {status}")
        elif data.get("email") != "alice@example.com":
            failures.append(f"用户邮箱不正确: {data.get('email')}")
        else:
            print(f"✓ 测试1通过: 正常创建用户 (id={data.get('id')})")

        # 测试 2: 非法邮箱应返回 400
        status, data = request("POST", "/users", {"name": "Bob", "email": "not-an-email"})
        if status != 400:
            failures.append(f"非法邮箱期望 400，实际 {status} (Bug 1 未修复)")
        else:
            print(f"✓ 测试2通过: 非法邮箱返回 400")

        # 测试 3: 查询不存在用户应返回 404
        status, data = request("GET", "/users/99999")
        if status != 404:
            failures.append(f"查询不存在用户期望 404，实际 {status} (Bug 3 未修复)")
        else:
            print(f"✓ 测试3通过: 查询不存在用户返回 404")

        # 测试 4: 查询刚创建的用户应返回 200
        status, data = request("GET", "/users/1")
        if status != 200:
            failures.append(f"查询存在用户期望 200，实际 {status}")
        elif data.get("name") != "Alice":
            failures.append(f"查询用户姓名不正确: {data.get('name')}")
        else:
            print(f"✓ 测试4通过: 查询存在用户返回 200")

        # 测试 5: 并发创建用户计数正确（Bug 2）
        import threading
        errors = []
        def create_user(i):
            try:
                request("POST", "/users", {"name": f"User{i}", "email": f"user{i}@test.com"})
            except Exception as e:
                errors.append(str(e))
        threads = [threading.Thread(target=create_user, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 检查最后一个用户的 id 是否为 12（1 个 Alice + 10 个并发 = 11，加上可能 Bob 被拒绝所以是 11）
        # Alice=1, 并发10个=2~11，所以最大 id 应该是 11
        status, data = request("GET", "/users/11")
        if status != 200:
            failures.append(f"并发计数可能丢失：查询用户 11 返回 {status} (Bug 2 未修复)")
        else:
            print(f"✓ 测试5通过: 并发创建用户计数正确 (最大id=11)")

    finally:
        proc.terminate()
        proc.wait(timeout=3)

    if failures:
        print(f"\n❌ {len(failures)} 个测试失败:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n✅ 全部测试通过！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
