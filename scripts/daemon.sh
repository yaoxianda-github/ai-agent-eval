#!/bin/bash
# agent-eval workbench 守护进程管理
# 用法: ./scripts/daemon.sh start | stop | status | restart

PROJECT_DIR="/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval"
SCRIPT_DIR="${PROJECT_DIR}/scripts"
LOG_DIR="${PROJECT_DIR}/logs"
PID_FILE="${LOG_DIR}/workbench.pid"
DAEMON_PID_FILE="${LOG_DIR}/daemon.pid"
DAEMON_LOG="${LOG_DIR}/daemon.log"
PORT=8000

mkdir -p "${LOG_DIR}"

is_daemon_running() {
    if [ -f "${DAEMON_PID_FILE}" ]; then
        dpid=$(cat "${DAEMON_PID_FILE}" 2>/dev/null)
        if [ -n "${dpid}" ] && kill -0 "${dpid}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

is_workbench_running() {
    if [ -f "${PID_FILE}" ]; then
        wpid=$(cat "${PID_FILE}" 2>/dev/null)
        if [ -n "${wpid}" ] && kill -0 "${wpid}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

case "${1:-}" in
    start)
        if is_daemon_running; then
            dpid=$(cat "${DAEMON_PID_FILE}")
            echo "守护进程已在运行 (PID=${dpid})"
            exit 0
        fi
        # 清理旧的 pid 文件
        rm -f "${DAEMON_PID_FILE}" "${PID_FILE}"
        # 用 nohup 启动守护循环，完全脱离终端
        nohup bash "${SCRIPT_DIR}/_daemon_loop.sh" >> "${DAEMON_LOG}" 2>&1 < /dev/null &
        disown
        echo "守护进程启动中..."
        sleep 5
        if is_workbench_running; then
            wpid=$(cat "${PID_FILE}")
            echo "守护进程已启动，workbench 运行中 (PID=${wpid}, 端口 ${PORT})"
            echo "服务日志: ${LOG_DIR}/workbench.log"
            echo "守护日志: ${DAEMON_LOG}"
        else
            echo "启动失败，请查看日志: ${DAEMON_LOG}"
            exit 1
        fi
        ;;
    stop)
        if is_daemon_running; then
            dpid=$(cat "${DAEMON_PID_FILE}")
            kill "${dpid}" 2>/dev/null || true
            echo "已停止守护进程 (PID=${dpid})"
        else
            echo "守护进程未运行"
        fi
        if is_workbench_running; then
            wpid=$(cat "${PID_FILE}")
            kill "${wpid}" 2>/dev/null || true
            echo "已停止 workbench (PID=${wpid})"
        fi
        # 清理端口
        old_pid=$(lsof -ti tcp:${PORT} 2>/dev/null || true)
        if [ -n "${old_pid}" ]; then
            kill -9 ${old_pid} 2>/dev/null || true
        fi
        rm -f "${DAEMON_PID_FILE}" "${PID_FILE}"
        ;;
    status)
        if is_daemon_running; then
            dpid=$(cat "${DAEMON_PID_FILE}")
            echo "守护进程: 运行中 (PID=${dpid})"
        else
            echo "守护进程: 未运行"
        fi
        if is_workbench_running; then
            wpid=$(cat "${PID_FILE}")
            echo "workbench: 运行中 (PID=${wpid}, 端口 ${PORT})"
        else
            echo "workbench: 未运行"
        fi
        ;;
    restart)
        $0 stop
        sleep 2
        $0 start
        ;;
    *)
        echo "用法: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
