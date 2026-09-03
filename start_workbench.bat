@echo off
rem ============================================================
rem  AI Agent 评测工作台 · 一键启动（Windows）
rem  双击本文件即可启动，浏览器自动打开 http://127.0.0.1:8000
rem ============================================================
cd /d "%~dp0"

if not exist ".venv" (
  echo 未检测到虚拟环境，使用系统 Python 启动...
) else (
  call ".venv\Scripts\activate.bat"
)

echo 启动 Web 评测工作台... 浏览器打开 http://127.0.0.1:8000
echo 提示：如需启用 Langfuse trace，请先设置 AGENT_EVAL_TRACE=langfuse
start http://127.0.0.1:8000
python -m agent_eval.web --port 8000
pause
