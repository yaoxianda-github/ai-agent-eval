#!/bin/bash
# agent-eval workbench 启动脚本（供 launchd 调用）
# 1. 清理占用 8000 端口的旧进程
# 2. 启动 workbench 服务

set -e

PROJECT_DIR="/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval"
VENV_PYTHON="${PROJECT_DIR}/.venv/bin/python"
AGENT_EVAL="${PROJECT_DIR}/.venv/bin/agent-eval"
PORT=8000
LOG_DIR="${PROJECT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] workbench 守护进程启动" >> "${LOG_DIR}/workbench-daemon.log"

# 清理占用 8000 端口的旧进程（避免 address already in use）
OLD_PID=$(lsof -ti tcp:${PORT} 2>/dev/null || true)
if [ -n "${OLD_PID}" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 清理占用端口 ${PORT} 的旧进程: ${OLD_PID}" >> "${LOG_DIR}/workbench-daemon.log"
    kill -9 ${OLD_PID} 2>/dev/null || true
    sleep 1
fi

# 加载用户 shell 环境（确保 DEEPSEEK_API_KEY 等环境变量可用）
if [ -f "${HOME}/.zshrc" ]; then
    source "${HOME}/.zshrc" 2>/dev/null || true
fi

cd "${PROJECT_DIR}"

# 启动 workbench（前台运行，launchd 监控）
exec "${AGENT_EVAL}" workbench
