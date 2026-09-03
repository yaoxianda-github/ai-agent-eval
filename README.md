# ai-agent-eval

通用 AI Agent 评测框架 ——**可分享、可复用、可贡献**的 Agent 评测体系。

同一套任务包 × 多个 Agent 后端 → 得到**可复现、可追溯、可对比**的评测报告。



* GitHub：[https://github.com/yaoxianda-github/ai-agent-eval](https://github.com/yaoxianda-github/ai-agent-eval)

* Python 包：`agent_eval` · CLI：`agent-eval`

* License：MIT

## 特性



* **任务包即契约（task-spec@v1）**：任务作者只写 `spec.yaml` + fixtures，不碰框架代码；判定点声明式描述，得分 = 权重 × 校验点通过率

* **统一后端接口 + 注册表**：新增一个被测 Agent = 新增一个 `Backend` 子类并注册，框架本体不动

* **确定性判定优先**：5 种校验点（文件存在 / 内容含 / 不含 / 命令退出码 0），分数可下钻到 run → 步骤轨迹 → 判分依据 → 原始产物

* **判定看产物、不看 Agent 自报**：即使 Agent 未正确收尾，只要产物符合校验点即得分

* **可观测性分层记录**：白盒后端（自研 ReAct）记步骤级轨迹，黑盒后端（aider）记单次调用输出，报告注明口径

* **自包含 HTML 报告**：`agent-eval report` 聚合全部 run，离线可打开，适合演示与分享

* **Web 评测工作台（V2.1）**：本地 FastAPI + 单页前端，浏览器全程操作——新建任务、运行、下钻轨迹、对比、看报告，CLI 引擎零重写

* **LLM-as-a-Judge 语义判分（V2.2）**：`verifier: llm_judge` 的开放任务（如 T502 周报）在确定性校验之外，由 LLM 按 rubric 判分（完整性/准确性/结构/语言），verdict 附带 score 与 reasoning，可下钻

* **可选 Langfuse 分析层（V2.2）**：默认零依赖 no-op；设置 `AGENT_EVAL_TRACE=langfuse` + 凭据后自动记录每次 LLM 调用（输入/输出/token/耗时），用于成本与行为归因

* **一键启动（V2.2）**：`agent-eval workbench` 或双击 `start_workbench.bat` 即启动工作台；`scripts/build_workbench.py` 可打包为单文件可执行程序

## 快速开始

环境要求：Python 3.9+，一个 LLM API Key（默认 DeepSeek）。



```
\# 1) 安装（开发模式；aider 后端可选）

pip install -e ".\[dev]"

pip install -e ".\[aider]"        # 可选：接入第三方 aider 后端

\# 2) 配置 API Key（DeepSeek）

\#    Windows：setx DEEPSEEK\_API\_KEY "sk-..."（需重开终端）

\#    macOS/Linux：export DEEPSEEK\_API\_KEY="sk-..."

\# 3) 生成任务 fixtures（T205 的 20 张 EXIF 图片已随仓库提交，可跳过）

python scripts/gen\_fixtures.py

\# 4) 三条命令

agent-eval list-tasks                          # 列出任务包中的任务

agent-eval run --task T001 --agent minimal-react   # 跑单个任务（-c 可加 --model）

agent-eval run --task T001 --agent minimal-react --runs 3   # 多 run 采样（V2.0）

agent-eval report                              # 聚合全部 run 生成报告（reports/report.html）

\# 5) Web 评测工作台（V2.1+，可选）

pip install -e ".\[web]"

python -m agent\_eval.web --port 8000          # 浏览器打开 http://127.0.0.1:8000

agent-eval workbench                          # 等价启动命令；或双击 start\_workbench.bat

\# 6) Langfuse 分析层（V2.2，可选）

\#    setx AGENT\_EVAL\_TRACE "langfuse" + 配置 LANGFUSE\_PUBLIC\_KEY / SECRET\_KEY / HOST

pip install langfuse                          # 未安装时自动降级为 no-op
```

## 后端（Backend）



| 后端              | 说明                                                    | 轨迹                            |
| --------------- | ----------------------------------------------------- | ----------------------------- |
| `minimal-react` | 自研最小 ReAct Agent（LLM + JSON action 协议 + 5 个工具），作为白盒基线 | 步骤级（tool/args/observation/ts） |
| `aider`         | 第三方开源 CLI（AI 结对编程），作为真实产品黑盒被测对象                       | 单次调用输出                        |

新增后端：在 `src/agent_eval/backends/` 下定义 `Backend` 子类，然后在 `backends/__init__.py` 注册即可。适配层负责隔离各 Agent 的接口差异（如 aider 需 `--file` 显式传入输入文件、需 git baseline、需 `stdin=DEVNULL`）。

## 任务包

`tasks/` 下每个任务一个目录：`spec.yaml`（描述 + fixtures 路径 + 校验点 + 权重）+ `fixtures/`。清单见 `tasks/manifest.yaml`。



| 任务   | 级别 | 内容            | 判定                 |
| ---- | -- | ------------- | ------------------ |
| T001 | L1 | 批量日期格式规范化     | 确定性                |
| T102 | L2 | 销售数据汇总（csv）   | 确定性 + 重算比对         |
| T205 | L3 | 图片按 EXIF 时间归档 | 确定性 + 脚本比对         |
| T401 | L4 | 缺陷脚本修复与运行     | 确定性 + 运行验证         |
| T502 | L5 | 一周工作周报总结      | 结构校验（LLM-Judge 预留） |

如何新增任务、spec 字段约定，见 [docs/task-spec.md](docs/task-spec.md)。

## 目录结构



```
ai-agent-eval/

├── src/agent\_eval/

│   ├── cli.py            # CLI：list-tasks / run / report

│   ├── spec.py           # Task Spec 契约与加载

│   ├── runner.py         # 执行编排（fixtures → 后端 → 判定 → 评分 → run.json）

│   ├── verifiers.py      # 5 种确定性校验点

│   ├── scoring.py        # 评分（权重 × 通过率）

│   ├── reporter.py       # 自包含 HTML 报告

│   ├── tools.py          # ReAct 工具集（路径安全限制在工作目录内）

│   ├── stats.py          # 多 run 采样统计（best/mean/std/pass_rate）

│   ├── judge.py          # V2.2 LLM-as-a-Judge 语义判分器（verifier=llm_judge）

│   ├── observability.py  # V2.2 可选 Langfuse trace 层（默认 no-op）

│   ├── web/              # V2.1 Web 工作台：app.py(FastAPI) / store.py(SQLite) / taskgen.py / static/ 单页前端

│   └── backends/         # 后端注册表（base / minimal\_react / aider）

├── tasks/                # 任务包：manifest.yaml + \<id>/spec.yaml + fixtures/（T001/T102/T205/T401/T502/T601）

├── scripts/              # gen\_fixtures.py + 各任务判定脚本 verify\_\*.py + build\_workbench.py

├── results/              # run 产物（git 忽略）

├── reports/              # 报告输出（git 忽略）

├── start\_workbench.bat   # 一键启动工作台（双击即可）

├── docs/                 # task-spec.md + PROGRESS.md + V2_PLAN.md

└── LICENSE               # MIT
```

## 评测口径与已知限制



* **非确定性**：LLM Agent 同任务多次运行结果可能不同，框架保留多 run 证据，报告取各组合最好成绩

* **适配度差异**：不同 Agent 擅长不同任务类型（如 aider 在 "改已有代码" 上通过，在 "从零生成数据产物" 上可能循环超时）—— 这正是评测要暴露的信息

* **已完成**：LLM-as-a-Judge（T502 闭环，开放任务语义判分）、Langfuse 可选分析层、T601 日志统计任务、一键启动（workbench 命令 / bat / PyInstaller）

* **V2.2 未做**：黑盒采集器（豆包工作 / WorkBuddy 等桌面端，需桌面级自动化）、插件注册表、Docker 沙箱隔离

## Web 工作台（V2.1）

本地优先的单机工作台，浏览器全程操作，复用 CLI 引擎（零重写）：

| 页面 | 能力 |
| --- | --- |
| 工作台 | 选任务/后端/模型/超时/采样次数 → 启动运行 → 轮询进度 → 结果 + 多 run 统计 |
| 任务管理 | 现有任务列表 + 新建任务表单（动态校验点编辑器，生成 spec.yaml + fixtures 骨架 + 更新 manifest） |
| 运行历史 | SQLite 索引，按任务/后端/状态筛选，点击下钻 |
| 运行详情 | 判定结果 + 步骤轨迹 + 产物文件预览（含路径穿越防护） |
| 对比 | Agent × 任务得分矩阵 + 采样统计（N/mean/best/σ）+ 任务通过率 |
| 报告 | 复用引擎 reporter 生成自包含 HTML，iframe 内嵌查看 |
| 设置 | 目录/版本 + 环境变量说明 |

启动：`pip install -e ".[web]"` → `python -m agent_eval.web --port 8000` → 打开 http://127.0.0.1:8000

## 路线图



* MVP（1 周）：5 任务任务包 × 2 后端，HTML 对比报告

* V2.0（引擎地基）：框架单测 + 命令沙箱 + 多 run 采样（已完成）

* V2.1（Web 工作台）：FastAPI + 单页前端 + SQLite（已实现，pytest 56 全绿）

* V2.2（增强，已实现待验收）：LLM-as-a-Judge（T502 闭环 + rubric）· Langfuse 可选分析层 · T601 日志统计任务 · 一键启动（workbench/bat/PyInstaller）

* V2.2 未做：黑盒桌面端采集器、插件注册表、Docker 沙箱隔离、pip 发布