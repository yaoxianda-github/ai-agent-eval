#!/usr/bin/env python3
"""
agent-eval workbench 守护进程（Python 版）

比 bash 版更可靠：
- 使用 os.setsid() 完全脱离终端会话
- 健壮的异常处理和日志记录
- 指数退避重启（1s → 2s → 4s → ... → 最大 30s）
- 监控 workbench 进程存活，崩溃后自动重启
- 启动前清理 8000 端口残留进程
- 守护进程自身崩溃时也能被检测

用法：
  python scripts/_daemon.py start    # 启动守护进程
  python scripts/_daemon.py stop     # 停止守护进程和 workbench
  python scripts/_daemon.py status   # 查看状态
  python scripts/_daemon.py restart  # 重启
"""

import os
import sys
import time
import signal
import subprocess
import logging
from pathlib import Path
from datetime import datetime

# 项目根目录（脚本在 scripts/ 下，上级是项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_EVAL = PROJECT_ROOT / ".venv" / "bin" / "agent-eval"
WORK_LOG = PROJECT_ROOT / "logs" / "workbench.log"
DAEMON_LOG = PROJECT_ROOT / "logs" / "daemon.log"
PID_FILE = PROJECT_ROOT / "logs" / "daemon.pid"
WORKBENCH_PID_FILE = PROJECT_ROOT / "logs" / "workbench.pid"
PORT = 8000

# 确保 logs 目录存在
(PROJECT_ROOT / "logs").mkdir(exist_ok=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(DAEMON_LOG, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("daemon")


def log(msg: str):
    logger.info(msg)


def is_process_alive(pid: int) -> bool:
    """检查进程是否存活（排除僵尸进程）"""
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    # 额外检查：排除僵尸进程（defunct/zombie）
    # os.kill(pid, 0) 对僵尸进程也返回成功，需要用 ps 检查状态
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True, text=True, timeout=3
        )
        stat = result.stdout.strip()
        if stat.startswith("Z"):
            # 僵尸进程，视为已死亡
            return False
        return True
    except Exception:
        # ps 检查失败时，保守地认为进程存活（避免误杀）
        return True


def cleanup_port(port: int):
    """清理占用指定端口的进程"""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True, timeout=5
        )
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
                log(f"清理端口 {port} 残留进程 PID={pid}")
            except (OSError, ValueError):
                pass
        time.sleep(1)
    except Exception as e:
        log(f"清理端口失败: {e}")


def start_workbench() -> int:
    """启动 workbench 进程，返回 PID"""
    cleanup_port(PORT)

    # 确保日志文件存在
    WORK_LOG.touch(exist_ok=True)

    log_file = open(WORK_LOG, "a")
    proc = subprocess.Popen(
        [str(AGENT_EVAL), "workbench"],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # 等同于 setsid，完全脱离终端
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    log(f"workbench 启动 (PID={proc.pid})")

    # 等待服务就绪（最多等 10 秒）
    for i in range(20):
        time.sleep(0.5)
        try:
            result = subprocess.run(
                ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                 f"http://127.0.0.1:{PORT}/"],
                capture_output=True, text=True, timeout=3
            )
            if result.stdout.strip() == "200":
                log(f"workbench 就绪 (HTTP 200)，耗时 {(i+1)*0.5:.1f}s")
                return proc.pid
        except Exception:
            pass

    log("workbench 启动超时（10秒内未响应 HTTP 200），但进程已启动")
    return proc.pid


def stop_workbench(pid: int):
    """停止 workbench 进程"""
    if pid and is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"发送 SIGTERM 到 workbench PID={pid}")
            # 等待进程退出（最多 5 秒）
            for _ in range(10):
                time.sleep(0.5)
                if not is_process_alive(pid):
                    log("workbench 已优雅退出")
                    return
            # 强制杀死
            os.kill(pid, signal.SIGKILL)
            log("workbench 强制退出 (SIGKILL)")
        except OSError as e:
            log(f"停止 workbench 失败: {e}")
    cleanup_port(PORT)


def daemon_loop():
    """守护进程主循环"""
    log("=" * 50)
    log(f"守护进程启动 (PID={os.getpid()})")
    log(f"项目目录: {PROJECT_ROOT}")
    log(f"workbench 命令: {AGENT_EVAL} workbench")
    log("=" * 50)

    workbench_pid = None
    consecutive_failures = 0
    max_backoff = 30  # 最大退避时间（秒）

    # 信号处理：优雅退出
    def handle_signal(signum, frame):
        log(f"收到信号 {signum}，正在优雅退出...")
        if workbench_pid:
            stop_workbench(workbench_pid)
        if PID_FILE.exists():
            PID_FILE.unlink()
        log("守护进程已退出")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while True:
        try:
            # 检查 workbench 是否存活
            if workbench_pid is None or not is_process_alive(workbench_pid):
                if workbench_pid:
                    log(f"workbench 进程已死亡 (PID={workbench_pid})，准备重启...")
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

                # 指数退避
                backoff = min(2 ** consecutive_failures, max_backoff)
                if backoff > 1:
                    log(f"退避 {backoff}s 后重启（连续失败 {consecutive_failures} 次）")
                    time.sleep(backoff)

                # 启动 workbench
                workbench_pid = start_workbench()
                WORKBENCH_PID_FILE.write_text(str(workbench_pid))
                consecutive_failures = 0  # 启动成功后重置失败计数

            # 每 2 秒检查一次
            time.sleep(2)

        except Exception as e:
            log(f"守护循环异常: {e}", exc_info=True)
            time.sleep(5)


def cmd_start():
    """启动守护进程"""
    # 检查是否已在运行
    if PID_FILE.exists():
        old_pid = int(PID_FILE.read_text().strip())
        if is_process_alive(old_pid):
            print(f"守护进程已在运行 (PID={old_pid})")
            return
        else:
            log(f"发现过期 PID 文件 (PID={old_pid})，清理后重新启动")
            PID_FILE.unlink()

    # 使用 os.fork() + setsid() 完全脱离终端
    pid = os.fork()
    if pid > 0:
        # 父进程：写入 PID 文件并退出
        time.sleep(1)
        if is_process_alive(pid):
            PID_FILE.write_text(str(pid))
            print(f"守护进程已启动 (PID={pid})")
            print(f"workbench 日志: {WORK_LOG}")
            print(f"守护日志: {DAEMON_LOG}")
        else:
            print("守护进程启动失败，请查看日志")
            sys.exit(1)
        return

    # 子进程：创建新会话，脱离终端
    os.setsid()
    os.chdir(str(PROJECT_ROOT))

    # 重定向标准输入输出
    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(DAEMON_LOG, "a")
    sys.stderr = open(DAEMON_LOG, "a")

    # 运行守护循环
    daemon_loop()


def cmd_stop():
    """停止守护进程和 workbench"""
    if not PID_FILE.exists():
        print("守护进程未运行（无 PID 文件）")
        # 仍然尝试清理端口
        cleanup_port(PORT)
        return

    pid = int(PID_FILE.read_text().strip())
    if not is_process_alive(pid):
        print(f"守护进程已死亡 (PID={pid})，清理 PID 文件")
        PID_FILE.unlink()
        cleanup_port(PORT)
        return

    # 停止 workbench
    if WORKBENCH_PID_FILE.exists():
        wb_pid = int(WORKBENCH_PID_FILE.read_text().strip())
        stop_workbench(wb_pid)
        WORKBENCH_PID_FILE.unlink()

    # 停止守护进程
    print(f"停止守护进程 (PID={pid})...")
    os.kill(pid, signal.SIGTERM)

    # 等待退出（最多 5 秒）
    for _ in range(10):
        time.sleep(0.5)
        if not is_process_alive(pid):
            print("守护进程已优雅退出")
            PID_FILE.unlink()
            return

    # 强制杀死
    os.kill(pid, signal.SIGKILL)
    print("守护进程强制退出")
    PID_FILE.unlink()
    cleanup_port(PORT)


def cmd_status():
    """查看状态"""
    print("=== 守护进程状态 ===")
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        if is_process_alive(pid):
            print(f"守护进程: 运行中 (PID={pid})")
        else:
            print(f"守护进程: 已死亡 (PID={pid}，PID 文件过期)")
    else:
        print("守护进程: 未运行")

    print()
    print("=== workbench 状态 ===")
    if WORKBENCH_PID_FILE.exists():
        pid = int(WORKBENCH_PID_FILE.read_text().strip())
        if is_process_alive(pid):
            print(f"workbench: 运行中 (PID={pid}, 端口 {PORT})")
        else:
            print(f"workbench: 已死亡 (PID={pid})")
    else:
        print("workbench: 未运行")

    # HTTP 检查
    print()
    print("=== HTTP 检查 ===")
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"http://127.0.0.1:{PORT}/"],
            capture_output=True, text=True, timeout=3
        )
        print(f"http://127.0.0.1:{PORT}/ -> HTTP {result.stdout.strip()}")
    except Exception as e:
        print(f"HTTP 检查失败: {e}")


def main():
    if len(sys.argv) < 2:
        print("用法: python _daemon.py {start|stop|status|restart}")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "restart":
        cmd_stop()
        time.sleep(2)
        cmd_start()
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
