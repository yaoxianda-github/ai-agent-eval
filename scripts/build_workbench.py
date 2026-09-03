"""一键打包脚本（V2.2，可选）：把 Web 工作台打包为单文件可执行程序。

用法：
  pip install pyinstaller
  python scripts/build_workbench.py

产物：dist/agent-eval-workbench.exe（双击即可启动服务，免安装 Python）。
说明：当前项目以「源代码 + start_workbench.bat」为默认交付方式（可审计、可复用）；
本脚本仅作分发/面试演示的可选增强，不替代源码方式。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "_workbench_entry.py"


def ensure_entry() -> None:
    ENTRY.write_text(
        "\n".join(
            [
                "from agent_eval.web.app import create_app",
                "import uvicorn",
                "",
                "if __name__ == '__main__':",
                "    uvicorn.run(create_app(), host='127.0.0.1', port=8000, log_level='info')",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    ensure_entry()
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "agent-eval-workbench",
        "--add-data",
        f"{ROOT / 'src' / 'agent_eval' / 'web' / 'static'};agent_eval/web/static",
        "--add-data",
        f"{ROOT / 'tasks'};tasks",
        str(ENTRY),
    ]
    print("运行 PyInstaller...（首次较慢）")
    subprocess.run(cmd, cwd=ROOT, check=True)
    print(f"打包完成：{ROOT / 'dist' / 'agent-eval-workbench.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
