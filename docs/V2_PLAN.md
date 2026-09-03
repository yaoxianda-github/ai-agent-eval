# V2 实施计划：本地优先 Web 评测工作台

> 更新：2026-09-04（**V2.0 完成 3/3**；V2.1 已实现 + 修复完成；V2.2 已实现并验证通过，待提交）
> 定位：给测试工程师使用的、**本地优先的 Web 评测工作台**（用户已确认方向）
> 高保真原型：`docs/workbench_prototype.html`（已定稿，6 个页面，含任务管理）

## 一、V2 目标与范围

- **最终形态**：浏览器操作（localhost）完成"新建任务 → 选后端/参数 → 运行 → 下钻结果 → 对比 → 看报告"全流程，无需命令行。
- **核心约束**：现有 CLI 引擎（任务契约 / 后端注册表 / 判定 / 评分 / 报告）**100% 复用，不重写**；Web 只是外壳。
- **明确不做**（V2.1）：鉴权、多用户、在线部署、Docker 管理 UI、跨设备同步——纯本地单机。
- **Langfuse 分工**：V2.2 作为"深度 trace 分析层"接入（成本/token/每次调用），**不替代**评测工作台（编排+判定层）。工作台看"评测结论"，跳 Langfuse 看"为什么这么表现"。

## 二、架构（引擎复用 + Web 外壳）

```
单页前端（浏览器）—— 选任务/后端/参数 → 运行 → 下钻 verdict/轨迹 → 对比
        ▲ API（异步运行 + 进度 + run 历史）
FastAPI 服务层（list-tasks / list-backends / run / runs / report）
        ▲ 调用
现有 CLI 引擎（复用）：tasks / backends / runner / verifiers / scoring / reporter
        └── SQLite：run 历史持久化（V2.1）
```

## 三、阶段与天数

### V2.0 引擎地基（3 个工作日）——打牢 Web 外壳的地基

| 天 | 内容 | 验收（DoD） | 状态 |
|---|---|---|---|
| **Day1** | 框架自身单测：verifiers / scoring / spec / runner | pytest 32 用例全绿（含 Day3"验证验证器"回归） | ✅ 完成 |
| **Day2** | run_command 沙箱隔离：子进程 + 超时强杀 + 输出截断 | pytest 43 用例全绿；T001 真实任务回归 4/4 | ✅ 完成 |
| **Day3** | 多 run 采样：`--runs N` + 均值/最好/方差 统计 | `agent-eval run --runs 3` 输出统计；报告含采样区块 | ✅ 完成 |

### V2.1 Web 工作台 MVP（6-10 个工作日，1-2 周）——按原型实现

> 状态：**已实现**（2026-09-03 晚落盘，09-04 完成自测与修复），待最终验收（pytest 56 全绿 + 端到端走查 + git 提交）。

**已完成的修复（2026-09-04）**：
- Python 3.9 类型兼容：pydantic 运行时求值 `str | None` 失败 → `Optional[str]`
- SQLite 跨线程：后台 run 线程写入 + API 请求线程读取 → `check_same_thread=False` + 线程锁
- run_id 一致性：`run_one` 支持传入 `run_id`（Web 预生成 id 与实际落盘目录对齐）
- 前端白屏：历史页一处字符串引号不配对（JS 语法错误）→ 修复
- UI 布局：内容区完全拉满到屏幕右侧

验收后置事项：`pytest` 56 全绿确认 → 工作台端到端走查 → git 提交推送（与 V2.0-Day3 分两笔 commit）。

| 页面（对齐原型） | 后端能力 |
|---|---|
| 工作台（运行控制台） | FastAPI：任务列表 / 后端列表 / 参数 / 异步运行 + 进度轮询 |
| 任务管理（新建任务） | 任务 spec 生成（YAML + fixtures 骨架 + 校验点动态编辑器 + manifest 更新） |
| 运行详情（判定+轨迹下钻） | run 详情 API（verdicts / steps / 产物预览） |
| 对比（Agent×任务矩阵） | 聚合多 run 统计（均值/最好/方差） |
| 运行历史 | SQLite run 索引，筛选 / 下钻 |
| 报告 | 复用 reporter 生成 HTML，iframe 内嵌 |

技术选型：**FastAPI + 原生 JS 单页**（工程感强、面试可深挖、适合控制型工作台）；SQLite 持久化 run 历史索引。

实现清单：`src/agent_eval/web/`（app.py / store.py / taskgen.py / __main__.py / static/）、`tests/test_web.py`（13 用例，TestClient + FakeBackend）。

### V2.2 增强（2026-09-04 晚实现，已验证通过）

**已实现**：
- **LLM-as-a-Judge 判分器**：`judge.py`（client 可注入、无 key/失败/无产物诚实降级、verdict 与确定性同构 + score/reasoning）；`runner` 在 `verifier=llm_judge` 时追加语义判分；`TaskSpec` 新增 `rubric` 字段；T502 配自定义 rubric 闭环；任务生成器/前端表单支持 llm_judge + rubric
- **Langfuse 可选分析层**：`observability.py` 默认 no-op，`AGENT_EVAL_TRACE=langfuse` + 凭据时记录每次 LLM 调用（token/耗时）；minimal_react 每步与 judge 判分均已埋点；未装 SDK 静默降级
- **新任务 T601（日志错误统计）**：确定性判定 + `scripts/verify_t601.py` 复核脚本，任务包扩至 6 个
- **一键启动**：`agent-eval workbench` 命令 + `start_workbench.bat`（双击启动）+ `scripts/build_workbench.py`（PyInstaller 打包）

**未做（明确不做）**：黑盒桌面端采集器（豆包工作/WorkBuddy，需桌面级自动化，超出本环境能力）· 插件注册表 · Docker 沙箱隔离 · pip 发布。

自测：`tests/test_judge.py`（FakeClient 注入，7 用例）+ `tests/test_observability.py`（3 用例）+ `test_web.py` 新增 llm_judge/rubric 生成用例；全量 pytest **67 全绿（已确认）**；T502 真实判分 6/6（score 0.92）、T601 真实运行 3/3（score 1.0）均已通过。

## 四、里程碑与 Git

- [x] `71b67bf` V2.0-Day1 框架单测 + Day2 命令沙箱（已推送）
- [x] `ea36faf` sandbox Windows 清理竞态修复（已推送）
- [x] `ac66575` V2 实施计划 + V2.0-Day2 收尾（已推送）
- [ ] V2.0-Day3 多 run 采样 + 统计 + 报告采样区块（待提交）
- [ ] V2.1 Web 工作台：FastAPI + 单页前端 + SQLite + 13 个 web 用例（实现+修复完成，待最终验收/提交）
- [ ] V2.1 全流程打通（新建任务 → 运行 → 下钻 → 对比 → 报告）
- [ ] V2.2 实现（LLM-as-a-Judge / Langfuse / T601 / 一键启动）验收：pytest 67 全绿 + T502 真实判分 + T601 真实运行
- [ ] V2.2 未做项评估：黑盒采集器、插件注册表、Docker 沙箱、pip 发布

## 五、验收总标准

1. 浏览器全程操作、零命令行；
2. 真实 run 证据（轨迹/verdict/产物）全保留、可下钻；
3. 现有 CLI 引擎零重写（只新增，不改语义）；
4. 每日有可验收 DoD；Git 全程留痕。
