"""用户管理 API（含 3 个已知 bug，需修复）。

Bug 1（数据验证）：创建用户时未验证邮箱格式，任意字符串都能存入。
Bug 2（并发安全）：user_count 自增不是原子操作，并发请求会丢计数。
Bug 3（错误处理）：查询不存在用户时抛 KeyError 导致 500，应返回 404。

修复要求：
1. 添加邮箱格式验证（包含 @ 和域名），非法邮箱返回 400
2. 使用 threading.Lock 保护 user_count 自增操作
3. 查询不存在用户时返回 404 状态码和错误信息
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# 内存数据存储
users: dict[int, dict] = {}
user_count = 0
# TODO Bug 2: 添加 threading.Lock() 保护 user_count


class UserHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """创建用户：POST /users"""
        parsed = urlparse(self.path)
        if parsed.path != "/users":
            self._send_json(404, {"error": "not found"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON"})
            return

        name = data.get("name", "")
        email = data.get("email", "")

        # TODO Bug 1: 添加邮箱格式验证，非法邮箱返回 400
        # 验证规则：包含 @，@ 后至少有一个 .，且 @ 前后都有字符

        global user_count
        # TODO Bug 2: 使用 lock 保护 user_count 自增
        user_count += 1
        user_id = user_count
        users[user_id] = {"id": user_id, "name": name, "email": email}
        self._send_json(201, users[user_id])

    def do_GET(self) -> None:
        """查询用户：GET /users/<id>"""
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "users":
            self._send_json(404, {"error": "not found"})
            return

        try:
            user_id = int(parts[1])
        except ValueError:
            self._send_json(400, {"error": "invalid user id"})
            return

        # TODO Bug 3: 用户不存在时应返回 404，而不是抛 KeyError
        user = users[user_id]
        self._send_json(200, user)

    def log_message(self, format: str, *args) -> None:
        pass  # 静默日志


def run(port: int = 8899) -> None:
    server = HTTPServer(("127.0.0.1", port), UserHandler)
    print(f"用户管理 API 运行在 http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
