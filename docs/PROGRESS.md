# 项目进度存档

> 通用 AI Agent 评测框架（可分享、可复用）
> 仓库：https://github.com/yaoxianda-github/ai-agent-eval
> 最后更新：2026-09-04（**MVP 7/7 + V2.0 3/3 完成；V2.1 pytest 56 全绿；V2.2 已验证通过（67 全绿 + T502/T601 真实运行）**）

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

## 下一步

- **V2.2 验收**：`pytest` 预期 67 全绿 → T502 真实判分（需 DEEPSEEK_API_KEY）→ T601 真实运行 → 工作台端到端 → git 分三笔提交推送（Day3 / V2.1 / V2.2）
- **V2.2 未做项**（黑盒采集、插件注册表、Docker 沙箱、pip 发布）：按需评估
- **V2.3 候选**：更多后端横向基准、插件注册表、pip 发布
