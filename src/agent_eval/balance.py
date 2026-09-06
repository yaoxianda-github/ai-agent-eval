"""DeepSeek 账户余额查询：为 CI 成本核算提供"余额差分"真实成本。

背景：黑盒后端（deepseek-harness）不返回 token usage，token 计价成本恒为 0。
DeepSeek 开放平台提供 /user/balance（余额精确到分），整轮 gate 成本（约 ¥0.5-0.8）
远大于精度，可用"跑前余额 − 跑后余额"得到真实扣费。单任务成本（<¥0.01）会被
精度舍入，因此差分粒度是"整轮 gate"，每任务成本仍走 token 估算。

注：余额更新可能有分钟级延迟，精确度以平台结算为准。

实现说明：用系统 curl（而非 urllib）——macOS 上 Python 自带 CA 与系统
keychain 不一致时 urllib 会证书校验失败（self signed certificate in chain），
curl 走系统证书正常。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

_BALANCE_URL = "https://api.deepseek.com/user/balance"


def fetch_balance_cny(api_key: str | None = None) -> float | None:
    """查询 DeepSeek 账户余额（CNY）。无 key / 请求失败返回 None（不抛异常）。"""
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    curl = shutil.which("curl")
    if not curl:
        return None
    try:
        proc = subprocess.run(
            [curl, "-s", "--max-time", "10", _BALANCE_URL, "-H", f"Authorization: Bearer {key}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        for info in data.get("balance_infos", []):
            if info.get("currency") == "CNY":
                return float(info["total_balance"])
        return None
    except Exception:  # noqa: BLE001  （网络/解析失败静默，不阻断门禁）
        return None
