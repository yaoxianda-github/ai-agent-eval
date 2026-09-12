"""SWE-bench 转换器（M1）。

将 SWE-bench / SWE-bench-Lite 的 JSON 数据转换为 ai-agent-eval spec.yaml。

SWE-bench 数据格式（每个 instance）：
- instance_id: "django__django-11099"
- repo: "django/django"
- base_commit: commit hash
- problem_statement: 问题描述（Markdown）
- FAIL_TO_PASS: 必须从失败变通过的测试列表
- PASS_TO_PASS: 必须保持通过的测试列表
- test_patch: 测试补丁（需要应用到仓库）
- patch: 黄金修复补丁

转换策略：
- fixtures/setup.sh: clone 仓库 → checkout base_commit → 应用 test_patch
- checkpoint c1: cmd_exit_zero，运行 FAIL_TO_PASS 测试（修复后应全部通过）
- checkpoint c2: cmd_exit_zero，运行 PASS_TO_PASS 抽样（回归测试，应保持通过）
- level: L3-L4（代码修复任务）
- tags: [code, swe_bench, repo, python]
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from agent_eval.converters.base import BaseConverter
from agent_eval.spec import Checkpoint, TaskSpec


class SWEBenchConverter(BaseConverter):
    name = "swe-bench"

    def convert(
        self,
        source: Path,
        output_dir: Path,
        *,
        limit: int = 0,
        start_index: int = 0,
        id_prefix: str = "SW",
        include_pass_to_pass: bool = True,
        pass_to_pass_sample: int = 5,
        clone_repo: bool = False,
    ) -> list[TaskSpec]:
        """转换 SWE-bench JSON 为 spec.yaml。

        Args:
            source: SWE-bench JSON 文件路径（可以是 JSON 数组或 JSONL）
            output_dir: 输出目录
            limit: 转换数量上限，0 表示全部
            start_index: 起始序号（用于分批转换）
            id_prefix: 任务 ID 前缀
            include_pass_to_pass: 是否包含 PASS_TO_PASS 回归测试 checkpoint
            pass_to_pass_sample: PASS_TO_PASS 抽样数量（避免测试命令过长）
            clone_repo: 是否在转换时 clone 仓库到 fixtures（默认关闭，用 setup.sh 运行时准备）
        """
        instances = self._load_instances(source)
        if limit > 0:
            instances = instances[:limit]

        tasks: list[TaskSpec] = []
        for i, inst in enumerate(instances):
            idx = start_index + i
            task_id = f"{id_prefix}{idx:03d}"
            try:
                task = self._convert_instance(
                    inst, task_id,
                    include_pass_to_pass=include_pass_to_pass,
                    pass_to_pass_sample=pass_to_pass_sample,
                    clone_repo=clone_repo,
                    output_dir=output_dir,
                )
                self._write_spec(task, output_dir)
                tasks.append(task)
            except Exception as e:
                print(f"[WARN] 转换 {inst.get('instance_id', '?')} 失败: {e}")
                continue

        return tasks

    def _load_instances(self, source: Path) -> list[dict]:
        """加载 SWE-bench 数据，支持 JSON 数组和 JSONL。"""
        text = source.read_text(encoding="utf-8")
        if source.suffix == ".jsonl":
            return [json.loads(line) for line in text.strip().split("\n") if line.strip()]
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "instances" in data:
            return data["instances"]
        return [data]

    def _convert_instance(
        self,
        inst: dict,
        task_id: str,
        *,
        include_pass_to_pass: bool,
        pass_to_pass_sample: int,
        clone_repo: bool,
        output_dir: Path,
    ) -> TaskSpec:
        """转换单个 SWE-bench instance 为 TaskSpec。"""
        instance_id = inst.get("instance_id", "unknown")
        repo = inst.get("repo", "")
        base_commit = inst.get("base_commit", "")
        problem = inst.get("problem_statement", "").strip()
        fail_to_pass = inst.get("FAIL_TO_PASS", [])
        pass_to_pass = inst.get("PASS_TO_PASS", [])
        test_patch = inst.get("test_patch", "")

        # 标题：取 problem 的第一行，截断
        title = problem.split("\n")[0][:80] if problem else f"SWE-bench {instance_id}"

        # 描述：完整 problem_statement + 元信息
        description = (
            f"{problem}\n\n"
            f"---\n"
            f"来源: SWE-bench ({instance_id})\n"
            f"仓库: {repo} @ {base_commit[:12]}\n"
            f"必须通过的测试: {len(fail_to_pass)} 个\n"
            f"回归测试: {len(pass_to_pass)} 个"
        )

        # 难度估算：根据测试数量和仓库大小
        level = "L3"
        if len(fail_to_pass) >= 5 or len(pass_to_pass) >= 50:
            level = "L4"

        # checkpoints
        checkpoints: list[Checkpoint] = []

        # c1: 仓库已准备（setup.sh 执行成功）
        checkpoints.append(Checkpoint(
            id="c1",
            type="cmd_exit_zero",
            desc="仓库环境准备完成（clone + checkout + 应用测试补丁）",
            cmd="bash fixtures/setup.sh",
        ))

        # c2: FAIL_TO_PASS 测试全部通过
        if fail_to_pass:
            test_cmd = self._build_pytest_cmd(fail_to_pass, repo)
            checkpoints.append(Checkpoint(
                id="c2",
                type="cmd_exit_zero",
                desc=f"目标测试全部通过（{len(fail_to_pass)} 个 FAIL_TO_PASS）",
                cmd=test_cmd,
            ))

        # c3: PASS_TO_PASS 抽样回归测试
        if include_pass_to_pass and pass_to_pass:
            sampled = pass_to_pass[:pass_to_pass_sample]
            reg_cmd = self._build_pytest_cmd(sampled, repo)
            checkpoints.append(Checkpoint(
                id="c3",
                type="cmd_exit_zero",
                desc=f"回归测试通过（抽样 {len(sampled)}/{len(pass_to_pass)} 个 PASS_TO_PASS）",
                cmd=reg_cmd,
            ))

        # 创建 fixtures/setup.sh
        self._write_setup_sh(output_dir / task_id, repo, base_commit, test_patch, clone_repo)

        return TaskSpec(
            id=task_id,
            title=title,
            level=level,
            description=description,
            fixtures={"source": "fixtures/"},
            checkpoints=checkpoints,
            verifier="deterministic",
            weight=1.5 if level == "L4" else 1.0,
            cost_budget_usd=0.3,
            timeout_s=600,
            max_steps=50,
            tags=["code", "swe_bench", "repo", "python", "issue_fix"],
            capabilities=["execution", "tools", "context", "state"],
            spec_path=output_dir / task_id / "spec.yaml",
        )

    def _build_pytest_cmd(self, test_list: list[str], repo: str) -> str:
        """构建 pytest 命令。"""
        # 测试路径可能是 "tests/test_x.py::Class::method" 或 "django/tests/test_x.py"
        # 需要 cd 到 repo 目录运行
        tests = " ".join(f'"{t}"' for t in test_list)
        # 用 ; 而不是 &&，因为 setup.sh 已经在 c1 执行过了
        # 但为了保险，还是检查 repo 目录存在
        repo_dir = self._repo_dir_name(repo)
        return f"cd {repo_dir} && python -m pytest {tests} -x --tb=short -q"

    def _repo_dir_name(self, repo: str) -> str:
        """从 repo 路径提取目录名，如 django/django → django。"""
        return repo.split("/")[-1] if "/" in repo else repo

    def _write_setup_sh(
        self,
        task_dir: Path,
        repo: str,
        base_commit: str,
        test_patch: str,
        clone_repo: bool,
    ) -> None:
        """写入 fixtures/setup.sh。"""
        fixtures_dir = task_dir / "fixtures"
        fixtures_dir.mkdir(parents=True, exist_ok=True)

        repo_dir = self._repo_dir_name(repo)
        repo_url = f"https://github.com/{repo}.git"

        # 如果 test_patch 非空，写入测试补丁文件
        patch_file = ""
        patch_apply_block = "# 无测试补丁"
        if test_patch:
            patch_path = fixtures_dir / "test_patch.diff"
            patch_path.write_text(test_patch, encoding="utf-8")
            patch_file = "test_patch.diff"
            patch_apply_block = (
                f'if [ -f "../fixtures/{patch_file}" ]; then\n'
                f'    echo "Applying test patch..."\n'
                f'    git apply "../fixtures/{patch_file}" || echo "测试补丁应用失败，继续"\n'
                f"fi"
            )

        script = f"""#!/bin/bash
# SWE-bench 环境准备脚本
# 仓库: {repo} @ {base_commit[:12]}
set -e

REPO_DIR="{repo_dir}"
REPO_URL="{repo_url}"
BASE_COMMIT="{base_commit}"

if [ -d "$REPO_DIR" ]; then
    echo "仓库已存在，跳过 clone"
    cd "$REPO_DIR"
    git fetch --depth=1 origin "$BASE_COMMIT" 2>/dev/null || true
    git checkout "$BASE_COMMIT" 2>/dev/null || git checkout -f "$BASE_COMMIT"
else
    echo "Cloning $REPO_URL ..."
    git clone --depth=50 "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
    git checkout "$BASE_COMMIT"
fi

# 应用测试补丁
{patch_apply_block}

echo "环境准备完成"
"""
        setup_path = fixtures_dir / "setup.sh"
        setup_path.write_text(script, encoding="utf-8")
        setup_path.chmod(0o755)
