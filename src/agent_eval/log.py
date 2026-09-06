"""统一日志配置（V2.6）。

整个框架此前无 logging，核心引擎完全静默。本模块提供：
- setup_logging()：全局配置（控制台 + results/logs/ 文件），CLI/Web 入口调用一次
- get_logger(name)：各模块便捷获取 logger
- RunLogger：运行级日志上下文管理器，单次评测的日志同时写到 results/runs/<id>/run.log

级别由环境变量 AGENT_EVAL_LOG_LEVEL 控制（默认 INFO），可选 DEBUG/INFO/WARNING/ERROR。

文件切割（V2.6）：全局日志文件单文件超过 10MB 自动切割，每个文件按
「年月日-时分秒」命名（agent-eval-YYYYMMDD-HHMMSS.log），旧文件原样保留为归档；
归档数量由 AGENT_EVAL_LOG_BACKUP_COUNT 控制（默认 10），单文件上限由
AGENT_EVAL_LOG_MAX_BYTES 控制（默认 10MB）。仅用标准库 logging，无第三方依赖。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_DEFAULT_LEVEL = "INFO"

# 文件切割默认值：单文件 10MB，最多保留 10 个归档
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 10
_STAMP_FORMAT = "%Y%m%d-%H%M%S"

_configured = False


def _resolve_level(level: str | None) -> int:
    raw = (level or os.environ.get("AGENT_EVAL_LOG_LEVEL") or _DEFAULT_LEVEL).upper()
    return getattr(logging, raw, logging.INFO)


def _env_int(name: str, default: int) -> int:
    """从环境变量读取整数，非法值回退默认。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


class TimestampedSizeRotatingHandler(logging.handlers.RotatingFileHandler):
    """按大小切割、文件按「年月日-时分秒」命名的 Handler。

    与标准 RotatingFileHandler 的 .1/.2 链式滚动不同：每个文件创建时即以当时
    时间戳命名，写满 maxBytes 后直接切换到一个新的时间戳文件，旧文件自然成为
    归档（无需重命名）；归档数超过 backupCount 时删除最老的归档。
    """

    def __init__(
        self,
        log_dir: str | Path,
        prefix: str = "agent-eval",
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
        encoding: str = "utf-8",
    ) -> None:
        self.log_dir = Path(log_dir)
        self.prefix = prefix
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # 归档文件名：agent-eval-YYYYMMDD-HHMMSS.log，同秒内再切割追加 -N
        self._archive_re = re.compile(
            rf"^{re.escape(prefix)}-\d{{8}}-\d{{6}}(-\d+)?\.log$"
        )
        initial = self._stamped_path()
        super().__init__(
            initial,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
        )

    def _stamped_path(self, stamp: str | None = None) -> str:
        stamp = stamp or datetime.now().strftime(_STAMP_FORMAT)
        path = self.log_dir / f"{self.prefix}-{stamp}.log"
        # 同一秒内多次切割：追加 -1/-2 序号，避免覆盖
        seq = 1
        while path.exists():
            path = self.log_dir / f"{self.prefix}-{stamp}-{seq}.log"
            seq += 1
        return str(path)

    def doRollover(self) -> None:  # noqa: N802 - 沿用标准库方法名
        # 关闭写满的当前文件
        if self.stream:
            self.stream.close()
            self.stream = None
        # 切换到新的时间戳文件继续写；旧文件保持其时间戳名即归档
        self.baseFilename = self._stamped_path()
        self._purge_old_archives()
        if not self.delay:
            self.stream = self._open()

    def _purge_old_archives(self) -> None:
        """归档数超过 backupCount 时，按修改时间删除最老的归档（不含当前文件）。"""
        if self.backupCount <= 0:
            return
        current = Path(self.baseFilename).name
        archives = [
            p
            for p in self.log_dir.glob(f"{self.prefix}-*.log")
            if self._archive_re.match(p.name) and p.name != current
        ]
        archives.sort(key=lambda p: p.stat().st_mtime)
        excess = len(archives) - self.backupCount
        for old in archives[: max(excess, 0)]:
            try:
                old.unlink()
            except OSError:
                pass


def setup_logging(
    level: str | None = None,
    log_dir: str | Path | None = None,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
    """配置全局日志：控制台 + results/logs/ 下按大小切割的时间戳文件。

    幂等：重复调用不会重复添加 handler。CLI 入口、Web create_app、CI 命令各调一次即可。
    单文件大小上限默认 10MB，写满后切到新的 agent-eval-YYYYMMDD-HHMMSS.log。
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

    # 文件输出：results/logs/agent-eval-YYYYMMDD-HHMMSS.log，单文件超 10MB 切割
    if log_dir is None:
        log_dir = Path("results") / "logs"
    if max_bytes is None:
        max_bytes = _env_int("AGENT_EVAL_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES)
    if backup_count is None:
        backup_count = _env_int("AGENT_EVAL_LOG_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT)
    try:
        file_handler = TimestampedSizeRotatingHandler(
            log_dir,
            prefix="agent-eval",
            max_bytes=max_bytes,
            backup_count=backup_count,
        )
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

    单次运行日志量小（远不到切割阈值），故 run.log 用普通 FileHandler，不做切割。

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
