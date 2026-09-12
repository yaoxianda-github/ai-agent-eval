"""agent-eval 熔断降级与在线监控（V3.3 P3）。

参考：Agent评测体系全生命周期6步闭环——第5步：在线监控+熔断降级
- 错误率 > 20% 触发熔断
- 单任务 > 20步 防死循环
- P99 延迟 > 30s 触发降级
- 每日采样 100 条人工复核
- 灰度发布：1% → 5% → 50% → 100%，每阶段 2-3 天
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CircuitConfig:
    """熔断降级配置。"""
    error_rate_threshold: float = 0.20      # 错误率阈值（>20%触发熔断）
    max_steps_per_task: int = 20            # 单任务最大步数（防死循环）
    p99_latency_threshold_s: float = 30.0   # P99延迟阈值（>30s触发降级）
    sample_rate: float = 0.01               # 灰度采样率（1%）
    daily_sample_count: int = 100           # 每日人工复核采样数
    cooldown_seconds: int = 300             # 熔断冷却时间（5分钟）
    half_open_max_requests: int = 5         # 半开状态最大请求数


@dataclass
class CircuitStats:
    """熔断统计。"""
    total_requests: int = 0
    error_count: int = 0
    success_count: int = 0
    timeout_count: int = 0
    step_overflow_count: int = 0
    latencies: list[float] = field(default_factory=list)
    window_start: float = field(default_factory=time.time)

    def reset(self) -> None:
        self.total_requests = 0
        self.error_count = 0
        self.success_count = 0
        self.timeout_count = 0
        self.step_overflow_count = 0
        self.latencies = []
        self.window_start = time.time()

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.error_count / self.total_requests

    @property
    def p99_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]


class CircuitBreaker:
    """熔断器：closed → open → half_open → closed。"""

    def __init__(self, config: CircuitConfig | None = None, stats_path: Path | None = None):
        self.config = config or CircuitConfig()
        self.state = "closed"  # closed / open / half_open
        self.stats = CircuitStats()
        self.open_time: float | None = None
        self.half_open_count = 0
        self.stats_path = stats_path
        if stats_path and stats_path.exists():
            self._load_stats()

    def _load_stats(self) -> None:
        try:
            data = json.loads(self.stats_path.read_text(encoding="utf-8"))
            self.stats = CircuitStats(**data.get("stats", {}))
            self.state = data.get("state", "closed")
            self.open_time = data.get("open_time")
        except (OSError, json.JSONDecodeError):
            pass

    def _save_stats(self) -> None:
        if not self.stats_path:
            return
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        self.stats_path.write_text(
            json.dumps({
                "state": self.state,
                "open_time": self.open_time,
                "stats": {
                    "total_requests": self.stats.total_requests,
                    "error_count": self.stats.error_count,
                    "success_count": self.stats.success_count,
                    "timeout_count": self.stats.timeout_count,
                    "step_overflow_count": self.stats.step_overflow_count,
                    "latencies": self.stats.latencies[-1000:],  # 保留最近1000条
                    "window_start": self.stats.window_start,
                },
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def allow_request(self) -> bool:
        """是否允许请求通过。"""
        if self.state == "closed":
            return True
        if self.state == "open":
            # 冷却期过后进入半开状态
            if self.open_time and time.time() - self.open_time >= self.config.cooldown_seconds:
                self.state = "half_open"
                self.half_open_count = 0
                self._save_stats()
                return True
            return False
        if self.state == "half_open":
            return self.half_open_count < self.config.half_open_max_requests
        return False

    def record_success(self, latency: float, steps: int) -> None:
        """记录成功请求。"""
        self.stats.total_requests += 1
        self.stats.success_count += 1
        self.stats.latencies.append(latency)
        if self.state == "half_open":
            self.half_open_count += 1
            # 半开状态下连续成功则关闭熔断
            if self.half_open_count >= self.config.half_open_max_requests:
                self.state = "closed"
                self.open_time = None
                self.stats.reset()
        self._save_stats()

    def record_failure(self, error_type: str = "error", latency: float = 0.0) -> None:
        """记录失败请求。"""
        self.stats.total_requests += 1
        self.stats.error_count += 1
        if error_type == "timeout":
            self.stats.timeout_count += 1
        elif error_type == "step_overflow":
            self.stats.step_overflow_count += 1
        if latency:
            self.stats.latencies.append(latency)

        # 检查是否需要熔断
        if self.stats.total_requests >= 10 and self.stats.error_rate > self.config.error_rate_threshold:
            self._trip("error_rate")
        elif self.stats.p99_latency > self.config.p99_latency_threshold_s and self.stats.total_requests >= 10:
            self._trip("p99_latency")

        if self.state == "half_open":
            # 半开状态下失败则重新打开
            self._trip("half_open_failure")
        self._save_stats()

    def _trip(self, reason: str) -> None:
        """触发熔断。"""
        self.state = "open"
        self.open_time = time.time()
        self._save_stats()

    def check_steps(self, steps: int) -> bool:
        """检查步数是否超限（防死循环）。"""
        return steps <= self.config.max_steps_per_task

    def get_status(self) -> dict[str, Any]:
        """获取熔断器状态。"""
        return {
            "state": self.state,
            "error_rate": round(self.stats.error_rate, 3),
            "p99_latency": round(self.stats.p99_latency, 2),
            "total_requests": self.stats.total_requests,
            "error_count": self.stats.error_count,
            "success_count": self.stats.success_count,
            "timeout_count": self.stats.timeout_count,
            "step_overflow_count": self.stats.step_overflow_count,
            "open_time": self.open_time,
            "cooldown_remaining": max(0, int(self.config.cooldown_seconds - (time.time() - (self.open_time or 0)))) if self.open_time else 0,
            "config": {
                "error_rate_threshold": self.config.error_rate_threshold,
                "max_steps_per_task": self.config.max_steps_per_task,
                "p99_latency_threshold_s": self.config.p99_latency_threshold_s,
                "cooldown_seconds": self.config.cooldown_seconds,
            },
        }


# ---------- 灰度发布 ----------

@dataclass
class GrayReleaseConfig:
    """灰度发布配置。"""
    enabled: bool = False
    stage: str = "0%"  # 0% / 1% / 5% / 50% / 100%
    stage_start_time: float = field(default_factory=time.time)
    min_stage_duration_hours: float = 48.0  # 每阶段最少2天
    rollback_on_error: bool = True
    rollback_error_rate: float = 0.30  # 错误率>30%自动回滚

    STAGES = ["0%", "1%", "5%", "50%", "100%"]

    def canary_pass(self) -> bool:
        """金丝雀判断：当前请求是否应该走新版本。"""
        if not self.enabled or self.stage == "0%":
            return False
        if self.stage == "100%":
            return True
        import random
        rate = float(self.stage.strip("%")) / 100.0
        return random.random() < rate

    def can_advance(self) -> bool:
        """是否可以进入下一阶段。"""
        if self.stage == "100%":
            return False
        elapsed = time.time() - self.stage_start_time
        return elapsed >= self.min_stage_duration_hours * 3600

    def advance(self) -> str:
        """进入下一阶段。"""
        if not self.can_advance():
            return self.stage
        idx = self.STAGES.index(self.stage)
        if idx < len(self.STAGES) - 1:
            self.stage = self.STAGES[idx + 1]
            self.stage_start_time = time.time()
        return self.stage

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "stage": self.stage,
            "stage_start_time": self.stage_start_time,
            "elapsed_hours": round((time.time() - self.stage_start_time) / 3600, 1),
            "min_stage_duration_hours": self.min_stage_duration_hours,
            "can_advance": self.can_advance(),
            "rollback_on_error": self.rollback_on_error,
            "rollback_error_rate": self.rollback_error_rate,
        }


# ---------- 每日采样审计 ----------

class DailySampler:
    """每日采样审计：每日采样 N 条运行记录供人工复核。"""

    def __init__(self, sample_count: int = 100, storage_path: Path | None = None):
        self.sample_count = sample_count
        self.storage_path = storage_path
        self.today_samples: list[str] = []
        self.today_date = time.strftime("%Y-%m-%d")
        if storage_path and storage_path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if data.get("date") == self.today_date:
                self.today_samples = data.get("samples", [])
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(
            json.dumps({
                "date": self.today_date,
                "samples": self.today_samples,
                "count": len(self.today_samples),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def should_sample(self, run_id: str) -> bool:
        """判断该 run 是否应该被采样。"""
        # 新的一天重置
        today = time.strftime("%Y-%m-%d")
        if today != self.today_date:
            self.today_date = today
            self.today_samples = []

        if len(self.today_samples) >= self.sample_count:
            return False
        if run_id in self.today_samples:
            return False
        self.today_samples.append(run_id)
        self._save()
        return True

    def get_status(self) -> dict[str, Any]:
        return {
            "date": self.today_date,
            "sampled_count": len(self.today_samples),
            "target_count": self.sample_count,
            "samples": self.today_samples,
        }
