"""跨平台评测用例转换器（M1）。

将其他平台的评测基准（SWE-bench / GAIA / AgentBench 等）转换为
ai-agent-eval 的 spec.yaml 任务格式，快速扩充评测集。

每个转换器实现 BaseConverter.convert()，返回 list[TaskSpec]。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from agent_eval.spec import TaskSpec


class BaseConverter(ABC):
    """评测用例转换器基类。"""

    name: str = "base"

    @abstractmethod
    def convert(self, source: Path, output_dir: Path, **kwargs) -> list[TaskSpec]:
        """读取源文件，转换为 TaskSpec 列表并写入 output_dir。

        Args:
            source: 源数据文件路径（JSON / JSONL / 目录）
            output_dir: 输出目录，转换器在此创建 tasks/<id>/spec.yaml
            **kwargs: 转换器特定参数

        Returns:
            转换后的 TaskSpec 列表
        """
        raise NotImplementedError

    def _write_spec(self, task: TaskSpec, output_dir: Path) -> Path:
        """将 TaskSpec 写入 output_dir/<id>/spec.yaml。"""
        import yaml

        task_dir = output_dir / task.id
        task_dir.mkdir(parents=True, exist_ok=True)
        spec_path = task_dir / "spec.yaml"

        # 构造可序列化的 dict
        data = {
            "id": task.id,
            "title": task.title,
            "level": task.level,
            "description": task.description,
            "tags": task.tags,
            "fixtures": task.fixtures,
            "ground_truth": {
                "checkpoints": [
                    {
                        "id": cp.id,
                        "type": cp.type,
                        "desc": cp.desc,
                        "path": cp.path,
                        "pattern": cp.pattern,
                        "cmd": cp.cmd,
                    }
                    for cp in task.checkpoints
                ]
            },
            "verifier": task.verifier,
            "weight": task.weight,
            "cost_budget_usd": task.cost_budget_usd,
            "timeout_s": task.timeout_s,
        }
        if task.max_steps:
            data["max_steps"] = task.max_steps
        if task.rubric:
            data["rubric"] = task.rubric
        if task.capabilities:
            data["capabilities"] = task.capabilities

        spec_path.write_text(
            yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        return spec_path
