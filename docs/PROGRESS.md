# 项目进度存档

> 通用 AI Agent 评测框架（可分享、可复用）
> 仓库：https://github.com/yaoxianda-github/ai-agent-eval
> 最后更新：2026-09-26（**V5.0 团队回归看板**——badcase 批量转回归用例 + 定期自动回归 + 退化检测 + 趋势图）

## 冲刺进度

| 天 | 内容 | 状态 |
|---|---|---|
| Day 1 | 项目骨架 + 任务包契约（task-spec@v1）+ 5 个任务 + fixtures + 判定脚本 | ✅ |
| Day 2 | Runner 编排 + 自建 ReAct 后端（minimal-react v0.2.0） | ✅ |
| Day 3 | 判定器（5 种校验点）+ 评分器；Agent 健壮性加固；判定器路径 bug 修复 | ✅ |
| Day 4 | 接入 Aider（第三方 CLI）双后端对比 | ✅ |
| Day 5 | 报告生成器（reporter.py + agent-eval report） | ✅ |
| Day 6 | README 完整化 + `--timeout` 参数 | ✅ |
| Day 7 | 演示脚本 + 面试预案（docs/DEMO.md） | ✅ |

## V2.0 引擎地基（3 天）

| 阶段 | 内容 | 状态 |
|---|---|---|
| Day1 | 框架自身单测：verifiers / scoring / spec / runner → 43 用例 | ✅ |
| Day2 | run_command 沙箱：子进程 + 超时强杀 + 输出截断 | ✅ |
| Day3 | 多 run 采样 `--runs N` + 均值/最好/方差 统计 + 报告采样区块 | ✅ |

## V2.1 Web 评测工作台（2026-09-03 晚 ~ 09-04）

- **后端**：FastAPI 服务层（14 个 API）+ SQLite run 历史索引（run.json 为权威）+ 任务包生成（spec.yaml + fixtures 骨架 + manifest 更新）+ 产物安全预览（路径穿越防护 / 类型白名单 / ≤200KB）
- **前端**：原生 JS 无构建 SPA，7 视图（工作台 / 任务管理 / 历史 / 详情 / 对比 / 报告 / 设置），2s 轮询进度，校验点动态增删编辑器
- **自测**：`tests/test_web.py` 13 用例（TestClient + FakeBackend，不依赖外部 LLM）；全量 56 用例
- **验证与修复**：
  1. `pip install -e ".[web]"` 装齐 fastapi 0.128 / uvicorn 0.39 / python-multipart
  2. **Python 3.9 类型兼容**：pydantic 无法运行时求值 `str | None` → 改 `Optional[str]`
  3. **SQLite 跨线程**：后台 run 线程写入 + API 请求线程读取 → `check_same_thread=False` + 线程锁
  4. **run_id 不一致**：`run_one` 内部自生成 vs Web 预生成 → `run_one` 支持传入 `run_id`
  5. **前端白屏**：历史页一处字符串引号不配对（JS 语法错误）→ 修复
  6. **UI 布局**：内容区完全拉满到屏幕右侧
- **状态**：浏览器可正常打开、布局完成；**pytest 56 全绿（已确认）**；git 待提交

## V2.2 增强（2026-09-04 晚实现，已验证通过）

- **LLM-as-a-Judge 语义判分**：`judge.py`（client 可注入 / 无 key·失败·无产物诚实降级 / verdict 与确定性同构 + score/reasoning）；runner 在 `verifier=llm_judge` 时追加判分；TaskSpec 新增 `rubric` 字段；T502 配自定义 rubric 闭环；任务管理页支持 llm_judge + rubric
- **Langfuse 可选分析层**：`observability.py` 默认 no-op，启用后记录每次 LLM 调用（token/耗时）；minimal_react 每步 + judge 判分均已埋点；未装 SDK 静默降级
- **新任务 T601**（日志错误统计，确定性 + `scripts/verify_t601.py` 复核）——任务包扩至 6 个
- **一键启动**：`agent-eval workbench` / `start_workbench.bat` / `scripts/build_workbench.py`（PyInstaller 打包）
- **明确不做**：黑盒桌面端采集器（需桌面级自动化）、插件注册表、Docker 沙箱、pip 发布
- **自测**：`tests/test_judge.py`（7）+ `tests/test_observability.py`（3）+ test_web 新增 llm_judge/rubric 用例；全量 pytest **67 全绿（已确认）**
- **真实链路验证（已确认）**：T502 真实 LLM 判分 6/6（score=0.92，含 reasoning）；T601 确定性任务 3/3（score=1.0，含独立复核脚本）
- **修复记录**：T601 校验口径两处 bug——① 日志格式为「时间戳 ERROR 消息」，`startswith("ERROR")` 恒为 0，改按级别字段匹配；② 正则 token 数写错多算一位，修正后 3/3

## MVP 交付物

- **CLI 三条命令**：`agent-eval list-tasks / run / report`
- **5 个任务**（L1-L5）× **2 个后端**（minimal-react 白盒基线 / aider 黑盒第三方）
- **自包含 HTML 报告**（后端×任务得分矩阵 + 汇总 + 通过率 + 口径/适配度说明）
- **GitHub 开源仓库**（MIT），含 README、task-spec 规范、进度与演示文档

## 关键结论（面试素材）

1. **LLM Agent 非确定性**：同 Agent 同任务多次运行结果不同 → 加固解析 + temperature=0 + 多 run 保留证据、报告取最好成绩
2. **判定看产物、不看自报**：max_steps 未收尾但产物正确照样 4/4
3. **谁来验证验证器**：判定器自身出过路径 bug，靠证据链定位修复
4. **Agent × 任务类型适配度**：aider 擅长改已有代码、在"从零生成数据产物"任务上循环超时——评测暴露"没有银弹"
5. **后端适配层**：注册表 + 适配层隔离不同 Agent 的接口差异（--file / git baseline / stdin DEVNULL）

## Git 历史

- **MVP（已推送）**：b19dd1b Day 1-3 · 4361d35 Day 4-5 · d0626b4 Day 6 · 87d7786 Day 7
- **V2.0（已推送）**：71b67bf Day1 单测+Day2 沙箱 · ea36faf Windows 清理竞态 · ac66575 V2 计划+Day2 收尾
- **待提交（工作树积压）**：V2.0-Day3 多 run 采样 / V2.1 全部（web/ + tests/test_web.py + pyproject + README + V2_PLAN + PROGRESS）/ V2.2（judge.py + observability.py + T601 + 一键启动 + 测试 + 文档）

## V2.3 CI 质量门禁与成本核算（2026-09-05 ~ 09-08）

- **`agent-eval ci` 无头命令**：`ci.py` 支持 `--gate core/full`、`--junit-xml`、`--allure-dir`、`--agent`，按 `ci/gate.yaml` 执行卡口，通过率不达标退出码非 0
- **JUnit XML + Allure 报告**：标准测试报告格式，GitHub Actions 直接消费
- **合并阻断验证**：演示仓库 `agent-eval-demo` 实跑 workflow，core 包通过率不达标自动阻断 PR 合并（`--gate core` 正式卡口）
- **成本核算**：`costing.py` 预计成本（任务级 token 模型）+ `balance.py` 实际成本（DeepSeek 余额差分），工作台与 CI 报告均展示
- **多 Agent 对比门禁**：gate 配置 `agents:` 列表，每 Agent 独立跑同一任务集，全部达标才 PASS

## V2.4 轨迹回放与 RAG 真实检索（2026-09-08 ~ 09-10）

- **轨迹回放面板**：运行详情以时间线展示全链路（输入意图 → 知识/检索 → 模型生成 → 工具执行），支持类型过滤与展开
- **RAG 真实检索**：`search_kb` 工具（BM25 检索）替代文件读取式伪检索，T701/T702 带干扰项知识库
- **dsh 黑盒模型层**：解析隔离 DSH_HOME 下 `session.jsonl.zstd`，还原模型 reasoning / 工具决策 / 最终输出
- **Langfuse 可选分析层**：`AGENT_EVAL_TRACE=langfuse` 时 LLM 调用同步云端

## V2.5 ~ V2.8 工程化与商业化（2026-09-10 ~ 09-15）

- **V2.5**：search_kb 升级 BM25（k1=1.5, b=0.75），T701/T702 纳入 core 卡口（12 任务）
- **V2.6 统一日志体系**：控制台 + 文件双输出，单文件超 10MB 按时间戳切割，每次运行独立 `run.log`
- **V2.7 多 Agent 对比矩阵**：批次模型发起 N 个 Agent × 任务集 × runs 对比，彩色矩阵 + 下钻 + CSV 导出；License 社区/Pro 收费墙（Open Core）
- **V2.8 API Key 智能管理**：Web 设置页可视化配置写入 `.env`，优先级高于系统环境变量；`.env` 无效时自动回退 `~/.zshrc`；发起对比前自动预检 Key 连通性；claude-code 代理支持；对比批次随时取消；Badcase 管理模块（标记、分类、转化回归用例）；10+ 后端接入（claude-code / codex / hermes / kimi / qoder / trae / workbuddy）

## V2.9 评分升级与工程能力（2026-09-16 ~ 09-20）

- **三层评分体系**：规则评分 70% + 轨迹效率 30%（步数 40% + 错误数 40% + 重复调用 20%），`scoring.py` 新增 `score_trajectory()`，`score_task()` 输出综合得分
- **自动重试机制**：`runner.py` 默认重试 1 次，针对 timeout/error 自动重试，重试前清空 workspace 产物，等待 1 秒后重试
- **异步并发执行**：`web/app.py` 使用 ThreadPoolExecutor，默认并发数 3（环境变量 `EVAL_CONCURRENCY` 配置），单个任务失败不影响其他任务
- **版本对比报告**：`reporter.py` 新增 `compare_versions()` 函数，支持两个版本的任务得分对比
- **HTML 报告导出**：新增 `/api/matrix/export_html` 接口，导出自包含 HTML 格式对比报告
- **Badcase 智能分析**：Bug 分析工作流（现象→影响→根因→修复→回归→风险→确认），`/api/badcases/{id}/analyze`
- **经验记忆沉淀**：Badcase 自动沉淀为经验记忆，支持召回、自动管理、质量统计
- **智能评测流式输出**：`/api/eval-agent/stream` SSE 流式输出评测过程
- **任务包管理**：任务包 CRUD、安装、删除接口
- **UI/UX 持续优化**：运行历史快捷筛选（只看失败/今天/claude-code）、首页/尾页快捷按钮、时间格式标准化、对比矩阵页面布局调整、5 套配色主题切换

## 当前能力总览

- **任务**：42+ 评测任务，L1-L5 五级难度，含 file/data/rag/code/sec 等标签
- **后端**：10+ Agent 后端（minimal-react / deepseek-harness / claude-code / aider / codex / hermes / kimi / qoder / trae / workbuddy）
- **评分**：三层评分（规则 70% + 轨迹效率 30%）+ LLM Judge 语义判分
- **报告**：自包含 HTML + CSV + JUnit XML + Allure
- **CI/CD**：`agent-eval ci` 无头命令 + GitHub Actions 合并阻断
- **商业化**：Open Core，社区版/Pro License 分档

## V5.0 团队回归看板（功能 B，2026-09-26）

- **Badcase 批量转回归用例**：`POST /api/badcases/convert-to-tasks`，一键将待处理 badcase 全部转为 T-REG-NNN 回归任务（自动编号、生成 spec.yaml、更新 manifest、badcase 状态置 fixed）
- **立即回归**：`POST /api/regression/run`，复用批次机制跑 regression_pack 任务集，创建回归运行记录
- **定期回归**：`regression_schedule` 配置（间隔小时/Agent/启用），后台调度线程每 60s 检查到点自动触发
- **退化检测**：与最近一次更早的已完成回归对比，得分下降 Δ<-0.15 判定退化并记录明细
- **回归看板**：新前端视图（#/regression）——KPI（任务数/转化数/运行数/通过率/健康状态）+ 操作区 + 退化告警 + SVG 通过率趋势图 + 回归历史表
- **API**：`/api/regression/board|runs|trend|schedule`、`/api/regression/run`、`/api/badcases/convert-to-tasks`
- **验证**：真实回归 12 任务 × minimal-react 通过率 100%；test_web.py 新增 4 用例（空看板/批量转化/回归回写与退化/调度配置），全绿

## 下一步

- **团队回归看板（功能 B）**：Badcase 自动转化回归用例，定期自动跑，跟踪修复效果
- **桌面端黑盒采集器**：豆包工作 / WorkBuddy 等桌面 Agent 的行为采集
- **插件注册表**：后端插件化，第三方 Agent 无需改框架即可接入
- **Docker 沙箱隔离**：评测任务在容器内执行，避免污染宿主环境
- **pip 发布**：正式发布到 PyPI
- **混合检索增强**：BM25 + 向量检索混合召回，提升 RAG 评测真实性
