#!/bin/bash
# workbench 守护循环（被 daemon.sh 用 nohup 启动）
# 职责：监控 workbench 进程，崩溃后自动重启

PROJECT_DIR="/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval"
AGENT_EVAL="${PROJECT_DIR}/.venv/bin/agent-eval"
PORT=8000
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${LOG_DIR}/workbench.pid"
DAEMON_PID_FILE="${LOG_DIR}/daemon.pid"
WORK_LOG="${LOG_DIR}/workbench.log"
DAEMON_LOG="${LOG_DIR}/daemon.log"

mkdir -p "${LOG_DIR}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${DAEMON_LOG}"
}

clean_port() {
    old_pid=$(lsof -ti tcp:${PORT} 2>/dev/null || true)
    if [ -n "${old_pid}" ]; then
        log "清理占用端口 ${PORT} 的旧进程: ${old_pid}"
        kill -9 ${old_pid} 2>/dev/null || true
        sleep 1
    fi
}

is_running() {
    if [ -f "${PID_FILE}" ]; then
        pid=$(cat "${PID_FILE}" 2>/dev/null)
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

start_workbench() {
    clean_port
    cd "${PROJECT_DIR}"
    if [ -f "${HOME}/.zshrc" ]; then
        source "${HOME}/.zshrc" 2>/dev/null || true
    fi
    nohup "${AGENT_EVAL}" workbench >> "${WORK_LOG}" 2>&1 &
    pid=$!
    echo "${pid}" > "${PID_FILE}"
    log "workbench 启动 (PID=${pid})"
}

# 主循环
echo $$ > "${DAEMON_PID_FILE}"
log "守护进程启动 (PID=$$)"
consecutive_failures=0

while true; do
    if ! is_running; then
        if [ ${consecutive_failures} -gt 0 ]; then
            log "检测到 workbench 已退出，${consecutive_failures} 秒后重启..."
            sleep ${consecutive_failures}
        fi
        start_workbench
        sleep 5
        if is_running; then
            consecutive_failures=0
            log "workbench 重启成功"
        else
            consecutive_failures=$((consecutive_failures + 1))
            if [ ${consecutive_failures} -gt 10 ]; then
                consecutive_failures=10
            fi
            log "workbench 重启失败，将在 ${consecutive_failures} 秒后重试"
        fi
    fi
    sleep 2
done
