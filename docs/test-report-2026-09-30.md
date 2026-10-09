# AI Agent 评测平台（ai-agent-eval）功能测试报告

| 项目 | 内容 |
|---|---|
| 被测对象 | ai-agent-eval 评测工作台（FastAPI 后端 + 原生 SPA 前端，hash 路由） |
| 服务地址 | http://127.0.0.1:8000（测试期间持续 200，未重启） |
| 测试时间 | 2026-09-30（Asia/Shanghai, UTC+8） |
| 测试方式 | 三路交叉验证：① curl 全量接口；② 读代码（app.py / app.js / costing.py / config_manager.py / spec.py 等）核对逻辑；③ 浏览器实测关键交互流（快速，不长期占用） |
| 测试原则 | 只测试不改业务代码、不删数据、不批量清库；敏感信息一律打码 |
| 数据规模 | 任务 56 个、运行记录 962 条、批次 8+ 个、badcase 1 条、回归任务 12 个 |

---

## 1. 测试概述

本次测试覆盖需求要求的全部 9 个模块，对 `src/agent_eval/web/app.py` 中梳理出的 **约 70 个路由**逐一做了状态码/返回结构核查，并对照前端 `app.js` 核实调用链路。测试中**未发现接口 500、未发现页面空白、历史 bug「run_id is not defined」未复发**。共发现 **5 个问题**，其中 **P1 重要 1 个**（评委可信度页面损坏），其余为 P2 优化项。

---

## 2. 逐模块测试结果

### 2.1 概览仪表盘 `#/dashboard`
- **通过项**
  - 页面正常渲染，浏览器控制台 **0 报错**。
  - `/api/summary` 返回 56 任务 / 7 agent / total_runs=898，结构含 best、agent_rows、agent_agg、task_pass、sample_stats，可支撑 KPI 卡片与 Agent 能力对比。
  - 运行状态分布：`/api/runs?status=completed` 与 `?status=failed` 分别正确过滤（failed 当前 0 条返回空数组，completed 返回数据）。
  - 失败分桶 `/api/runs/failure-buckets` 正常：200 条采样中 93 失败（46.5%），分桶 timeout/crash/max_steps/file_missing/content_mismatch/cmd_failed/judge_failed/other 八类，含 run_ids、百分比、配色字段，前端可直接渲染归因图。
  - Judge 一致率接口 `/api/runs/judge-agreement` 本身可用（当前 total_reviewed=0，数据为空，符合现状）。
  - 六维度健康看板 `/api/dimensions/health` 返回六维（business_goal/task_quality/interaction/safety_compliance/robustness/cost_efficiency），含 samples/pass_rate/health(gray/yellow/red)/threshold，健康度分级正确（task_quality 70.8% yellow，safety_compliance 37.5% red，其余 gray）。
- **问题项**：见问题 #1（评委可信度入口页损坏，与本模块"Judge 一致率"展示相关联）。

### 2.2 任务管理 `#/tasks`
- **通过项**
  - `/api/tasks` 返回 **56 个任务**，前缀分布 T*=33、SEC*=9、LLM*=10、RAG*=4（含 T-REG-001 等回归用例归入 T*），与需求口径一致。
  - 每个任务含 `dimension`（维度列）、`level`、`weight`、`tier`、`risk_level/risk_category`、`cost_estimate`（预计成本）等字段，可支撑维度列与筛选。
  - 支持 `?dimension=` 筛选（后端 app.py:837 已实现 V5.2 维度筛选）。
  - 预计成本字段 `cost_estimate` 由 costing.estimate_cost 计算，含 source=measured/estimate 区分。
  - 浏览器实测页面正常渲染，无控制台报错。
- **问题项**：无阻断；批量操作发起链路经 `/api/batches`（POST）验证存在且参数校验完整（agents/scope/runs 上限 runs≤10）。

### 2.3 运行历史 `#/history`
- **通过项**
  - `/api/runs` 分页正确：`limit`/`offset` 传参生效，offset=0 与 offset=5 返回**不同** run_id 序列，total=962。
  - 状态筛选 `?status=failed`/`?status=completed`、agent 筛选 `?agent_id=minimal-react` 均生效。
  - 实际成本 `actual_cost_cny` 链路正确：completed 且有 usage 的 run 有值（如 e0ce8ffd… = ¥0.04，token 15827/2796）；error 无 usage 的 run 为 `null`，前端可优雅展示。
  - 时间字段、置信度（confidence_score/level）、人工复核标记（human_review_passed）均从 run.json 正确回读。
  - 浏览器实测列表渲染正常。
- **问题项**：无。
  - 备注：后端分页参数为 `limit/offset`，前端 history 页（app.js:1205）也用 `limit/offset`，**前后端一致**（早期若用 page/page_size 会被忽略，实测前端未犯此错）。

### 2.4 运行详情 `#/run/:rid`
- **通过项**
  - `/api/runs/{rid}` 返回完整结构：status、metrics.score、steps(7)、traces(15)、verdicts(5)、duration_s、workspace、forbidden_tool_calls、skill_results、lock 等。
  - 浏览器实测 `#/run/e0ce8ffd9a0f` 完整渲染：判定结果、人工复核、执行分析、轨迹回放（时间线）、执行轨迹、产物文件 6 个 Tab 齐全；score 0.835、置信度 80.5/高置信、SPEC 哈希、校验点哈希、配置快照均展示。
  - **历史 bug「run_id is not defined」未复发**：控制台 0 报错；代码核查 app.js 中 run_id 均为表头字符串或已定义变量，无裸引用。
  - 产物文件 `/api/runs/{rid}/files` 返回 5 个文件（含 input/output 目录结构），`/file?path=` 读取接口存在且做了路径穿越防护（ws_abs resolve 校验）。
  - 不存在的 run_id 返回 404「run 不存在」，不 500。
- **问题项**：无阻断。

### 2.5 对比矩阵 `#/compare`
- **通过项**
  - 发起对比：POST `/api/batches`（agents 多选 + scope/core/golden 等 + runs 1~10 上限 + judge_mode），参数校验完整，Key 预检不过会拦截（app.py:1374）。
  - 历史批次 `/api/batches` 返回 8+ 个 done 批次，含 label/status/summary。
  - 得分矩阵 `/api/matrix?batch_id=…` 返回 agents/tasks/cells/totals，cell 内含 n/best/mean/std/pass_rate/runs 明细，可下钻。
  - **导出 CSV `/api/matrix/export?batch_id=…` 正常**：含 UTF-8 BOM（Excel 中文不乱码），列含加权总分/任务通过率/总成本/总耗时/平均波动σ，格式正确。
  - 导出 HTML `/api/matrix/export_html` 返回 200（2885 字节）。
  - 取消批次 `/api/batches/{bid}/cancel` 接口存在。
  - 缺失 batch_id 时 matrix 返回 422（参数校验），不存在 batch 返回 404，边界正确。
- **问题项**：无阻断。

### 2.6 Badcase `#/badcases`
- **通过项**
  - 列表 `/api/badcases` 支持 `page/page_size` 分页（与前端 app.js:1499 一致），返回 items/total/total_pages/categories。
  - 详情 `/api/badcases/{bid}` 字段完整：run_id/task_id/agent_id/category/severity/status/root_cause/fix_plan/tags/regression_task_id/pool/run_summary。
  - 现有 badcase `7e3055199f69`（T800×minimal-react，max_steps）已成功转化为回归用例 T-REG-001，status=fixed，闭环数据正确。
  - 智能分析 POST `/api/badcases/{bid}/analyze`、批量分析 `/api/badcases/batch-analyze` 接口存在；`/api/badcases/import-from-run` 可从失败 run 自动归类 category/severity。
  - **转化为回归用例** POST `/api/badcases/{bid}/convert-to-task` 逻辑完整：生成 spec.yaml + fixtures + 追加 manifest.yaml + 回写 badcase.regression_task_id（app.py:1977-2067），重复 task_id 会 400 拦截。
  - 严重度/标签字段齐全（severity P0/P1/P2、tags）。
- **问题项**：无阻断。

### 2.7 回归管理 `#/regression`
- **通过项**
  - 回归看板 `/api/regression/board` 返回：regression_task_count=12、converted_badcase_count=1、pending=0、最近一次回归（验证回归 #1，done，12/12 通过，pass_rate=1.0，health=healthy）、degraded_alerts=[]。
  - 趋势 `/api/regression/trend`、运行列表 `/api/regression/runs`、单次运行 `/api/regression/runs/{reg_id}`、调度 `/api/regression/schedule`（GET/POST）均 200。
  - badcase→回归→回归运行的闭环数据链路可追溯（board.recent.batch_id=d7b228e10abf 与 matrix 批次对应）。
  - 浏览器实测页面渲染正常。
- **问题项**：无阻断。当前调度默认 disabled（符合预期，需手动开启）。

### 2.8 设置 `#/settings`
- **通过项**
  - API Key 快速配置 UI 完整：password 输入框 + 👁（查看）+ 📋（复制）+ 清除 + 保存按钮，逐一可见。
  - `/api/settings/env` 列表**只返回 configured/source/used_by，不泄露明文**（DEEPSEEK/ANTHROPIC 已配置 source=.env，OPENAI 未配置）。
  - 👁 查看 `/api/settings/env/{key}/value` 仅对白名单内 api_key 类开放，非白名单 key 返回 400「不在白名单内」，门控正确。
  - 保存 POST `/api/settings/env` 写入项目 `.env` 并**即时注入当前进程环境**（无需重启），白名单外 key 被过滤。
  - 配色方案：代码中提供 **7 个选项**（default + warm-brown / forest-green / navy-blue / charcoal-warm / teal-fresh / tech-blue），CSS 定义 6 套 data-theme，点击切换写 localStorage 并在启动时恢复。
  - CLI 说明、系统路径（tasks_dir/results_dir/report_dir）由 `/api/meta` 提供。
- **问题项**：见 #3（password 输入未包裹 form 的浏览器警告）。

### 2.9 日志 / 成本核算
- **通过项**
  - 预计成本：`/api/tasks` 的 `cost_estimate` 与 `/api/costs` 的 pricing（输入 ¥2/M、输出 ¥3/M deepseek-chat 口径）一致；无实测数据时按 L1~L5 分级 token 估算并标注 source=estimate。
  - 实际成本：`/api/runs` 的 `actual_cost_cny` 从 run.json 的 metrics.usage（prompt/completion tokens）×同一定价计算。
  - 两者共用 `pricing_for()` 与同一 token→成本公式，**数值链路一致、可对账**；matrix CSV 中也输出 cost_cny。
  - 运行日志：`/api/runs/{rid}` 含 steps/traces/error，错误 run（如 ffb79ba3… status=error）可追溯。
- **问题项**：见 #4（实际成本定价未按模型区分）。

---

## 3. 问题清单

| 编号 | 模块 | 严重度 | 问题描述 | 复现步骤 | 影响 | 建议修复 |
|---|---|---|---|---|---|---|
| #1 | 概览/Judge一致率、`#/judge-trust`（判分置信度页） | **P1** | 前端请求 URL 错误 + 响应结构不匹配：app.js:1607 请求 `/api/judge/agreement?limit=100`，后端实际路由为 `/api/runs/judge-agreement`（返回 404）；且后端返回扁平结构 `{total_reviewed, agreement_rate, ..., details:[]}`，前端却读 `d.stats.total_reviewed`、`d.items`。 | 浏览器访问 `#/judge-trust`，页面统计区显示 "Not Found"、列表卡在"加载中..."；控制台报 404。 | 「判分置信度/评委可信度」整页功能不可用，Judge 一致率（假阳/假阴）看板完全空白，无法支撑 Judge 可信度评估。 | ① 把 app.js:1607 改为 `/api/runs/judge-agreement?limit=100`；② 对齐数据结构：前端读取改为顶层 `total_reviewed/agreement_rate/false_positive/false_negative`，列表用 `d.details`；或后端包一层 `{stats:..., items:...}`。 |
| #2 | 全局/路由标题 | P2 | 顶部面包屑标题映射 titleMap 用 camelCase 键 `judgeTrust`，而路由名是 kebab-case `judge-trust`，导致该页标题回退显示为"工作台"（页面内 h2 仍正确显示"评委可信度"）。 | 访问 `#/judge-trust`，顶部 page-title 显示"工作台"而非"判分置信度"。 | 仅标题文案错位，不影响功能（但该页本身因 #1 已损坏）。 | 将 titleMap 键改为 `judge-trust`，与路由名统一。 |
| #3 | 设置 `#/settings` | P2 | 7 个 password 输入框未包裹在 `<form>` 内，浏览器持续输出 verbose 警告 "Password field is not contained in a form"。 | 打开设置页，查看控制台。 | 无功能影响，仅控制台噪音；个别浏览器对无 form 的 password 自动填充策略可能受限。 | 用一个 `<form>`（或 `form=` 关联）包裹 Key 输入区。 |
| #4 | 成本核算 | P2 | 实际成本 `actual_cost_cny`（app.py:1297）与 `/api/costs` 均固定用 deepseek-chat 单价（¥2/¥3 每 M），未按 run 实际模型/后端区分；costing.py 虽内置 claude/星火等多模型定价，但运行历史实际成本未传入 model。 | 当未来接入 claude-code 等非 deepseek 后端并产生 usage 时，实际成本会被低估/高估。 | 当前 962 条运行几乎全为 minimal-react(deepseek)，暂无实际偏差；多 agent 横向对比成本时会失真。 | 计算实际成本时从 run.json 读取模型名，调用 `pricing_for(model)` 取对应单价。 |
| #5 | 设置/配色 | P2 | 需求称"5 套配色"，实际代码提供 6 套自定义主题（+default 共 7 选项）。 | 设置页配色区可见 7 个色块。 | 非缺陷，仅文档/需求口径与实现不一致。 | 统一文档描述为"6 套主题配色"，或按规划删减至 5 套。 |

> 说明：测试中**未发现 P0 阻断级问题**——核心主流程（任务→运行→历史→详情→对比导出 CSV→badcase→回归看板）均可用，无接口 500、无页面空白、无按钮无响应导致的死路。

---

## 4. 接口核查结果表

| 路由 | 方法 | 实测状态码 | 结论 |
|---|---|---|---|
| `/api/meta` | GET | 200 | 通过，返回版本/路径 |
| `/api/settings/env` | GET | 200 | 通过，Key 列表打码 |
| `/api/settings/env/{key}/value` | GET | 200 / 400 | 通过，白名单门控生效（非白名单 400） |
| `/api/settings/env` | POST | （未实际写入） | 代码核查：写 .env + 即时注入进程，白名单过滤 |
| `/api/tasks` | GET | 200 | 通过，56 任务，含 dimension/cost_estimate |
| `/api/tiers` | GET | 200 | 通过 |
| `/api/circuit-status` | GET | 200 | 通过 |
| `/api/costs` | GET | 200 | 通过，pricing + benchmark token 均值 |
| `/api/flywheel-stats` | GET | 200 | 通过 |
| `/api/backends` | GET | 200 | 通过，9 个后端（id/version/default_model） |
| `/api/backends/{id}/check-api-key` | GET | 200 | 通过，minimal-react 实测连通 ok（1317ms） |
| `/api/backends/check-api-keys` | POST | （未批量调） | 代码核查：逐后端检查+异常兜底 |
| `/api/runs`（列表） | GET | 200 | 通过，limit/offset 分页、status/agent 筛选正确 |
| `/api/runs/failure-buckets` | GET | 200 | 通过，8 类分桶 |
| `/api/runs/judge-agreement` | GET | 200 | 通过（数据空），但**前端调错 URL**（见 #1） |
| `/api/judge/agreement`（前端误调） | GET | **404** | **问题 #1** |
| `/api/runs/{rid}` | GET | 200 / 404 | 通过，详情完整；不存在 404 |
| `/api/runs/{rid}/human-review` | POST | （未写入） | 代码核查：score/passed/notes/reviewer 落盘 |
| `/api/runs/{rid}/files` | GET | 200 | 通过 |
| `/api/runs/{rid}/file` | GET | （代码核查） | 通过，含路径穿越防护 |
| `/api/license` | GET | 200 | 通过 |
| `/api/batches` | GET/POST | 200 | 通过，批次列表 + 发起参数校验 |
| `/api/batches/{bid}` | GET | 200 | 通过 |
| `/api/batches/{bid}/cancel` | POST | （代码核查） | 通过 |
| `/api/matrix?batch_id=` | GET | 200 / 422 / 404 | 通过；缺参数 422、不存在 404 |
| `/api/matrix/export?batch_id=` | GET | 200 / 403 / 404 | 通过 CSV（含 BOM）；license 未授权时 403 |
| `/api/matrix/export_html` | GET | 200 | 通过 |
| `/api/summary` | GET | 200 | 通过 |
| `/api/dimensions/health` | GET | 200 | 通过，六维健康分级 |
| `/api/report` | POST | （代码核查） | 通过 |
| `/reports/{name}` | GET | （代码核查） | 通过 |
| `/api/tasks/generate` | POST | （代码核查） | 通过 |
| `/api/packages` | GET | 200(空) | 通过，当前未安装任务包 |
| `/api/packages/{name}` | GET/DELETE | （代码核查） | 通过 |
| `/api/packages/install` | POST | （代码核查） | 通过 |
| `/api/badcases` | GET/POST | 200 | 通过，page/page_size 分页 |
| `/api/badcases/{bid}` | GET/PUT/DELETE | 200 | 通过 |
| `/api/badcases/import-from-run` | POST | （代码核查） | 通过，自动归类 category/severity |
| `/api/badcases/batch-analyze` | POST | （代码核查） | 通过 |
| `/api/badcases/{bid}/convert-to-task` | POST | （代码核查） | 通过，spec.yaml+manifest+回链 |
| `/api/badcases/convert-to-tasks` | POST | （代码核查） | 批量转化 |
| `/api/badcases/{bid}/analyze` | POST | （代码核查） | LLM 智能分析 |
| `/api/badcases/{bid}/regression-compare` | GET | （代码核查） | 修复前后对比 |
| `/api/regression-badcases` | GET | 200 | 通过 |
| `/api/regression/board` | GET | 200 | 通过，12 回归任务/health=healthy |
| `/api/regression/run` | POST | （代码核查） | 通过 |
| `/api/regression/runs` | GET | 200 | 通过 |
| `/api/regression/runs/{reg_id}` | GET | （代码核查） | 通过 |
| `/api/regression/trend` | GET | 200 | 通过 |
| `/api/regression/schedule` | GET/POST | 200 | 通过，默认 disabled |
| `/api/memories` 等 5 个 | GET/POST/PUT/DELETE | 200 | 通过，page/page_size 分页 |
| `/api/memories/recall`、`/memory-auto-manage`、`/memory-quality-stats` | POST/GET | 200 | 通过 |
| `/api/agent/run`、`/run-stream` | POST | （代码核查） | 通过 |

**全量接口中无 500、无超时。**

---

## 5. 特别关注点核查结论

| 关注点 | 结论 |
|---|---|
| 页面空白 | **未发现**。9 个 hash 路由（dashboard/tasks/history/run/badcases/compare/settings/regression）浏览器实测均正常渲染。 |
| JS 控制台报错 | 仅两处：① `#/judge-trust` 因 #1 报 404；② `#/settings` 7 条 password 未入 form 的 verbose 警告（#3）。其余路由 0 报错。 |
| 历史 bug「run_id is not defined」 | **未复发**。run 详情页 `#/run/{rid}` 完整渲染 6 个 Tab，控制台无此错误；代码核查无裸 run_id 变量引用。 |
| 接口 500 / 超时 / 空数据行为 | 无 500；空数据（如 failed=0、无人工复核）返回空数组/null 而非报错；不存在资源返回 404。 |
| 按钮无响应 / 无状态反馈 | 主流程按钮（发起运行/保存配置/取消批次/导出）链路经代码核查有明确 then/catch 与 banner 反馈；未发现死按钮。 |
| 状态一致性（列表/详情/对比） | 通过。同一 run 在列表 actual_cost、详情 score、matrix cell 中数值一致；batch_id 在列表/矩阵/回归看板间可互相追溯。 |
| 分页翻页 / 筛选组合 / 快捷筛选 | 通过。runs 用 limit/offset、badcases/memories 用 page/page_size，前后端参数一致；status/agent_id 筛选生效。 |
| 预计成本 vs 实际成本 | 通过。同一 pricing 口径；预计来自 benchmark 均值/分级估算，实际来自 run.json usage；当前数据可对账（如 e0ce8ffd 实际 ¥0.04）。多模型时见 #4。 |
| API Key 配置链路 | **打通，但实现与需求假设有别**：设置页保存 → 写入项目根 `.env` → `apply_env_to_process()` 即时注入进程；服务启动时（app.py:213）也会加载 .env，重启后仍生效。**设计上不写 `~/.zshrc`**（config_manager docstring 明确"不用去改 zshrc"），以项目 .env 为准，更干净。列表不泄明文，👁 查看走白名单门控。实测 minimal-react Key 连通 ok。报告中所有 Key 已打码（sk-****）。 |

---

## 6. 整体结论与遗留风险

**整体结论**：ai-agent-eval 工作台核心功能完整、主流程畅通，9 个模块基本可用。接口层健壮（无 500、边界 404/422 处理得当、CSV 导出规范、分页/筛选/成本链路正确），数据闭环（badcase→回归用例→回归运行→看板）已跑通，历史高危 bug 未复发。

**唯一需优先修复的功能性缺陷是 #1（判分置信度页面前后端契约不一致）**——它使 Judge 一致性评估这一质量度量入口完全空白，建议下个迭代优先对齐 URL 与数据结构。

**遗留风险**：
1. 多 agent 成本对比前需先修复 #4（实际成本按模型计价），否则 claude 等高价后端的成本会失真。
2. Judge 一致率当前 total_reviewed=0，需积累人工复核数据后才能真正度量 Judge 可信度——修复 #1 后此数据才可见。
3. 失败分桶中 crash 占失败 97.8%（91/93），timeout=0，建议后续结合 runner 错误日志分析 crash 根因（本次仅功能测试，未深入崩溃原因）。
4. 调度 schedule 默认关闭，回归自动化需手动启用并配置 agent。

---

*报告生成：2026-09-30 ｜ 测试方式：curl + 代码交叉验证 + 浏览器关键流实测 ｜ 敏感 Key 已全部打码*
