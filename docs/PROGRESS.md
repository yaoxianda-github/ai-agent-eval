# 项目进度存档

> 通用 AI Agent 评测框架（可分享、可复用）
> 仓库：https://github.com/yaoxianda-github/ai-agent-eval
> 最后更新：2026-09-04（**MVP 7/7 + V2.0 3/3 完成；V2.1 Web 工作台实现完成，待最终验收**）

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
- **状态**：浏览器可正常打开、布局完成；pytest 56 全绿待最终确认；git 待提交

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
- **待提交（工作树积压）**：V2.0-Day3 多 run 采样 / V2.1 全部（web/ + tests/test_web.py + pyproject + README + V2_PLAN + PROGRESS）

## 下一步

- **V2.1 最终验收**：`pytest` 56 全绿 → 工作台端到端（跑任务 → 下钻 → 对比 → 报告）→ git 分两笔提交推送
- **V2.2**：Langfuse 追踪（分析层）、LLM-as-a-Judge（T502 开放任务）、更多后端/任务、黑盒桌面端采集、一键打包启动
