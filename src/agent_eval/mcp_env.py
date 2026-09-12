"""MCP 工具环境管理器（M2）。

任务 spec 可声明 mcp_servers 字段，评测执行前由本模块：
1. 生成 MCP 配置文件（通用 JSON 格式，兼容 Claude Desktop / 各类 MCP client）
2. 通过环境变量 MCP_CONFIG_FILE 告知后端 Agent 配置路径
3. 可选：预先启动 server 做健康检查（验证 command 可执行）
4. 执行完毕后清理配置文件和临时进程

配置文件格式：
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": {"KEY": "value"}
    }
  }
}
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_eval.log import get_logger

logger = get_logger(__name__)


@dataclass
class MCPServerConfig:
    """单个 MCP server 的配置。"""
    name: str
    command: str  # 完整命令行，如 "npx -y @modelcontextprotocol/server-filesystem /tmp"
    env: dict[str, str] = field(default_factory=dict)

    def to_mcp_json(self) -> dict:
        """转换为 MCP 配置文件格式。"""
        parts = shlex.split(self.command)
        if not parts:
            raise ValueError(f"MCP server '{self.name}' command 为空")
        return {
            "command": parts[0],
            "args": parts[1:],
            "env": self.env if self.env else {},
        }


@dataclass
class MCPEnvironment:
    """MCP 环境管理器。

    用法：
        env = MCPEnvironment(spec.mcp_servers, workspace)
        env.prepare()  # 生成配置文件，设置环境变量
        try:
            # ... 执行 backend ...
        finally:
            env.cleanup()
    """
    servers: list[dict]  # 来自 spec.mcp_servers
    workspace: Path  # 工作目录，配置文件放在这里
    config_path: Optional[Path] = None
    _server_processes: list[subprocess.Popen] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._server_configs = [
            MCPServerConfig(
                name=s.get("name", f"mcp-{i}"),
                command=s.get("command", ""),
                env=s.get("env", {}),
            )
            for i, s in enumerate(self.servers)
        ]

    @property
    def has_servers(self) -> bool:
        return len(self._server_configs) > 0

    def prepare(self) -> dict[str, str]:
        """准备 MCP 环境：生成配置文件，返回需要设置的环境变量。

        Returns:
            需要注入到 backend 进程的环境变量 dict
        """
        if not self.has_servers:
            return {}

        # 生成配置文件
        config = {"mcpServers": {}}
        for srv in self._server_configs:
            config["mcpServers"][srv.name] = srv.to_mcp_json()

        self.config_path = self.workspace / "mcp_config.json"
        self.config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"MCP 配置文件已生成: {self.config_path}（{len(self._server_configs)} 个 server）")

        # 返回环境变量
        env = {
            "MCP_CONFIG_FILE": str(self.config_path),
            "MCP_CONFIG_PATH": str(self.config_path),  # 兼容部分 client
        }
        return env

    def health_check(self, timeout: float = 10.0) -> list[tuple[str, bool, str]]:
        """预先启动所有 MCP server 做健康检查（验证 command 可执行）。

        启动后发送 initialize 请求，等待响应，然后关闭。
        返回 [(name, ok, detail), ...]

        注意：这只是验证 server 能否启动，不保持运行。
        实际执行时由 backend 自己启动 server。
        """
        if not self.has_servers:
            return []

        results = []
        for srv in self._server_configs:
            try:
                parts = shlex.split(srv.command)
                proc = subprocess.Popen(
                    parts,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, **srv.env},
                    cwd=str(self.workspace),
                )
                # 发送 initialize 请求
                init_msg = json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "ai-agent-eval", "version": "1.0"},
                    },
                }) + "\n"
                try:
                    stdout, stderr = proc.communicate(
                        input=init_msg.encode("utf-8"),
                        timeout=timeout,
                    )
                    ok = b'"jsonrpc"' in stdout or b'"result"' in stdout
                    detail = "initialize 响应正常" if ok else f"响应异常: {stdout[:200]!r}"
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    ok = False
                    detail = f"启动超时（{timeout}s）"
                except Exception as e:
                    ok = False
                    detail = f"通信错误: {e}"

                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()

                results.append((srv.name, ok, detail))
                logger.info(f"MCP 健康检查 [{srv.name}]: {'OK' if ok else 'FAIL'} - {detail}")

            except FileNotFoundError:
                results.append((srv.name, False, f"命令不存在: {srv.command}"))
            except Exception as e:
                results.append((srv.name, False, f"启动失败: {e}"))

        return results

    def cleanup(self) -> None:
        """清理 MCP 环境：删除配置文件，关闭残留进程。"""
        # 关闭残留进程
        for proc in self._server_processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._server_processes.clear()

        # 删除配置文件
        if self.config_path and self.config_path.exists():
            try:
                self.config_path.unlink()
                logger.info(f"MCP 配置文件已清理: {self.config_path}")
            except OSError:
                pass
            self.config_path = None


def parse_mcp_servers(servers_data: list[dict]) -> list[MCPServerConfig]:
    """从 spec 的 mcp_servers 字段解析为 MCPServerConfig 列表。"""
    return [
        MCPServerConfig(
            name=s.get("name", f"mcp-{i}"),
            command=s.get("command", ""),
            env=s.get("env", {}),
        )
        for i, s in enumerate(servers_data)
    ]
