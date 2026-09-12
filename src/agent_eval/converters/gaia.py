"""GAIA 评测集转换器（M1 扩展）。

GAIA（General AI Assistants）是通用 AI 助手评测基准，包含需要推理、
多模态理解、工具使用等能力的任务。数据格式为 JSON/JSONL，字段包括：
- question: 问题描述
- level: 难度级别（1/2/3）
- answer: 最终答案
- file_name: 附件文件名（可选）
- tools: 需要使用的工具（可选，如 "Internet", "Python", "File"）

数据源：https://huggingface.co/datasets/gaia-benchmark/GAIA

转换逻辑：
- question → description
- level → 映射到 L2/L3/L4
- answer → 写入 rubric（供 LLM judge 参考），短答案同时用 content_contains 验证
- file_name → fixtures 声明（附件需用户自行下载放入 fixtures/）
- tools → capabilities 推断
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from agent_eval.converters.base import BaseConverter
from agent_eval.log import get_logger
from agent_eval.spec import Checkpoint, TaskSpec

logger = get_logger(__name__)


class GAIAConverter(BaseConverter):
    """GAIA 评测集 → spec.yaml 转换器。"""

    name = "gaia"

    # GAIA level → ai-agent-eval level 映射
    LEVEL_MAP = {1: "L2", 2: "L3", 3: "L4"}

    # GAIA level → 超时时间（秒）
    TIMEOUT_MAP = {1: 120, 2: 300, 3: 600}

    # GAIA level → 最大步数
    MAX_STEPS_MAP = {1: 20, 2: 40, 3: 60}

    # GAIA level → 任务权重
    WEIGHT_MAP = {1: 1.0, 2: 1.5, 3: 2.0}

    # GAIA tools → ai-agent-eval capabilities 映射
    TOOLS_CAPABILITIES_MAP = {
        "Internet": "tools",
        "Python": "execution",
        "File": "tools",
        "Search": "tools",
        "Calculator": "tools",
        "Code interpreter": "execution",
    }

    def convert(
        self,
        source: Path,
        output_dir: Path,
        limit: int = 0,
        start_index: int = 0,
        id_prefix: str = "GA",
        **kwargs,
    ) -> list[TaskSpec]:
        """读取 GAIA JSON/JSONL 文件，转换为 spec.yaml 任务。

        Args:
            source: GAIA 数据文件路径（.json 或 .jsonl）
            output_dir: 输出目录，在此创建 tasks/<id>/spec.yaml
            limit: 转换数量上限，0 表示全部
            start_index: 起始任务序号（用于分批转换）
            id_prefix: 任务 ID 前缀，默认 "GA"

        Returns:
            转换后的 TaskSpec 列表
        """
        instances = self._load_instances(source)
        logger.info("加载 GAIA 实例: %d 个（来源: %s）", len(instances), source)

        if limit > 0:
            instances = instances[start_index : start_index + limit]
        elif start_index > 0:
            instances = instances[start_index:]

        tasks: list[TaskSpec] = []
        for i, inst in enumerate(instances):
            task_id = f"{id_prefix}{start_index + i:03d}"
            try:
                task = self._convert_instance(task_id, inst)
                self._write_spec(task, output_dir)
                tasks.append(task)
                logger.debug("转换成功: %s", task_id)
            except Exception as e:
                logger.warning("转换失败 %s: %s", task_id, e)

        logger.info("转换完成: %d/%d 个任务", len(tasks), len(instances))
        return tasks

    def _load_instances(self, source: Path) -> list[dict]:
        """加载 GAIA 数据文件（支持 JSON 数组和 JSONL 格式）。"""
        text = source.read_text(encoding="utf-8")
        if source.suffix == ".jsonl":
            instances = []
            for line in text.strip().splitlines():
                line = line.strip()
                if line:
                    instances.append(json.loads(line))
            return instances
        else:
            data = json.loads(text)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "data" in data:
                return data["data"]
            else:
                return [data]

    def _convert_instance(self, task_id: str, inst: dict) -> TaskSpec:
        """将单个 GAIA 实例转换为 TaskSpec。"""
        question = inst.get("question", "").strip()
        level = int(inst.get("level", 1))
        answer = str(inst.get("answer", "")).strip()
        file_name = inst.get("file_name", "") or ""
        tools = inst.get("tools", []) or []

        # 确保 tools 是 list
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]

        # 映射 level
        eval_level = self.LEVEL_MAP.get(level, "L3")
        timeout_s = self.TIMEOUT_MAP.get(level, 300)
        max_steps = self.MAX_STEPS_MAP.get(level, 40)
        weight = self.WEIGHT_MAP.get(level, 1.5)

        # 构造标题（取 question 前 60 字符）
        title = question[:60] + ("..." if len(question) > 60 else "")

        # 构造描述（包含问题和附件说明）
        description = question
        if file_name:
            description += f"\n\n附件文件: {file_name}（请从 fixtures/ 目录读取）"

        # 构造 checkpoints
        checkpoints = self._build_checkpoints(answer)

        # 构造 capabilities
        capabilities = self._infer_capabilities(tools, file_name)

        # 构造 tags
        tags = ["gaia", "general", "reasoning", f"level-{level}"]
        if file_name:
            tags.append("file-attachment")
        if tools:
            tags.append("tools-required")

        # 构造 fixtures（如果有附件）
        fixtures = {}
        if file_name:
            fixtures = {"source": "fixtures/"}

        # 构造 rubric（供 LLM judge 参考）
        rubric = ""
        if answer:
            rubric = (
                f"任务的正确答案是: \"{answer}\"\n"
                f"请判断 Agent 写入 answer.txt 的内容是否与正确答案语义一致。\n"
                f"注意：答案可能有多种等价表达形式（如数字格式、单位、大小写等），"
                f"只要语义正确即可判定为通过。"
            )

        # 决定 verifier：短答案（< 100 字符）用 deterministic + content_contains，
        # 长答案用 llm_judge
        verifier = "deterministic" if len(answer) < 100 else "llm_judge"

        return TaskSpec(
            id=task_id,
            title=title,
            level=eval_level,
            description=description,
            fixtures=fixtures,
            checkpoints=checkpoints,
            verifier=verifier,
            weight=weight,
            cost_budget_usd=round(0.1 * level, 2),
            timeout_s=timeout_s,
            max_steps=max_steps,
            tags=tags,
            rubric=rubric,
            capabilities=capabilities,
        )

    def _build_checkpoints(self, answer: str) -> list[Checkpoint]:
        """构造 checkpoint 列表。

        策略：
        - c1: file_exists - Agent 必须写入 answer.txt
        - c2: content_contains - answer.txt 包含正确答案（短答案时）
        - 长答案时 c2 仍用 file_exists，实际验证由 llm_judge 完成
        """
        checkpoints = [
            Checkpoint(
                id="c1",
                type="file_exists",
                desc="Agent 将最终答案写入 answer.txt",
                path="answer.txt",
            ),
        ]

        # 短答案（< 100 字符）可以用 content_contains 做确定性验证
        if answer and len(answer) < 100:
            # 对答案做简单的归一化处理（去除首尾空格，转小写）
            normalized = answer.strip().lower()
            checkpoints.append(
                Checkpoint(
                    id="c2",
                    type="content_contains",
                    desc=f"answer.txt 包含正确答案（不区分大小写）",
                    path="answer.txt",
                    pattern=normalized,
                )
            )

        return checkpoints

    def _infer_capabilities(self, tools: list[str], file_name: str) -> list[str]:
        """根据 GAIA tools 字段推断 ai-agent-eval capabilities。"""
        caps = set()
        for tool in tools:
            cap = self.TOOLS_CAPABILITIES_MAP.get(tool)
            if cap:
                caps.add(cap)
        # 有附件时需要文件处理能力
        if file_name:
            caps.add("tools")
        # GAIA 任务通常需要上下文理解
        caps.add("context")
        return sorted(caps)
