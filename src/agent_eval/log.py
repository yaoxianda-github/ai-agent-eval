"""统一日志配置（V2.6）。

整个框架此前无 logging，核心引擎完全静默。本模块提供：
- setup_logging()：全局配置（控制台 + results/logs/ 文件），CLI/Web 入口调用一次
- get_logger(name)：各模块便捷获取 logger
- RunLogger：运行级日志上下文管理器，单次评测的日志同时写到 results/runs/<id>/run.log

级别由环境变量 AGENT_EVAL_LOG_LEVEL 控制（默认 INFO），可选 DEBUG/INFO/WARNING/ERROR。
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_LEVEL = "INFO"
_configured = False


def _resolve_level(level: str | None) -> int:
    raw = (level or os.environ.get("AGENT_EVAL_LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    return getattr(logging, raw, logging.INFO)


def setup_logging(level: str | None = None, log_dir: str | Path | None = None) -> None:
    """配置全局日志：控制台 + 文件（results/logs/agent-eval-YYYYMMDD.log）。

    幂等：重复调用不会重复添加 handler。CLI 入口、Web create_app、CI 命令各调一次即可。
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(_resolve_level(level))

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # 控制台输出（stderr，不干扰 CLI 的 typer.echo 用户输出）
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 文件输出：results/logs/agent-eval-YYYYMMDD.log
    if log_dir is None:
        log_dir = Path("results") / "logs"
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"agent-eval-{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # 只读环境下文件输出失败不阻断，控制台仍可用
        pass

    # 降低第三方库噪音
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """各模块便捷获取 logger：logger = get_logger(__name__)。"""
    return logging.getLogger(name)


@contextmanager
def run_logger(run_id: str, run_dir: str | Path):
    """运行级日志上下文管理器：with 块内的日志同时写到 run_dir/run.log。

    用法：
        with run_logger(run_id, run_dir):
            rec = run_one(...)
    退出时自动移除 file handler，不影响全局日志。
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "run.log"
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        handler.close()
        root.removeHandler(handler)
