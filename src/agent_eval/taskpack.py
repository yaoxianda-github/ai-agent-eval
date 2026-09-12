"""任务包市场（M3）。

任务包是可发布、可安装、可版本管理的评测任务集合。
借鉴 skillhub / npm 的包管理思路，把 tasks/ 从静态目录变成可扩展的包生态。

任务包格式：
  <package>/
    package.yaml      # 包元数据（name/version/author/description/tasks）
    tasks/            # 任务目录（每个任务一个子目录，含 spec.yaml + fixtures/）

安装位置：
  - 用户级：~/.agent-eval/packages/<name>/
  - 项目级：<project>/packages/<name>/

manifest.yaml 支持 includes 字段引用已安装的任务包：
  includes:
    - swe-bench-lite
    - gaia-benchmark
  tasks: [T001, T002, ...]  # 本地任务（与 includes 合并）
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from agent_eval.log import get_logger

logger = get_logger(__name__)

# 任务包安装根目录（用户级）
DEFAULT_PACKAGES_DIR = Path.home() / ".agent-eval" / "packages"


@dataclass
class TaskPackage:
    """已安装的任务包元数据。"""
    name: str
    version: str
    author: str = ""
    description: str = ""
    license: str = ""
    tasks: list[str] = field(default_factory=list)  # 包内任务 ID 列表
    install_path: Optional[Path] = None  # 安装路径

    @classmethod
    def from_package_yaml(cls, path: Path) -> "TaskPackage":
        """从 package.yaml 加载任务包元数据。"""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # 自动扫描 tasks/ 目录（如果 package.yaml 没声明 tasks）
        tasks = data.get("tasks", [])
        if not tasks:
            tasks_dir = path.parent / "tasks"
            if tasks_dir.is_dir():
                tasks = sorted([
                    d.name for d in tasks_dir.iterdir()
                    if d.is_dir() and (d / "spec.yaml").exists()
                ])
        return cls(
            name=data.get("name", path.parent.name),
            version=data.get("version", "0.0.0"),
            author=data.get("author", ""),
            description=data.get("description", ""),
            license=data.get("license", ""),
            tasks=tasks,
            install_path=path.parent,
        )


def get_packages_dir(project_dir: Optional[Path] = None) -> Path:
    """获取任务包安装目录。

    优先级：环境变量 AGENT_EVAL_PACKAGES > 项目级 packages/ > 用户级 ~/.agent-eval/packages/
    """
    env = os.environ.get("AGENT_EVAL_PACKAGES")
    if env:
        return Path(env)
    if project_dir:
        project_packages = project_dir / "packages"
        if project_packages.is_dir():
            return project_packages
    return DEFAULT_PACKAGES_DIR


def list_packages(packages_dir: Optional[Path] = None) -> list[TaskPackage]:
    """列出已安装的所有任务包。"""
    packages_dir = packages_dir or get_packages_dir()
    if not packages_dir.is_dir():
        return []
    packages = []
    for pkg_dir in sorted(packages_dir.iterdir()):
        if pkg_dir.is_dir():
            package_yaml = pkg_dir / "package.yaml"
            if package_yaml.exists():
                try:
                    packages.append(TaskPackage.from_package_yaml(package_yaml))
                except Exception as e:
                    logger.warning(f"加载任务包 {pkg_dir.name} 失败: {e}")
    return packages


def get_package(name: str, packages_dir: Optional[Path] = None) -> Optional[TaskPackage]:
    """按名称获取已安装的任务包。"""
    packages_dir = packages_dir or get_packages_dir()
    pkg_dir = packages_dir / name
    package_yaml = pkg_dir / "package.yaml"
    if package_yaml.exists():
        return TaskPackage.from_package_yaml(package_yaml)
    return None


def install_package(
    source: str,
    packages_dir: Optional[Path] = None,
    name: Optional[str] = None,
) -> TaskPackage:
    """安装任务包。

    source 可以是：
    - git 仓库 URL（https://github.com/... 或 git@github.com:...）
    - 本地目录路径

    Args:
        source: 任务包来源（git URL 或本地路径）
        packages_dir: 安装目录（默认用户级）
        name: 覆盖包名（默认从 package.yaml 读取）

    Returns:
        安装后的 TaskPackage
    """
    packages_dir = packages_dir or get_packages_dir()
    packages_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(source).expanduser().resolve()

    if source.startswith("http://") or source.startswith("https://") or source.startswith("git@"):
        # git 仓库：clone 到临时目录，然后复制到 packages_dir
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / "repo"
            logger.info(f"Cloning 任务包: {source}")
            result = subprocess.run(
                ["git", "clone", "--depth", "1", source, str(tmp_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"git clone 失败: {result.stderr}")
            # 读取 package.yaml 获取包名
            package_yaml = tmp_path / "package.yaml"
            if not package_yaml.exists():
                raise FileNotFoundError(f"任务包缺少 package.yaml: {package_yaml}")
            pkg = TaskPackage.from_package_yaml(package_yaml)
            pkg_name = name or pkg.name
            dest = packages_dir / pkg_name
            if dest.exists():
                logger.info(f"任务包 {pkg_name} 已存在，覆盖安装")
                shutil.rmtree(dest)
            shutil.copytree(tmp_path, dest)
            pkg.install_path = dest
            logger.info(f"任务包安装成功: {pkg_name} v{pkg.version} ({len(pkg.tasks)} 个任务)")
            return pkg
    elif source_path.is_dir():
        # 本地目录：直接复制
        package_yaml = source_path / "package.yaml"
        if not package_yaml.exists():
            raise FileNotFoundError(f"任务包缺少 package.yaml: {package_yaml}")
        pkg = TaskPackage.from_package_yaml(package_yaml)
        pkg_name = name or pkg.name
        dest = packages_dir / pkg_name
        if dest.exists():
            logger.info(f"任务包 {pkg_name} 已存在，覆盖安装")
            shutil.rmtree(dest)
        shutil.copytree(source_path, dest)
        pkg.install_path = dest
        logger.info(f"任务包安装成功: {pkg_name} v{pkg.version} ({len(pkg.tasks)} 个任务)")
        return pkg
    else:
        raise ValueError(f"无效的任务包来源: {source}（必须是 git URL 或本地目录）")


def remove_package(name: str, packages_dir: Optional[Path] = None) -> bool:
    """卸载任务包。

    Returns:
        True 如果成功删除，False 如果任务包不存在
    """
    packages_dir = packages_dir or get_packages_dir()
    pkg_dir = packages_dir / name
    if not pkg_dir.is_dir():
        return False
    shutil.rmtree(pkg_dir)
    logger.info(f"任务包已卸载: {name}")
    return True


def load_included_tasks(
    includes: list[str],
    packages_dir: Optional[Path] = None,
) -> list[tuple[str, Path]]:
    """加载 manifest includes 引用的任务包中的任务。

    Args:
        includes: 任务包名称列表
        packages_dir: 任务包安装目录

    Returns:
        list of (task_id, spec_path) 元组
    """
    packages_dir = packages_dir or get_packages_dir()
    tasks: list[tuple[str, Path]] = []
    for pkg_name in includes:
        pkg = get_package(pkg_name, packages_dir)
        if pkg is None:
            logger.warning(f"manifest includes 引用了未安装的任务包: {pkg_name}（跳过）")
            continue
        if not pkg.install_path:
            continue
        tasks_dir = pkg.install_path / "tasks"
        for task_id in pkg.tasks:
            spec_path = tasks_dir / task_id / "spec.yaml"
            if spec_path.exists():
                tasks.append((task_id, spec_path))
            else:
                logger.warning(f"任务包 {pkg_name} 中任务 {task_id} 缺少 spec.yaml（跳过）")
    return tasks
