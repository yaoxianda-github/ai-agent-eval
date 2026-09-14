"""环境变量配置管理（V3.6）。

支持在 Web 设置页面快速配置 API Key 等环境变量，写入项目根目录 .env 文件。
.evn 文件已在 .gitignore 中排除，不会提交到仓库。

设计原则：
- 只管理白名单内的环境变量（API Key、Base URL 等），避免误改系统变量
- 读取时只返回是否已配置（masked），不返回实际值，防止泄露
- 写入时追加或更新 .env 文件中的对应行
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


# 白名单：允许在 Web 端配置的环境变量
# key: 环境变量名, label: 显示名称, desc: 说明, used_by: 使用的后端
ENV_WHITELIST: dict[str, dict] = {
    "DEEPSEEK_API_KEY": {
        "label": "DeepSeek API Key",
        "desc": "minimal-react / deepseek-harness / aider 默认使用",
        "used_by": ["minimal-react", "deepseek-harness", "aider"],
        "category": "api_key",
    },
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic API Key",
        "desc": "claude-code / hermes-agent 使用（支持 opus 4.8）",
        "used_by": ["claude-code", "hermes-agent"],
        "category": "api_key",
    },
    "OPENAI_API_KEY": {
        "label": "OpenAI API Key",
        "desc": "codex-agent 使用",
        "used_by": ["codex-agent"],
        "category": "api_key",
    },
    "MOONSHOT_API_KEY": {
        "label": "Moonshot (Kimi) API Key",
        "desc": "kimi-code 使用（也支持 KIMI_API_KEY 别名）",
        "used_by": ["kimi-code"],
        "category": "api_key",
    },
    "DASHSCOPE_API_KEY": {
        "label": "DashScope (通义千问) API Key",
        "desc": "qoder-agent 使用（也支持 QWEN_API_KEY 别名）",
        "used_by": ["qoder-agent"],
        "category": "api_key",
    },
    "LLM_BASE_URL": {
        "label": "自定义 LLM Base URL",
        "desc": "覆盖默认 API 端点（如代理、私有部署）",
        "used_by": ["minimal-react"],
        "category": "endpoint",
    },
    "LLM_API_KEY": {
        "label": "自定义 LLM API Key",
        "desc": "配合 LLM_BASE_URL 使用（OpenAI 兼容端点）",
        "used_by": ["minimal-react"],
        "category": "api_key",
    },
}


def _env_file_path() -> Path:
    """获取 .env 文件路径（项目根目录）。"""
    # 从当前文件向上找项目根（src/agent_eval/config_manager.py -> 项目根）
    return Path(__file__).resolve().parent.parent.parent / ".env"


def read_env_file() -> dict[str, str]:
    """读取 .env 文件，返回 key-value 字典。"""
    env_path = _env_file_path()
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$', line)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            # 去除引号
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            result[key] = value
    return result


def write_env_file(values: dict[str, str]) -> None:
    """写入环境变量到 .env 文件。

    - 如果 key 已存在，更新其值
    - 如果 key 不存在，追加到文件末尾
    - value 为空字符串时，删除该 key（注释掉）
    """
    env_path = _env_file_path()
    existing = read_env_file()

    # 合并新值
    for key, value in values.items():
        if key not in ENV_WHITELIST:
            continue  # 只允许白名单内的变量
        existing[key] = value

    # 写回文件
    lines: list[str] = []
    written_keys: set[str] = set()

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(line)
                continue
            match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match.group(1) in existing:
                key = match.group(1)
                value = existing[key]
                if value == "":
                    # 空值：注释掉
                    lines.append(f"# {key}=")
                else:
                    lines.append(f'{key}="{value}"')
                written_keys.add(key)
            else:
                lines.append(line)

    # 追加新 key
    for key, value in existing.items():
        if key not in written_keys and key in ENV_WHITELIST:
            if value:
                lines.append(f'{key}="{value}"')
            else:
                lines.append(f"# {key}=")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_env_status() -> list[dict]:
    """获取所有白名单环境变量的配置状态（不返回实际值）。

    返回列表，每项包含：
    - key: 环境变量名
    - label: 显示名称
    - desc: 说明
    - used_by: 使用的后端列表
    - category: 类别（api_key / endpoint）
    - configured: 是否已配置（.env 文件或系统环境变量）
    - source: 配置来源（.env / environment / none）
    """
    env_file = read_env_file()
    result: list[dict] = []
    for key, meta in ENV_WHITELIST.items():
        in_env_file = key in env_file and env_file[key] != ""
        in_system = bool(os.environ.get(key))
        configured = in_env_file or in_system
        if in_env_file:
            source = ".env"
        elif in_system:
            source = "environment"
        else:
            source = "none"
        result.append({
            "key": key,
            "label": meta["label"],
            "desc": meta["desc"],
            "used_by": meta["used_by"],
            "category": meta["category"],
            "configured": configured,
            "source": source,
        })
    return result


def apply_env_to_process() -> int:
    """将 .env 文件中的变量加载到当前进程环境。

    .env 文件优先级高于系统环境变量（会覆盖），方便用户通过 Web 设置页面修改配置，
    不用去改 ~/.zshrc 等系统级配置文件。
    只覆盖白名单内的变量，非空值才覆盖。
    被覆盖的系统环境变量原始值会保存到 _system_env_backup，供 fallback 使用。
    返回加载/覆盖的变量数量。
    """
    env_file = read_env_file()
    count = 0
    for key, value in env_file.items():
        if key in ENV_WHITELIST and value:
            # 保存被覆盖的系统环境变量原始值（只保存一次，避免重复覆盖）
            if key not in _system_env_backup and key in os.environ:
                _system_env_backup[key] = os.environ[key]
            os.environ[key] = value
            count += 1
    return count


# 被 .env 覆盖的系统环境变量原始值，供 fallback 使用
_system_env_backup: dict[str, str] = {}


def get_fallback_env(key: str) -> str | None:
    """获取被 .env 覆盖的系统环境变量原始值。

    如果 .env 中的配置无效，可以用这个函数获取系统环境变量中的原始值作为 fallback。
    """
    return _system_env_backup.get(key)


def resolve_env_with_fallback(key: str) -> tuple[str | None, str]:
    """获取环境变量值，并返回来源。

    优先级：当前进程环境（.env 覆盖后）> 系统环境变量备份（被覆盖前的原始值）
    返回 (value, source)，source 为 "current" 或 "fallback" 或 "none"。
    """
    current = os.environ.get(key)
    if current:
        return current, "current"
    fallback = _system_env_backup.get(key)
    if fallback:
        return fallback, "fallback"
    return None, "none"
