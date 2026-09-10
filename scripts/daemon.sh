#!/bin/bash
# agent-eval workbench 守护进程管理（Python 版）
# 用法: ./scripts/daemon.sh start | stop | status | restart
#
# 改进：使用 Python 守护进程（scripts/_daemon.py）替代 bash 循环
# - os.setsid() 完全脱离终端会话
# - 健壮的异常处理和日志记录
# - 指数退避重启（1s → 2s → 4s → ... → 最大 30s）
# - 优雅退出（SIGTERM → 等待 → SIGKILL）
# - 启动前自动清理 8000 端口

PROJECT_DIR="/Users/yaoxianda/Desktop/yaoxianda/doubaowork/ai-agent-eval"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
DAEMON_SCRIPT="${PROJECT_DIR}/scripts/_daemon.py"

case "${1:-}" in
    start|stop|status|restart)
        exec "${PYTHON}" "${DAEMON_SCRIPT}" "$1"
        ;;
    *)
        echo "用法: $0 {start|stop|status|restart}"
        exit 1
        ;;
esac
