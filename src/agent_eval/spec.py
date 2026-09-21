"""Task Spec 数据模型与加载（Day 1）。

Task Spec 是任务包与评测框架之间的契约：
- 任务作者编写 tasks/<id>/spec.yaml（字段见 docs/task-spec.md）
- 框架据此加载任务、执行后端 Agent、运行判定器
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

import yaml

from agent_eval.log import get_logger

logger = get_logger(__name__)

CheckpointType = Literal[
    "file_exists",
    "file_not_exists",
    "content_contains",
    "content_not_contains",
    "cmd_exit_zero",
    # M4：RPA/UI 操作评测的 checkpoint 类型
    "ui_element_exists",   # path=URL, pattern=CSS selector
    "browser_url_contains", # path=起始URL, pattern=期望URL包含的字符串
    "http_status",         # path=URL, pattern=状态码（如 "200"、"2"、"200-299"）
    # V3.8 P1：参数级 checkpoint——断言工具调用的参数值
    "tool_call_assert",    # tool=工具名, param=参数名, pattern=参数值正则（值域判断）
    # V4.3 P0：轨迹级 checkpoint——断言工具调用顺序与无死循环
    "tool_order_assert",   # tools=[工具1,工具2,...]，按顺序检查是否依次调用
    "no_loop_assert",      # max_consecutive=N，同一工具同一参数连续调用不超过N次
    # V4.4 P0：风险层 checkpoint——安全合规检测
    "no_sensitive_leak",   # 检测输出中是否泄露敏感信息（API Key、密码、PII）
    "no_path_escape",      # 检测工具调用是否尝试访问 workspace 外的路径（越权）
    "no_hallucinated_tool", # 检测是否调用了不存在的工具（幻觉检测）
]

LEVELS = {"L1", "L2", "L3", "L4", "L5"}
VERIFIERS = {"deterministic", "llm_judge"}
# HarnessDev 论文归纳的六类 Harness 控制能力
CAPABILITIES = {"execution", "tools", "context", "state", "lifecycle", "verification"}
# P1 改进：能力评测 vs 回归评测分离（对应 AliExpress 文章）
EVAL_TYPES = {"capability", "regression"}
EVAL_TYPE_LABELS = {
    "capability": "能力评测",
    "regression": "回归评测",
}
# V3.7：评测分层——校验点所属阶段（单元/链路/端到端三层评测体系）
CHECKPOINT_STAGES = {
    "retrieval",      # 检索/读取阶段：文件读取、信息获取、知识召回
    "reasoning",      # 推理/规划阶段：任务理解、步骤规划、逻辑判断
    "tool_use",       # 工具执行阶段：命令执行、文件写入、API调用
    "final_answer",   # 最终输出阶段：结果文件、报告生成、格式校验
    "integration",    # 链路集成阶段：多步骤配合、端到端流程
}
STAGE_LABELS = {
    "retrieval": "检索/读取",
    "reasoning": "推理/规划",
    "tool_use": "工具执行",
    "final_answer": "最终输出",
    "integration": "链路集成",
}


@dataclass
class Checkpoint:
    id: str
    type: CheckpointType
    desc: str = ""
    path: str = ""
    pattern: str = ""
    cmd: str = ""
    stage: str = "final_answer"  # V3.7：校验点所属评测阶段，默认最终输出
    tool: str = ""   # V3.8 P1：tool_call_assert 类型的工具名
    param: str = ""  # V3.8 P1：tool_call_assert 类型的参数名
    tools: list[str] = field(default_factory=list)  # V4.3 P0：tool_order_assert 类型的工具序列
    max_consecutive: int = 3  # V4.3 P0：no_loop_assert 类型的最大连续重复次数
    gate_mode: str = "blocking"  # V4.0 P2：渐进式规则状态（shadow只记录/warning提醒/blocking阻断）
    category: str = "outcome"  # V4.1 P1：成功标准三层分类（outcome业务结果/gate硬门禁/quality软质量）


@dataclass
class SkillSpec:
    """V3.8 P2：Skill 定义——多个 Tool 编排封装的能力包。

    与 Tool（最小原子能力）区分：Skill 测的是组合对不对（编排顺序、子工具完整性），
    Tool 测的是单步准不准（选择/参数/格式/结果使用/边界）。
    """
    id: str
    name: str
    desc: str = ""
    tools: list[str] = field(default_factory=list)  # 该技能包含的工具调用序列（有序）
    expected_order: bool = True  # 是否要求严格按顺序调用


@dataclass
class TaskSpec:
    id: str
    title: str
    level: str
    description: str
    fixtures: dict = field(default_factory=dict)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    verifier: str = "deterministic"
    weight: float = 1.0
    cost_budget_usd: float = 0.5
    timeout_s: int = 300
    max_steps: Optional[int] = None  # 可选：覆盖后端默认步数上限（如 L4 修复类任务提额）
    tags: list[str] = field(default_factory=list)
    rubric: str = ""  # V2.2：verifier=llm_judge 时的评分标准（任务作者自定义）
    capabilities: list[str] = field(default_factory=list)  # V2.5：任务考察的 Harness 能力（六类）
    mcp_servers: list[dict] = field(default_factory=list)  # M2：任务声明的 MCP server 列表（stdio 模式）
    tier: str = ""  # V3.1：数据集分层（golden/boundary/regression/random）
    risk_level: str = "P2"  # V3.9 P1：任务风险等级（P0=核心卡口必须100%通过 / P1=重要≥90% / P2=一般≥80%）
    risk_category: str = "normal"  # V3.9 P1：风险分类（normal正常/boundary边界/tool工具/hallucination幻觉/security安全）
    forbidden_tools: list[str] = field(default_factory=list)  # V3.8 P1：禁止调用的危险工具列表（调用即触发红线）
    skills: list[SkillSpec] = field(default_factory=list)  # V3.8 P2：Skill 定义（多个 Tool 编排封装的能力包）
    # V4.0 P2：Case Contract（运行前冻结的契约约束，参考 ODAR 文章 4.1 节）
    required_skills: list[str] = field(default_factory=list)  # 必需触发的 Skill 名称列表
    required_tools: list[str] = field(default_factory=list)  # 必需调用的工具名称列表
    output_contract: str = ""  # 输出契约描述（如 "json"、"markdown报告"、"csv文件"）
    scenario_type: str = "happy_path"  # V4.1 P1：测试集场景类型（happy_path正常/boundary边界/error_recovery异常恢复/adversarial对抗/off_topic离题诱导 V4.3 P2-2）
    eval_type: str = "regression"  # P1 改进：能力评测 vs 回归评测（capability能力测试要难/regression回归测试要稳）
    spec_path: Optional[Path] = None

    @classmethod
    def from_yaml(cls, path: Path) -> "TaskSpec":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        checkpoints = [
            Checkpoint(
                id=cp.get("id", ""),
                type=cp.get("type", ""),
                desc=cp.get("desc", ""),
                path=cp.get("path", ""),
                pattern=cp.get("pattern", ""),
                cmd=cp.get("cmd", ""),
                stage=cp.get("stage", "final_answer"),
                tool=cp.get("tool", ""),
                param=cp.get("param", ""),
                tools=cp.get("tools", []),
                max_consecutive=cp.get("max_consecutive", 3),
                gate_mode=cp.get("gate_mode", "blocking"),
                category=cp.get("category", "outcome"),
            )
            for cp in data.get("ground_truth", {}).get("checkpoints", [])
        ]
        spec = cls(
            id=data.get("id", path.parent.name),
            title=data.get("title", ""),
            level=data.get("level", "L1"),
            description=data.get("description", ""),
            fixtures=data.get("fixtures", {}),
            checkpoints=checkpoints,
            verifier=data.get("verifier", "deterministic"),
            weight=float(data.get("weight", 1.0)),
            cost_budget_usd=float(data.get("cost_budget_usd", 0.5)),
            timeout_s=int(data.get("timeout_s", 300)),
            max_steps=int(data["max_steps"]) if data.get("max_steps") else None,
            tags=list(data.get("tags", [])),
            rubric=str(data.get("rubric", "")),
            capabilities=list(data.get("capabilities", [])),
            mcp_servers=list(data.get("mcp_servers", [])),
            tier=str(data.get("tier", "")),
            risk_level=str(data.get("risk_level", "P2")),
            risk_category=str(data.get("risk_category", "normal")),
            forbidden_tools=list(data.get("forbidden_tools", [])),
            required_skills=list(data.get("required_skills", [])),
            required_tools=list(data.get("required_tools", [])),
            output_contract=str(data.get("output_contract", "")),
            scenario_type=str(data.get("scenario_type", "happy_path")),
            eval_type=str(data.get("eval_type", "regression")),  # P1 改进
            skills=[
                SkillSpec(
                    id=s.get("id", ""),
                    name=s.get("name", ""),
                    desc=s.get("desc", ""),
                    tools=list(s.get("tools", [])),
                    expected_order=s.get("expected_order", True),
                )
                for s in data.get("skills", [])
            ],
            spec_path=path,
        )
        errors = spec.validate()
        if errors:
            raise ValueError(f"任务 {spec.id} spec 校验失败: {'; '.join(errors)}")
        return spec

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.id:
            errors.append("缺少 id")
        if not self.title:
            errors.append("缺少 title")
        if self.level not in LEVELS:
            errors.append(f"level 必须为 {sorted(LEVELS)} 之一，当前: {self.level}")
        if self.verifier not in VERIFIERS:
            errors.append(
                f"verifier 必须为 {sorted(VERIFIERS)} 之一，当前: {self.verifier}"
            )
        # P0 改进：坑二修复 — checkpoint schema 校验
        # 没有任何校验点的任务无法判定通过/失败，应标记为配置异常
        if not self.checkpoints:
            errors.append("任务缺少任何校验点（checkpoints 为空），无法判定通过/失败")
        for cp in self.checkpoints:
            if not cp.id:
                errors.append("存在缺少 id 的校验点")
            if cp.type not in CheckpointType.__args__:
                errors.append(f"校验点 {cp.id} 类型非法: {cp.type}")
            if cp.stage not in CHECKPOINT_STAGES:
                errors.append(f"校验点 {cp.id} stage 非法: {cp.stage}，必须为 {sorted(CHECKPOINT_STAGES)} 之一")
            # 按类型检查必填字段（防止 expected/path 为空导致静默判 FAIL）
            if cp.type == "file_exists" and not cp.path:
                errors.append(f"校验点 {cp.id} (file_exists) 缺少 path 字段")
            if cp.type == "file_not_exists" and not cp.path:
                errors.append(f"校验点 {cp.id} (file_not_exists) 缺少 path 字段")
            if cp.type == "content_contains" and (not cp.path or not cp.pattern):
                errors.append(f"校验点 {cp.id} (content_contains) 缺少 path 或 pattern 字段")
            if cp.type == "content_not_contains" and (not cp.path or not cp.pattern):
                errors.append(f"校验点 {cp.id} (content_not_contains) 缺少 path 或 pattern 字段")
            if cp.type == "cmd_exit_zero" and not cp.cmd:
                errors.append(f"校验点 {cp.id} (cmd_exit_zero) 缺少 cmd 字段")
        for cap in self.capabilities:
            if cap not in CAPABILITIES:
                errors.append(f"capabilities 包含非法值 '{cap}'，必须为 {sorted(CAPABILITIES)} 之一")
        # P1 改进：校验 eval_type
        if self.eval_type not in EVAL_TYPES:
            errors.append(f"eval_type 必须为 {sorted(EVAL_TYPES)} 之一，当前: {self.eval_type}")
        return errors

    def fixtures_dir(self) -> Path:
        """fixtures 绝对目录（tasks/<id>/fixtures）。"""
        assert self.spec_path is not None
        return self.spec_path.parent / "fixtures"


def load_manifest(tasks_dir: Path) -> dict:
    path = tasks_dir / "manifest.yaml"
    if not path.exists():
        raise FileNotFoundError(f"找不到任务包清单: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_task_pack(tasks_dir: Path) -> list[TaskSpec]:
    """加载 tasks_dir 下 manifest 声明的全部任务。

    支持 manifest includes 字段：引用已安装的任务包，自动合并包内任务。
    本地任务与 includes 任务 ID 冲突时，本地任务优先。

    V3.1：支持 tasks 列表的两种格式：
    - 旧格式：["T001", "T002", ...]（字符串列表）
    - 新格式：[{"id": "T001", "tier": "golden"}, ...]（字典列表，含 tier 分层）
    """
    from agent_eval.taskpack import load_included_tasks

    manifest = load_manifest(tasks_dir)
    tasks: list[TaskSpec] = []
    seen_ids: set[str] = set()

    # 先加载本地任务（兼容新旧两种格式）
    raw_tasks = manifest.get("tasks", [])
    for item in raw_tasks:
        # 新格式：{"id": "T001", "tier": "golden"}
        if isinstance(item, dict):
            task_id = item.get("id")
            tier = item.get("tier")
        # 旧格式："T001"
        else:
            task_id = item
            tier = None
        if not task_id:
            continue
        spec_path = tasks_dir / task_id / "spec.yaml"
        if not spec_path.exists():
            raise FileNotFoundError(f"任务 {task_id} 缺少 spec.yaml: {spec_path}")
        spec = TaskSpec.from_yaml(spec_path)
        # tier 优先从 manifest 读取，其次从 spec.yaml 读取
        if tier and not getattr(spec, "tier", None):
            spec.tier = tier
        tasks.append(spec)
        seen_ids.add(task_id)

    # 再加载 includes 引用的任务包（跳过 ID 冲突的）
    includes = manifest.get("includes", [])
    if includes:
        for task_id, spec_path in load_included_tasks(includes):
            if task_id in seen_ids:
                logger.warning(f"任务 ID 冲突，本地任务优先: {task_id}（跳过任务包中的同名任务）")
                continue
            tasks.append(TaskSpec.from_yaml(spec_path))
            seen_ids.add(task_id)

    return tasks


def find_tasks_dir() -> Path:
    """定位任务包目录：优先环境变量 AGENT_EVAL_TASKS，其次当前目录，最后包默认位置。"""
    env = os.environ.get("AGENT_EVAL_TASKS")
    if env:
        return Path(env)
    cwd = Path.cwd() / "tasks"
    if cwd.is_dir():
        return cwd
    return Path(__file__).resolve().parents[2] / "tasks"
