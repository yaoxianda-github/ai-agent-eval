"""
用户登录和认证模块
PR: 增加 JWT token 刷新功能和密码重置功能
"""
import hashlib
import sqlite3
import time
import random
import string

SECRET_KEY = "my-secret-key-12345"
DB_PATH = "users.db"


def hash_password(password):
    # 对密码进行哈希
    return hashlib.md5(password.encode()).hexdigest()


def login(username, password):
    # 用户登录
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 查询用户
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{hash_password(password)}'"
    cursor.execute(query)
    user = cursor.fetchone()

    if not user:
        return {"success": False, "message": "用户名或密码错误"}

    # 生成 token
    token = generate_token(user[0])

    conn.close()
    return {
        "success": True,
        "token": token,
        "user_id": user[0],
        "username": user[1]
    }


def generate_token(user_id):
    # 生成 JWT token
    import json
    import base64

    payload = {
        "user_id": user_id,
        "exp": time.time() + 3600 * 24  # 24小时过期
    }

    # 简单的 token 生成
    payload_str = json.dumps(payload)
    payload_b64 = base64.b64encode(payload_str.encode()).decode()

    # 签名
    signature = hashlib.sha256((payload_b64 + SECRET_KEY).encode()).hexdigest()

    return f"{payload_b64}.{signature}"


def verify_token(token):
    # 验证 token
    import json
    import base64

    try:
        payload_b64, signature = token.split('.')

        # 验证签名
        expected_signature = hashlib.sha256((payload_b64 + SECRET_KEY).encode()).hexdigest()
        if signature != expected_signature:
            return None

        payload = json.loads(base64.b64decode(payload_b64).decode())

        # 检查是否过期
        if payload["exp"] < time.time():
            return None

        return payload
    except:
        return None


def refresh_token(token):
    # 刷新 token
    payload = verify_token(token)
    if not payload:
        return {"success": False, "message": "token 无效"}

    # 生成新 token
    new_token = generate_token(payload["user_id"])
    return {
        "success": True,
        "token": new_token
    }


def reset_password(username, email):
    # 重置密码
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 查询用户
    cursor.execute(f"SELECT * FROM users WHERE username = '{username}' AND email = '{email}'")
    user = cursor.fetchone()

    if not user:
        return {"success": False, "message": "用户不存在"}

    # 生成新密码
    new_password = ''.join(random.choices(string.ascii_letters + string.digits, k=8))

    # 更新密码
    cursor.execute(f"UPDATE users SET password = '{hash_password(new_password)}' WHERE id = {user[0]}")
    conn.commit()
    conn.close()

    # TODO: 发送邮件
    print(f"新密码已发送到 {email}: {new_password}")

    return {
        "success": True,
        "message": "密码已重置，请查收邮件",
        "new_password": new_password  # 临时返回，方便测试
    }


def change_password(user_id, old_password, new_password):
    # 修改密码
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(f"SELECT password FROM users WHERE id = {user_id}")
    user = cursor.fetchone()

    if not user or user[0] != hash_password(old_password):
        return {"success": False, "message": "原密码错误"}

    # 更新密码
    cursor.execute(f"UPDATE users SET password = '{hash_password(new_password)}' WHERE id = {user_id}")
    conn.commit()
    conn.close()

    return {"success": True, "message": "密码修改成功"}
