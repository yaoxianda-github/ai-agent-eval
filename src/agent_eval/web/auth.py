"""用户认证：密码 hash + HMAC-signed token（零第三方依赖）。

- 密码：PBKDF2-HMAC-SHA256，100k 迭代，随机 salt，存储格式 pbkdf2$100000$salt_hex$hash_hex
- Token：base64url(header).base64url(payload).base64url(hmac_sha256)，含 user_id/username/role/exp
- 不引入 bcrypt / PyJWT / itsdangerous，保持依赖干净
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

# token 签名密钥：优先环境变量，否则用项目级固定密钥（本地工具够用）
_SECRET = os.environ.get("AGENT_EVAL_SECRET", "agent-eval-local-secret-change-me")
_TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 天
_PBKDF2_ITERATIONS = 100_000


def hash_password(password: str) -> str:
    """生成 PBKDF2 密码 hash，格式 pbkdf2$iter$salt_hex$hash_hex。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否匹配存储的 hash。"""
    try:
        algo, iter_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2":
            return False
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(s: str) -> bytes:
    padding = "=" * (4 - len(s) % 4) if len(s) % 4 else ""
    return base64.urlsafe_b64decode(s + padding)


def create_token(user_id: int, username: str, role: str, ttl: int = _TOKEN_TTL_SECONDS) -> str:
    """签发 HMAC-signed token。"""
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl,
    }
    h = _b64encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    sig = hmac.new(_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64encode(sig)}"


def decode_token(token: str) -> Optional[dict]:
    """校验并解析 token，失败返回 None。"""
    try:
        h, p, s = token.split(".")
        signing_input = f"{h}.{p}".encode()
        expected_sig = hmac.new(_SECRET.encode(), signing_input, hashlib.sha256).digest()
        actual_sig = _b64decode(s)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64decode(p))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except (ValueError, json.JSONDecodeError, KeyError):
        return None


def extract_token(authorization: Optional[str]) -> Optional[str]:
    """从 Authorization: Bearer <token> 头提取 token。"""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None
