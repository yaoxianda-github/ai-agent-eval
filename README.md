# ai-agent-eval

通用 AI Agent 评测框架——可分享、可复用、可贡献的 Agent 评测体系。

- GitHub 仓库：<https://github.com/yaoxianda-github/ai-agent-eval>
- 包名（Python import）：`agent_eval` / CLI：`agent-eval`

> 当前状态：**MVP 初始化中**（一周冲刺计划，见项目 `docs/` 目录规划）。

## 这是什么

同一套任务包 × 多个 Agent 后端 → 得到**可复现、可追溯、可对比**的评测报告。
- 任务包（Task Spec）即契约：任务作者只写 spec + fixtures，不碰框架代码
- 统一后端接口：新增 Agent 只需新增一个采集/执行适配
- 确定性判定优先：分数可下钻到 run → 轨迹 → 判分依据 → 原始证据

## 快速开始

```bash
# 安装（开发模式）
pip install -e ".[dev]"

# 生成任务 fixtures（T205 图片需要 Pillow）
python scripts/gen_fixtures.py

# 列出任务包中的任务
agent-eval list-tasks

# 执行单个任务评测（Day 2-3 落地）
agent-eval run --task T001 --agent minimal-agent
```

## 目录结构

```
agent-eval/
├── src/agent_eval/     # 框架源码（cli / spec / runner / verifiers / scorers / collectors）
├── tasks/              # 任务包：manifest.yaml + <id>/spec.yaml + fixtures/
├── scripts/            # gen_fixtures.py + 各任务判定脚本 verify_*.py
├── observability/      # 可观测接入（MVP 阶段用结构化日志，预留 Langfuse 接口）
├── reports/            # 评测报告输出
├── docs/               # task-spec 规范与复盘
└── tests/              # 框架自身测试
```

Task Spec 规范见 [`docs/task-spec.md`](docs/task-spec.md)。

## 路线图

- MVP（1 周）：5 任务任务包 × 2 后端（自建 ReAct Agent + Aider），HTML 对比报告
- 后续：Langfuse 可观测、黑盒采集器（豆包工作 / WorkBuddy 等桌面端）、插件注册表、pip 发布

## License

MIT（待定）
