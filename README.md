# ai-agent-eval

通用 AI Agent 评测框架 —— **可分享、可复用、可贡献**的 Agent 评测体系。

同一套任务包 × 多个 Agent 后端 → 得到**可复现、可追溯、可对比**的评测报告；
配合 CI 质量门禁，让 Agent 能力成为**上线卡口**。

* GitHub：[https://github.com/yaoxianda-github/ai-agent-eval](https://github.com/yaoxianda-github/ai-agent-eval)
* 端到端演示仓库：[agent-eval-demo](https://github.com/yaoxianda-github/agent-eval-demo)（GitHub Actions 实跑 core 门禁 + 合并阻断）
* Python 包：`agent_eval` · CLI：`agent-eval`
* License：MIT

## 特性

* **任务包即契约（task-spec@v1）**：任务作者只写 `spec.yaml` + fixtures，不碰框架代码；判定点声明式描述，得分 = 权重 × 校验点通过率
* **统一后端接口 + 注册表**：新增一个被测 Agent = 新增一个 `Backend` 子类并注册，框架本体不动
* **确定性判定优先**：5 种校验点（文件存在 / 内容含 / 不含 / 命令退出码 0），分数可下钻到 run → 轨迹回放 → 判分依据 → 原始产物
* **判定看产物、不看 Agent 自报**：即使 Agent 未正确收尾，只要产物符合校验点即得分
* **轨迹回放（V2.4）**：运行详情以时间线展示全链路——输入意图 → 知识/检索 → 模型生成 → 工具执行，支持类型过滤与展开，开发/产品自助定位问题
* **RAG 真实检索环节（V2.4/V2.5）**：`search_kb` 工具（BM25 检索）使评测集具备真实知识检索语义；T701/T702 带干扰项知识库
* **黑盒后端模型层（V2.4）**：deepseek-harness 结束后解析 dsh session，还原模型 reasoning / 工具决策 / 最终输出，黑盒不再"黑"
* **CI 质量门禁（V2.3/M1）**：`agent-eval ci` 无头运行，输出 JUnit XML + Allure + 汇总 JSON，core 包通过率不达标退出码非 0——GitHub 分支保护 required check 直接阻断合并
* **多 Agent 横向对比门禁（V2.3）**：gate 配置 `agents:` 列表，每 Agent 独立跑同一任务集，全部达标才 PASS，报告输出 agent × task 结果矩阵
* **多 Agent 对比矩阵（V2.7，Open Core 首个 Pro 能力）**：工作台一键发起「N 个 Agent × 任务集 × runs」对比批次，实时进度、彩色得分矩阵（点击下钻到每次 run 与轨迹）、加权总分/通过率/真实成本/耗时/稳定性 σ 汇总、自动结论与 CSV 导出；社区版限 2 Agent、隐藏成本稳定性列、禁导出，导入 License 解锁 Pro
* **真实成本核算（V2.3）**：任务级预计成本（token 成本模型）+ 运行实际成本（DeepSeek 余额差分，批量精度 ¥0.09），工作台与 CI 报告均展示
* **LLM-as-a-Judge 语义判分（V2.2）**：`verifier: llm_judge` 的开放任务由 LLM 按 rubric 判分，verdict 附带 score 与 reasoning
* **可选 Langfuse 分析层（V2.2）**：默认零依赖 no-op；设置 `AGENT_EVAL_TRACE=langfuse` + 凭据后自动记录每次 LLM 调用
* **自包含 HTML 报告**：`agent-eval report` 聚合全部 run，离线可打开
* **Web 评测工作台**：本地 FastAPI + 单页前端，浏览器全程操作——任务管理（含成本提示）、运行、轨迹回放、对比、报告

## 快速开始

环境要求：Python 3.9+，一个 LLM API Key（默认 DeepSeek）。

```bash
# 1) 安装（开发模式）
pip install -e ".[dev]"

# 2) 配置 API Key（DeepSeek）
#    macOS/Linux：export DEEPSEEK_API_KEY="sk-..."
#    Windows：setx DEEPSEEK_API_KEY "sk-..."（需重开终端）

# 3) 三条命令
agent-eval list-tasks                          # 列出任务包中的任务（23 个）
agent-eval run --task T001 --agent minimal-react              # 跑单个任务
agent-eval run --task T001 --agent minimal-react --runs 3     # 多 run 采样（对抗非确定性）
agent-eval report                              # 聚合全部 run 生成报告（reports/report.html）

# 4) CI 质量门禁（M1）
agent-eval ci --gate core --agent minimal-react \
  --junit-xml results/junit.xml --allure-dir results/allure-results
#    按 ci/gate.yaml 执行 core 卡口；通过率 < 阈值时退出码非 0（阻断合并）

# 5) Web 评测工作台
agent-eval workbench                          # 浏览器打开 http://127.0.0.1:8000

# 6) 可选：Langfuse 分析层（AGENT_EVAL_TRACE=langfuse + 凭据后自动启用）
```

## 后端（Backend）

| 后端 | 说明 | 轨迹 |
| --- | --- | --- |
| `minimal-react` | 自研最小 ReAct Agent（LLM + JSON action 协议 + 6 工具含 `search_kb`），白盒基线 | 步骤级 + llm 节点（model/input/output/tokens） |
| `deepseek-harness` | DeepSeek 官方开源 harness（dsh，headless 模式），真实产品黑盒 | dsh 单步 + **session 解析**（reasoning/工具决策/final/tool） |
| `aider` | 第三方开源 CLI（AI 结对编程），真实产品黑盒 | 单次调用输出 |

新增后端：在 `src/agent_eval/backends/` 下定义 `Backend` 子类，然后在 `backends/__init__.py` 注册即可。

dsh 后端注意：使用**评测专用隔离 DSH_HOME**（`~/.cache/agent-eval/dsh-home`），凭据只来自环境变量 `DEEPSEEK_API_KEY`——避免 `~/.dsh` 用户个人凭据污染（旧凭据会导致 403 预扣失败）。

## 任务包

`tasks/` 下每个任务一个目录：`spec.yaml` + `fixtures/`。清单见 `tasks/manifest.yaml`（当前 23 个）。

| 任务 | 级别 | 内容 | 判定 |
| --- | --- | --- | --- |
| T001 | L1 | 批量日期格式规范化 | 确定性 |
| T102 | L2 | 销售数据汇总（csv） | 确定性 + 重算比对 |
| T207 | L3 | 日志多级统计报告 | 确定性 + 脚本比对 |
| T303 | L4 | 实现缺失函数并通过验证 | 确定性 + 运行验证 |
| T305 | L3 | GAIA 员工档案推理问答 | 确定性 |
| T401 | L4 | 缺陷脚本修复与运行 | 确定性 + 运行验证 |
| T502 | L5 | 一周工作周报总结 | LLM-as-a-Judge（rubric 判分） |
| T601 | L3 | 日志统计 | 确定性 + 脚本比对 |
| T701 | L2 | **RAG** 知识库检索：整机质保查询 | 确定性（search_kb + 作答） |
| T702 | L3 | **RAG** 知识库检索：两段信息归纳 | 确定性（多片段检索 + 归纳） |

> 全量清单与 spec 字段约定见 `tasks/manifest.yaml` 与 [docs/task-spec.md](docs/task-spec.md)。

## CI/CD 质量门禁（V2.3/M1）

把评测接入 GitHub CI 的完整链路（端到端验证见 [docs/CI_GATE_VALIDATION.md](docs/CI_GATE_VALIDATION.md)）：

```
PR 提交 → GitHub Actions 跑 agent-eval ci --gate core
        → 通过率 ≥ min_pass_rate  → 合并按钮可用（PASS）
        └ 通过率 <  min_pass_rate  → 退出码非 0，required check 失败，合并被阻断（FAIL）
```

### gate 配置（`ci/gate.yaml`）

```yaml
gate:
  core:            # 上线卡口：PR 合并前必须通过
    tasks: [T001, T102, T103, T207, T305, T306, T308, T303, T401, T402, T701, T702]  # 12 任务
    runs: 3                    # 多采样对抗 LLM 非确定性
    task_pass_ratio: 0.5       # 任务级多数制：3 run 中 ≥2 通过才算该任务通过
    min_pass_rate: 0.9         # 12 任务允许 1 个未通过
  full:            # 全量回归：合并后/发布前
    tasks: "*"     # 全部 23 任务
    runs: 1
    min_pass_rate: 0.8
  # 多 Agent 横向对比：agents: [minimal-react, deepseek-harness]（每 Agent 独立跑，全部达标才 PASS）
```

### 门禁判定语义

1. 每个任务跑 `runs` 次 → 任务通过 = 通过 run 数 / runs ≥ `task_pass_ratio`
2. gate 通过率 = 通过任务数 / 总任务数 ≥ `min_pass_rate` → **PASS**；否则退出码非 0
3. 输出：JUnit XML（每任务一个 suite，CI 直接解析）、Allure results、`results/ci-report.json` 汇总（含 agent × task 结果矩阵与成本）

### 在 GitHub 仓库启用

```yaml
# .github/workflows/agent-eval-gate.yml（骨架）
- run: pip install "git+https://github.com/yaoxianda-github/ai-agent-eval.git"
- run: agent-eval ci --gate core --agent deepseek-harness --junit-xml results/junit.xml --allure-dir results/allure-results
- if: failure()
  uses: actions/upload-artifact@v4
  with: { name: reports, path: "results/*" }
```

配合分支保护：Settings → Branches → 添加 required check（如 `agent-eval core gate`）→ 失败即禁止合并。

## 成本核算

| 口径 | 位置 | 说明 |
| --- | --- | --- |
| 预计成本 | 任务管理（每任务成本列，`--` 带说明） | token 成本模型估算：`usage` × 单价（prompt ¥1/1M、completion ¥2/1M） |
| 实际成本 | 运行历史（实际成本列）与 CI 报告 | **余额差分**：运行前后调用 DeepSeek `/user/balance`（curl 实现，urllib SSL 失效的兼容），批量差分精度 ¥0.09；黑盒 token=0 兜底 |

成本列右上角感叹号悬浮/点击可查看计算逻辑。

## Web 工作台

本地优先的单机工作台，浏览器全程操作，复用 CLI 引擎（零重写）：

| 页面 | 能力 |
| --- | --- |
| 工作台 | 选任务/后端/模型/超时/采样次数 → 启动运行 → 轮询进度 → 结果 + 多 run 统计 |
| 任务管理 | 23 任务列表 + 新建任务表单（动态校验点编辑器）+ 每任务预计成本（含成本口径提示） |
| 运行历史 | SQLite 索引，分页浏览，按任务/后端/状态筛选，实际成本列 |
| 运行详情 | 判定结果 + **轨迹回放时间线** + 步骤轨迹 + 产物文件预览（含路径穿越防护） |
| 对比矩阵 | 发起「N Agent × 任务集 × runs」对比批次，实时进度 + 彩色得分矩阵（下钻每次 run/轨迹）+ 加权总分/通过率/成本/耗时/σ 汇总 + 自动结论 + CSV 导出（V2.7） |
| 报告 | 复用引擎 reporter 生成自包含 HTML，iframe 内嵌查看 |
| 设置 | 目录/版本 + 环境变量说明 |

启动：`pip install -e ".[web]"` → `agent-eval workbench` → 打开 http://127.0.0.1:8000

### 轨迹回放（V2.4）

运行详情页以时间线展示一次评测的全链路，按时间（绝对时间 + 相对耗时）排序：

1. **输入意图**：任务描述（intent 节点）
2. **知识/检索**：检索类工具（`search_kb` / read_file / list_dir / read / grep）返回的知识片段（retrieval 节点）
3. **模型生成**：每步 LLM 输出（llm 节点；minimal-react 记录 model/input/output/tokens；dsh 解析出 reasoning/工具决策/final）
4. **工具执行**：工具调用与观察结果（tool 节点）

支持类型过滤（全部/意图/知识/模型/工具）与长文本展开/收起。数据来源：

- **本地观测层**：`run.json` 新增 `traces` 字段，runner 统一生成（后端自报优先，旧后端由 steps 兜底合成；历史 run 在 API 读取时自动合成，无需迁移）
- **RAG 真实检索**：`search_kb` 做 BM25 检索（k1=1.5, b=0.75；英文/数字词 + 中文 2-gram），返回命中片段 + 来源行号 + 得分——时间线的"知识"节点即真实检索命中的知识片段
- **dsh 黑盒模型层**：解析隔离 DSH_HOME 下 `session.jsonl.zstd`（zstandard 模块 → zstd CLI 回退），提取模型 reasoning / 工具决策 / 最终输出
- **Langfuse（可选）**：`AGENT_EVAL_TRACE=langfuse` 时 LLM 调用同步云端，与本地 traces 独立完整、互不依赖

### 多 Agent 对比矩阵与 License（V2.7，Open Core）

「对比矩阵」页一次发起多个 Agent 跑同一任务集（core 卡口包或 full 全量，每格可重复 runs 对抗非确定性），后台逐格执行并实时显示进度；完成后输出：

- **彩色得分矩阵**：行=Agent、列=任务，格内为最好成绩 + 通过率（颜色：绿≥100%、黄≥50%、红<50%），点击任意格下钻该组合的每次 run，并可直达轨迹详情
- **Agent 汇总**：按任务权重加权总分、任务通过率、真实 token 成本、总耗时、平均波动 σ（稳定性）
- **自动结论**：谁总分领先/并列、成本对比、谁最稳定
- **CSV 导出**：矩阵 + 汇总一键导出（带 BOM，Excel 直接打开中文不乱码）

功能分档（feature flag，标准库 HMAC 签名，无第三方依赖）：

| 能力 | 社区版（默认） | Pro |
| --- | --- | --- |
| 对比 Agent 数 | ≤ 2 | 不限 |
| 历史批次保留 | 仅最近 1 个 | 全部 |
| 成本 / 稳定性 σ 列 | 隐藏 | 显示 |
| CSV 导出 | 禁用 | 允许 |

签发并启用 Pro License（本地/私有化）：

```bash
# 1) 签发（正式发售请用 AGENT_EVAL_LICENSE_SECRET 覆盖内置演示密钥）
python -m agent_eval.license issue --plan pro --days 365

# 2) 三选一启用（优先级从高到低）
export AGENT_EVAL_LICENSE="<token>"            # 环境变量直接给 token
export AGENT_EVAL_LICENSE_FILE=/path/key.file  # 或指定文件
# 或把 token 写入项目根 license.key（已在 .gitignore，不会误提交）
```

档位由后端接口强校验（前端限制仅为体验优化，绕过前端仍会被 403 拒绝）。当前档位见 `GET /api/license` 与页面右上角徽章。

## 目录结构

```
ai-agent-eval/
├── src/agent_eval/
│   ├── cli.py            # CLI：list-tasks / run / ci / report / workbench
│   ├── spec.py           # Task Spec 契约与加载
│   ├── runner.py         # 执行编排（fixtures → 后端 → 判定 → 评分 → run.json + traces）
│   ├── verifiers.py      # 5 种确定性校验点
│   ├── scoring.py        # 评分（权重 × 通过率）
│   ├── reporter.py       # 自包含 HTML 报告
│   ├── tools.py          # ReAct 工具集（含 search_kb BM25 检索）
│   ├── traces.py         # 轨迹分类公共规则（retrieval / tool）
│   ├── ci.py             # M1 无头 CI 门禁（gate 判定 / JUnit / Allure / 汇总）
│   ├── costing.py        # 成本模型（预计成本）
│   ├── balance.py        # DeepSeek 余额差分（实际成本）
│   ├── sandbox.py        # 命令沙箱（超时终止 + 输出编码探测回退 GBK）
│   ├── stats.py          # 多 run 采样统计（best/mean/std/pass_rate）
│   ├── judge.py          # LLM-as-a-Judge 语义判分器（verifier=llm_judge）
│   ├── observability.py  # 可选 Langfuse trace 层（默认 no-op）
│   ├── web/              # Web 工作台：app.py(FastAPI) / store.py(SQLite) / taskgen.py / static/
│   └── backends/         # 后端注册表（base / minimal_react / deepseek_harness / aider）
├── tasks/                # 任务包：manifest.yaml + <id>/spec.yaml + fixtures/（23 任务）
├── ci/gate.yaml          # 门禁配置（core / full / compare）
├── scripts/              # gen_fixtures.py + verify_*.py + build_workbench.py
├── results/              # run 产物与 CI 报告（git 忽略）
├── docs/                 # task-spec / CI_GATE_VALIDATION / DEMO / PROGRESS / 复盘
└── LICENSE               # MIT
```

## 评测口径与已知限制

* **非确定性**：LLM Agent 同任务多次运行结果可能不同，框架保留多 run 证据，报告取采样统计（best/mean/std/pass_rate）
* **适配度差异**：不同 Agent 擅长不同任务类型（如 aider 在"改已有代码"上通过，在"从零生成数据产物"上可能循环超时）——这正是评测要暴露的信息
* **判定看产物**：校验点基于工作目录真实产物，不采信 Agent 自报完成
* **已知限制**：黑盒桌面端采集器（豆包工作 / WorkBuddy 等）、插件注册表、Docker 沙箱隔离、pip 发布尚未实现；llm_judge 开放任务（T502/T503/T504）稳定性依赖模型，暂不入 core 卡口

## 路线图

* ✅ MVP：任务包 × 2 后端，HTML 对比报告
* ✅ V2.0：框架单测 + 命令沙箱 + 多 run 采样
* ✅ V2.1：Web 工作台（FastAPI + 单页前端 + SQLite）
* ✅ V2.2：LLM-as-a-Judge · Langfuse 可选分析层 · 一键启动
* ✅ V2.3（M1）：CI 质量门禁（ci 命令 / JUnit / Allure / gate 判定 / 合并阻断）· 成本核算（预计 + 余额差分）· 多 Agent 对比门禁 · 端到端演示仓库
* ✅ V2.4：轨迹回放面板（全链路时间线）· RAG 真实检索环节（search_kb + T701/T702）· dsh 黑盒模型层
* ✅ V2.5：search_kb 升级 BM25 · T701/T702 纳入 core 卡口（12 任务）
* ✅ V2.6：统一日志体系（控制台 + 文件双输出、按 10MB 时间戳切割归档、每次运行独立 run.log）
* ✅ V2.7：商业化功能 A——多 Agent 对比矩阵（批次模型 + 彩色矩阵 + 下钻 + 汇总 + 导出）· License 收费墙（Open Core）
* 🔜 下一步：团队回归看板（功能 B）· 桌面端黑盒采集器 · 插件注册表 · Docker 沙箱隔离 · pip 发布 · 混合检索/向量检索增强
