# ai-agent-eval

通用 AI Agent 评测框架——**可分享、可复用、可贡献**的 Agent 评测体系。

同一套任务包 × 多个 Agent 后端 → 得到**可复现、可追溯、可对比**的评测报告。

- GitHub：<https://github.com/yaoxianda-github/ai-agent-eval>
- Python 包：`agent_eval` · CLI：`agent-eval`
- License：MIT

## 特性

- **任务包即契约（task-spec@v1）**：任务作者只写 `spec.yaml` + fixtures，不碰框架代码；判定点声明式描述，得分 = 权重 × 校验点通过率
- **统一后端接口 + 注册表**：新增一个被测 Agent = 新增一个 `Backend` 子类并注册，框架本体不动
- **确定性判定优先**：5 种校验点（文件存在 / 内容含/不含 / 命令退出码 0），分数可下钻到 run → 步骤轨迹 → 判分依据 → 原始产物
- **判定看产物、不看 Agent 自报**：即使 Agent 未正确收尾，只要产物符合校验点即得分
- **可观测性分层记录**：白盒后端（自研 ReAct）记步骤级轨迹，黑盒后端（aider）记单次调用输出，报告注明口径
- **自包含 HTML 报告**：`agent-eval report` 聚合全部 run，离线可打开，适合演示与分享

## 快速开始

环境要求：Python 3.9+，一个 LLM API Key（默认 DeepSeek）。

```bash
# 1) 安装（开发模式；aider 后端可选）
pip install -e ".[dev]"
pip install -e ".[aider]"        # 可选：接入第三方 aider 后端

# 2) 配置 API Key（DeepSeek）
#    Windows：setx DEEPSEEK_API_KEY "sk-..."（需重开终端）
#    macOS/Linux：export DEEPSEEK_API_KEY="sk-..."

# 3) 生成任务 fixtures（T205 的 20 张 EXIF 图片已随仓库提交，可跳过）
python scripts/gen_fixtures.py

# 4) 三条命令
agent-eval list-tasks                          # 列出任务包中的任务
agent-eval run --task T001 --agent minimal-react   # 跑单个任务（-c 可加 --model）
agent-eval report                              # 聚合全部 run 生成报告（reports/report.html）
```

## 后端（Backend）

| 后端 | 说明 | 轨迹 |
|---|---|---|
| `minimal-react` | 自研最小 ReAct Agent（LLM + JSON action 协议 + 5 个工具），作为白盒基线 | 步骤级（tool/args/observation/ts） |
| `aider` | 第三方开源 CLI（AI 结对编程），作为真实产品黑盒被测对象 | 单次调用输出 |

新增后端：在 `src/agent_eval/backends/` 下定义 `Backend` 子类，然后在 `backends/__init__.py` 注册即可。适配层负责隔离各 Agent 的接口差异（如 aider 需 `--file` 显式传入输入文件、需 git baseline、需 `stdin=DEVNULL`）。

## 任务包

`tasks/` 下每个任务一个目录：`spec.yaml`（描述 + fixtures 路径 + 校验点 + 权重）+ `fixtures/`。清单见 `tasks/manifest.yaml`。

| 任务 | 级别 | 内容 | 判定 |
|---|---|---|---|
| T001 | L1 | 批量日期格式规范化 | 确定性 |
| T102 | L2 | 销售数据汇总（csv） | 确定性 + 重算比对 |
| T205 | L3 | 图片按 EXIF 时间归档 | 确定性 + 脚本比对 |
| T401 | L4 | 缺陷脚本修复与运行 | 确定性 + 运行验证 |
| T502 | L5 | 一周工作周报总结 | 结构校验（LLM-Judge 预留） |

如何新增任务、spec 字段约定，见 [`docs/task-spec.md`](docs/task-spec.md)。

## 目录结构

```
ai-agent-eval/
├── src/agent_eval/
│   ├── cli.py            # CLI：list-tasks / run / report
│   ├── spec.py           # Task Spec 契约与加载
│   ├── runner.py         # 执行编排（fixtures → 后端 → 判定 → 评分 → run.json）
│   ├── verifiers.py      # 5 种确定性校验点
│   ├── scoring.py        # 评分（权重 × 通过率）
│   ├── reporter.py       # 自包含 HTML 报告
│   ├── tools.py          # ReAct 工具集（路径安全限制在工作目录内）
│   └── backends/         # 后端注册表（base / minimal_react / aider）
├── tasks/                # 任务包：manifest.yaml + <id>/spec.yaml + fixtures/
├── scripts/              # gen_fixtures.py + 各任务判定脚本 verify_*.py
├── results/              # run 产物（git 忽略）
├── reports/              # 报告输出（git 忽略）
├── docs/                 # task-spec.md + PROGRESS.md
└── LICENSE               # MIT
```

## 评测口径与已知限制

- **非确定性**：LLM Agent 同任务多次运行结果可能不同，框架保留多 run 证据，报告取各组合最好成绩
- **适配度差异**：不同 Agent 擅长不同任务类型（如 aider 在"改已有代码"上通过，在"从零生成数据产物"上可能循环超时）——这正是评测要暴露的信息
- **MVP 未做**：Langfuse 可观测、黑盒采集器（豆包工作 / WorkBuddy 等桌面端）、插件注册表、LLM-as-a-Judge、Docker 沙箱隔离

## 路线图

- MVP（1 周）：5 任务任务包 × 2 后端，HTML 对比报告
- 后续：Langfuse 追踪、黑盒采集器、插件注册表、多后端横向基准、pip 发布
