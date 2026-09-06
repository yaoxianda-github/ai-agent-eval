"""License / 功能档位开关（Open Core 收费墙，V2.7 多 Agent 矩阵）。

同一套代码，社区版（community）默认免费、功能受限；导入有效 license 后解锁 Pro。
仅用标准库（hmac/hashlib/base64/json），不引第三方依赖。

License 形态（对称 HMAC，便于本地/私有化签发；未来 SaaS 化可平滑换成非对称签名）：
    <base64url(payload)>.<base64url(hmac_sha256(secret, payload_b64))>
    payload = {"plan":"pro","exp":"YYYY-MM-DD","seats":-1,"iss":"agent-eval"}

License 来源（优先级从高到低）：
    1. 环境变量 AGENT_EVAL_LICENSE（直接给 token）
    2. 环境变量 AGENT_EVAL_LICENSE_FILE 指定的文件
    3. 项目根 license.key
找不到或校验失败 → 回退社区版，不阻断任何核心（开源）能力。

⚠️ 内置 DEFAULT_SECRET 仅用于本地演示/自测，任何人都可自签；正式发售时用
AGENT_EVAL_LICENSE_SECRET 覆盖为私有密钥（签发与校验端一致）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import date, datetime
from pathlib import Path

# 仅用于本地演示的默认密钥；正式环境用环境变量覆盖
DEFAULT_SECRET = "agent-eval-local-demo-secret"

_PLAN_COMMUNITY = "community"
_PLAN_PRO = "pro"

# 各档位功能矩阵。数值类：0 / 负数表示不限。
_ENTITLEMENTS = {
    _PLAN_COMMUNITY: {
        "plan": _PLAN_COMMUNITY,
        "max_compare_agents": 2,      # 对比矩阵最多选 2 个 Agent
        "retain_batches": 1,          # 只保留最近 1 个对比批次（Pro 不限）
        "show_cost_stability": False, # 隐藏成本/稳定性列
        "export_csv": False,          # 禁止 CSV 导出
    },
    _PLAN_PRO: {
        "plan": _PLAN_PRO,
        "max_compare_agents": 0,      # 不限
        "retain_batches": 0,          # 不限
        "show_cost_stability": True,
        "export_csv": True,
    },
}

_cache: dict | None = None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


def _secret() -> str:
    return os.environ.get("AGENT_EVAL_LICENSE_SECRET") or DEFAULT_SECRET


def issue_license(
    plan: str = _PLAN_PRO,
    days: int = 365,
    *,
    seats: int = -1,
    secret: str | None = None,
) -> str:
    """签发一个 license token（CLI / 私有化交付用）。days 为有效天数。"""
    if plan not in _ENTITLEMENTS:
        raise ValueError(f"未知档位: {plan}（可用: {sorted(_ENTITLEMENTS)}）")
    secret = secret or _secret()
    exp = (datetime.now().date().toordinal() + int(days))
    payload = {
        "plan": plan,
        "iss": "agent-eval",
        "iat": date.today().isoformat(),
        "exp": date.fromordinal(exp).isoformat(),
        "seats": int(seats),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_license(token: str, secret: str | None = None) -> dict:
    """校验 license token，返回 payload；无效/过期抛 ValueError。"""
    secret = secret or _secret()
    try:
        body, sig = token.strip().split(".", 1)
    except ValueError as e:
        raise ValueError("license 格式错误") from e
    expect = _b64e(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        raise ValueError("license 签名不匹配")
    payload = json.loads(_b64d(body).decode("utf-8"))
    exp = payload.get("exp")
    if exp and datetime.strptime(exp, "%Y-%m-%d").date() < date.today():
        raise ValueError(f"license 已过期（{exp}）")
    if payload.get("plan") not in _ENTITLEMENTS:
        raise ValueError(f"license 档位未知: {payload.get('plan')}")
    return payload


def _read_token() -> str | None:
    token = os.environ.get("AGENT_EVAL_LICENSE")
    if token:
        return token.strip()
    f = os.environ.get("AGENT_EVAL_LICENSE_FILE")
    candidates = [Path(f)] if f else []
    candidates.append(Path("license.key"))
    for p in candidates:
        try:
            if p.is_file():
                txt = p.read_text(encoding="utf-8").strip()
                if txt:
                    return txt
        except OSError:
            continue
    return None


def get_entitlements(*, refresh: bool = False) -> dict:
    """返回当前生效的功能档位（带校验状态），结果缓存避免重复验签。"""
    global _cache
    if _cache is not None and not refresh:
        return dict(_cache)

    token = _read_token()
    plan = _PLAN_COMMUNITY
    licensed = False
    license_error = ""
    payload: dict = {}
    if token:
        try:
            payload = verify_license(token)
            plan = payload.get("plan", _PLAN_COMMUNITY)
            licensed = True
        except ValueError as e:
            license_error = str(e)

    ent = dict(_ENTITLEMENTS.get(plan, _ENTITLEMENTS[_PLAN_COMMUNITY]))
    ent.update(
        {
            "licensed": licensed,
            "exp": payload.get("exp", ""),
            "seats": payload.get("seats"),
            "error": license_error,
        }
    )
    _cache = ent
    return dict(ent)


def reset_cache() -> None:
    """测试用：清空缓存的档位结果。"""
    global _cache
    _cache = None


def can(agent_count: int, ent: dict | None = None) -> tuple[bool, str]:
    """校验「对比 N 个 Agent」是否在当前档位允许范围内，返回 (是否允许, 原因)。"""
    ent = ent or get_entitlements()
    cap = ent.get("max_compare_agents", 2)
    if cap and agent_count > cap:
        return False, (
            f"社区版对比矩阵最多选择 {cap} 个 Agent（当前 {agent_count} 个），"
            "导入 Pro License 后可不限数量横向对比。"
        )
    return True, ""


def _cli() -> None:
    """命令行签发：python -m agent_eval.license issue --plan pro --days 365"""
    import argparse

    ap = argparse.ArgumentParser(description="agent-eval license 签发/校验")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("issue", help="签发 license")
    i.add_argument("--plan", default=_PLAN_PRO, choices=sorted(_ENTITLEMENTS))
    i.add_argument("--days", type=int, default=365)
    i.add_argument("--seats", type=int, default=-1)
    v = sub.add_parser("verify", help="校验 license token（从 stdin 读入）")
    v.add_argument("token", nargs="?")
    args = ap.parse_args()
    if args.cmd == "issue":
        print(issue_license(args.plan, args.days, seats=args.seats))
    else:
        import sys
        tok = args.token or sys.stdin.read().strip()
        print(json.dumps(verify_license(tok), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
