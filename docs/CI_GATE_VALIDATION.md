# agent-eval ci —— CI/CD 集成与质量门禁 · 端到端验证说明

> 日期：2026-09-06 ｜ 关联仓库：[ai-agent-eval](https://github.com/yaoxianda-github/ai-agent-eval)（主仓库）、[agent-eval-demo](https://github.com/yaoxianda-github/agent-eval-demo)（演示仓库）
> 一句话：把 `agent-eval ci` 无头门禁接入 GitHub Actions，**实测"核心任务包通过率不达标 → 自动阻断代码合并"全链路成立**。

---

## 1. 背景与目标

M1 交付了 `agent-eval ci` 命令骨架（`src/agent_eval/ci.py` + `cli.py` 注册，见主仓库 commit `f8116a5`），支持：

- 无头模式（Headless）执行评测门禁，退出码 0（达标）/ 1（不达标）；
- 输出三件套报告：**JUnit XML**（checkpoint 级 testcase）、**Allure results**、**汇总 JSON**；
- 门禁按 `ci/gate.yaml` 中的包（package）组织：`demo`（精简演示）与 `core`（正式卡口）。

本次验证目标（用户指定验收标准）：

1. 在 GitHub 建演示仓库 `yaoxianda-github/agent-eval-demo`，实跑 `agent-eval ci` 门禁 workflow；
2. 配置 branch protection required check，端到端验证**通过放行 / 失败阻断**两类 PR；
3. 用 dorny/test-reporter 在 PR 发布 JUnit 结果；
4. README 验证记录表填实。

## 2. 架构与链路

```
开发者提交 PR / push
  └─ GitHub Actions（.github/workflows/agent-eval.yml，job: agent-eval demo gate）
       ├─ actions/checkout@v4           # 检出演示仓库（含 tasks/ 与 scripts/）
       ├─ actions/setup-python@v5(3.10)
       ├─ pip install 主仓库 git+... + Pillow   # 安装 agent-eval 及 verify 运行依赖
       ├─ 准备 python 软链 → setup-python 环境   # verify 脚本与 Pillow 同一解释器
       ├─ agent-eval ci --gate demo --junit-xml ... --allure-dir ... --report-json ...
       │     ├─ 通过（退出码 0）→ job 绿 → required check 通过 → 允许合并
       │     └─ 不达标（退出码 1）→ job 红 → branch protection 阻断合并
       ├─ dorny/test-reporter（fail-on-error=false）→ JUnit check 发布到 PR
       └─ upload-artifact → Allure results 随 artifact 上传（本地 allure generate 出报告）
```

关键语义决策：**阻断由 `agent-eval ci` 退出码 + required check 承担**；JUnit 报告里出现失败用例**不再单独标红 job**（`fail-on-error: "false"`），避免"gate 已通过但报告有失败用例"造成的误阻断。

## 3. 门禁配置（ci/gate.yaml）

```yaml
# demo —— 演示用精简门禁（快、便宜，验证链路）
demo:  [T106, T205, T305, T306]   # runs=2, task_pass_ratio=0.5, min_pass_rate=0.75

# core —— 正式上线卡口（后续扩展目标）
core:  [T001, T102, T103, T207, T305, T306, T308, T303, T401, T402]  # runs=3, task_pass_ratio=0.5, min_pass_rate=0.9
```

- `task_pass_ratio=0.5`：任务在多次 run 中 ≥50% 通过即判定任务 PASS；
- `min_pass_rate`：包内任务通过率下限，低于即门禁 FAIL（退出码 1）。

## 4. 验证过程：12 次 Actions run 的排障链

> 演示仓库 workflow 从"跑不起来"到"全绿"，共 12 次 run。每次失败都定位并修复了独立根因，这是最有价值的沉淀。

| Run | 提交 | 现象 | 根因与修复 |
|---|---|---|---|
| #1 | d7aa8cd | Docker action 构建失败 | `simple-elf/allure-report-action@v1.9` 在 runner 上无法构建 → **移除**，Allure results 改为随 artifact 上传、本地 `allure generate` |
| #2 | 7fe9e5e | 0/4 任务过、退出码 1 | **门禁机制本身验证成立**（54s、8 checkpoint、JUnit 生成）；test-reporter 因 0 通过置红 |
| #3 | e39da15 | c1 全过、c2 全挂 | c2 均为 `python @scripts/verify_*.py` → ubuntu 24.04 无 `python` 命令 → 加软链（无效，见 #9） |
| #4 | 5c80045 | 仍全挂 | 补上缺失的 `scripts/`（21 文件）→ 仍挂，怀疑路径解析 |
| #5 | fe3b214 | YAML 语法错误 L59 | debug 步骤 heredoc `<< 'PYEOF'` 与 YAML 锚点冲突 → 改 `python3 -c` 单行 |
| #6 | 7094b8e | agent 行为正常但校验失败 | debug 输出证明 agent 每次产出合规产物（如 T306 算出 162.00 写入 answer.txt）→ 锁定为**校验脚本路径问题** |
| #7 | 004f387 | T106/T305/T306 PASS、T205 挂 | **根因修复生效**（主仓库 `8968352`：`find_scripts_dir()`，env → cwd/scripts → 包默认）；T205 需 Pillow |
| #8 | abea172 | job 绿（T205 仍挂，gate 恰好 0.75） | workflow 安装 Pillow；test-reporter `fail-on-error=false`；阻断语义归退出码 |
| #9 | da3361f | 全 PASS | **python 软链指向 setup-python 环境**（`$(which python3)`），verify 与 Pillow 同一解释器 |
| #10 | 43842e9 | gate PASS（0.75 恰好达标） | PR #1 首版改坏 T305（工龄条件 ≥99），仅 1 任务挂 → 通过率仍达标 |
| #11 | dacd4e2 | **gate FAIL（0.50 < 0.75）** | PR #1 二版再改坏 T306（订单总额 +999 偏移）→ 2 任务挂 → 阻断生效 |
| #12 | 6251477 | 全 PASS | PR #1 还原校验 → 放行 → 合并 |

**主仓库侧两处代码修复**（已推送 origin/main）：

1. `8968352`：`verifiers.py` 的 `_SCRIPTS_DIR` 原为 `Path(__file__).resolve().parents[2] / "scripts"`，源码树可用但 **pip 安装态指向 site-packages 上层（包内无 scripts/）** → 新增 `find_scripts_dir()`：`AGENT_EVAL_SCRIPTS` env → `cwd/scripts` → 包默认，配套 3 个定位测试（全量 pytest 80 passed）。
2. `f8116a5`：M1 `agent-eval ci` 命令骨架 + 10 个测试。

## 5. 端到端验证结果

### 5.1 阻断路径（PR #1 坏代码版本）

| 检查项 | 结果 | 证据 |
|---|---|---|
| 门禁判定 | gate 0.50 < 0.75 → FAIL（T305 + T306 挂） | Run #11 日志 |
| required check | 红（1 failing check · Required） | PR #1 Checks 页 |
| 合并按钮 | **`aria-disabled=true`，无法合并** | 截图见 `images/gate-blocked.png` |
| main 直接 push | **被拒：`protected branch hook declined`**（保护规则同时拦住绕过路径） | git push 输出 |

### 5.2 放行路径（PR #1 修复版本）

| 检查项 | 结果 | 证据 |
|---|---|---|
| 门禁判定 | 4 任务 PASS → ≥0.75 → 退出码 0 | Run #12 日志（succeeded 1m49s） |
| PR 状态 | **`Ready to merge`**，合并按钮恢复可用（`aria-disabled=false`） | 截图见 `images/gate-unblocked.png` |
| 合并 | PR #1 已合并进 main（3 commits） | GitHub PR #1（Merged） |

### 5.3 分支保护配置（关键项）

- 规则：Settings → Branches → classic protection rule（id `82797648`）→ `main`
- **Require status checks**：`agent-eval demo gate`（job name，即 check run name，已用 REST API 核验存在）
- **Include administrators（enforce_admins）**：勾选——否则管理员（如仓库 owner）可绕过保护，阻断对 owner 无效
- 状态检查搜索小坑：经典规则 UI 的搜索框输入后下拉选项渲染在 portal 中，需点选 `li` 项；表单最终提交需触发原生 `form.requestSubmit()`（部分按钮 `bu.click` 首击不触发 React 提交）

## 6. 成本数据（实测）

| Actions Run | 场景 | 耗时 | token（in/out） | 成本 |
|---|---|---|---|---|
| #8 | demo 门禁（4 任务 × 2 runs） | ~60s | 40,475 / 3,903 | ≈ ¥0.093 |
| #10 | 同上（PR 首版） | 50s | 32,281 / 3,598 | ≈ ¥0.075 |
| #11 | 同上（门禁 FAIL） | ~1m | — | ≈ ¥0.08 |
| #12 | 同上（放行版） | 1m49s | — | ≈ ¥0.09 |

> 单次 demo 门禁 ≈ 1 分钟、**成本 < ¥0.1**（deepseek-chat 计价）。
> 正式卡口 `core`（10 任务 × 3 runs）预计 3–5 分钟、约 ¥0.5–1.0；runner 分钟数（GitHub Actions 免费额度）需按组织账单另计。

### 6.1 core 正式卡口实跑记录（2026-09-06 本地，minimal-react @ deepseek-chat）

实跑命令：`.venv/bin/agent-eval ci --gate core`（10 任务 × 3 runs = 30 次执行）

| 指标 | 实测值 |
|---|---|
| 门禁判定 | **FAIL（0.8 < 0.9）**，8/10 任务通过 |
| 总耗时 | **707.7s（≈ 11.8 分钟）** |
| tokens | 304,675 prompt + 23,146 completion |
| 成本 | **≈ ¥0.68**（`ci-report.json` cost_cny=0.6788） |
| JUnit | 28 tests / 6 failures（checkpoint 级） |

任务明细（runs = 3 次采样）：

| 任务 | 级别 | 通过 | 判定 | 失败 checkpoint |
|---|---|---|---|---|
| T001/T102/T103/T305/T308/T303/T401 | L1–L4 | 3/3 | PASS | — |
| T306 | L3 | 2/3 | PASS（≥0.5 多数制） | 1 次 run 波动（c1/c2） |
| **T207** | L3 | 0/3 | **FAIL** | c3：`ERROR_TOP3 mismatch report=['auth','api','db'] actual=['api','auth','db']`——**Top3 排序规则歧义**（verify 按出现顺序，agent 按字母序），3 次全同，属判定口径问题非 agent 能力 |
| **T402** | L4 | 0/3 | **FAIL** | 3 次均"达到最大步数 20"；run1 改坏脚本语法，run2/3 为 `rank=0 (must start from 1)`——**排名起点口径未在 spec 明确** + 20 步上限对 L4 偏紧 |

**结论**：core 卡口真实卡住了（0.8 < 0.9）——正式启用前需校准两处判定口径：
1. T207：统一 ERROR 模块 Top3 的排序规则（出现次数 desc → 并列时按模块名或出现顺序，spec 与 verify 对齐）；
2. T402：spec 明确"排名从 1 开始"，并评估提升 max_steps（20 → 30+）以适配 L4 修复类任务。

### 6.2 判定口径修复与重跑（2026-09-06）

修复内容（4 处改动，pytest 全量 80 passed 无回归）：
1. `src/agent_eval/spec.py`：TaskSpec 新增可选 `max_steps` 字段（spec 级覆盖后端默认 20）；
2. `src/agent_eval/runner.py`：构造 backend 时透传 `task.max_steps`；
3. `tasks/T207/spec.yaml`：排序规则强化（并列按模块名字母升序）+ 显式示例 `ERROR_TOP3=api,auth,db`；
4. `tasks/T402/spec.yaml`：rank 从 1 开始示例行 + `max_steps: 30`。

修复后 core 全量重跑（10 任务 × 3 runs）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 门禁判定 | FAIL（0.8 < 0.9） | **PASS（1.0 ≥ 0.9）** |
| T207 | 0/3（排序歧义） | **3/3** |
| T402 | 0/3（rank 口径 + 步数） | **2/3**（多数制 PASS） |
| 总耗时 | 707.7s | **271s（≈ 4.5 分钟）** |
| 成本 | ¥0.68 | **¥0.63** |
| tokens | 304,675+23,146 | 284,791+19,806 |

注：T303、T402 本轮 2/3（各 1 次 run 波动），runs=3 多数制（≥2 过即任务 PASS）恰好吸收了单次波动——这正是多采样的意义。

### 6.3 被测后端切换 deepseek-harness（2026-09-06，正式卡口跑绿）

按既定路线（minimal-react 为弱基线，换官方 agent harness 后重跑 core），完成 DeepSeek 官方 **deepseek-harness**（`dsh` CLI，MIT）接入：

1. **后端实现** `src/agent_eval/backends/deepseek_harness.py`：黑盒 subprocess 调 `dsh --profile headless "<task>"`；
2. **关键修复**：默认使用评测专用隔离 `DSH_HOME`（`~/.cache/agent-eval/dsh-home`，首次运行自动初始化 headless profile）——`~/.dsh` 携带用户个人凭据，实测导致 dsh AUTH 403 预扣失败且不可控；隔离后凭据只来自环境变量 `DEEPSEEK_API_KEY`，干净跑通；
3. **兼容修复**：`__init__` 接收 `max_steps` 保留参数（runner 透传，黑盒无步数概念）；
4. **CI workflow**：演示仓库 `agent-eval.yml` 增加 `setup-node@v4`（Node **22**，dsh 依赖 `Promise.withResolvers`，Node 20 启动即崩）+ 局部安装 `@deepseek-ai/dsh` 加入 PATH + `agent-eval ci --gate core --agent deepseek-harness`。

实测数据（core 10 任务 × 3 runs）：

| 项 | minimal-react（CI Run #16） | deepseek-harness（本地） | deepseek-harness（CI Run #18） |
|---|---|---|---|
| gate 通过率 | 0.8 < 0.9 **FAIL** | **1.0 ≥ 0.9 PASS** | **1.0 ≥ 0.9 PASS** |
| T303（L4 验证） | 1/3 | 3/3 | 3/3 |
| T402（L4 修复） | 1/3 | 3/3 | 3/3 |
| 其余 8 任务 | 3/3 | 3/3 | 3/3 |
| 总耗时 | 452.8s | 418.0s | 510.6s |
| 合并动作 | 阻断（PR #3 关闭留证） | — | **放行（PR #4 已合并 main）** |

结论：T303/T402 对 minimal-react 是真实能力瓶颈（非环境问题，verify 纯标准库无环境依赖）；切换官方 harness 后 core 卡口 10/10 稳定全绿。PR #4 合并后演示仓库 main 即 core + deepseek-harness 正式门禁。

## 7. 使用方法（接入真实项目）

1. **仓库 Secrets**：Settings → Secrets and variables → Actions → 添加 `DEEPSEEK_API_KEY`（`agent-eval ci` 通过环境变量读取）。
2. **拷贝 workflow**：把演示仓库 `.github/workflows/agent-eval.yml` 复制到目标仓库，按需改 `--gate core`、runner、超时。
3. **配置分支保护**：Settings → Branches → main → Require status checks → 选择 `agent-eval demo gate` → **勾选 Include administrators**。
4. 提交 PR：门禁自动运行并发布 JUnit；不达标时合并按钮被阻断，修复后自动放行。

## 8. 后续扩展（按用户既定路线）

- 被侧后端：`minimal-react`（白盒基线）与 **`deepseek-harness`（官方 agent harness，已接入并跑绿 core）**，注册表支持继续扩展；
- checkpoint 级 testcase：已实现（JUnit 每个 checkpoint 一个 testcase）；
- 多 agent 横向对比：`agent-eval ci` 支持指定 agent，可扩展 gate 定义"每 agent × 每任务"通过率矩阵；
- 成本核算：工作台已有"预计成本/实际成本"列，CI 侧可将 `ci-report.json` 成本字段接入成本看板；
- Allure 报告：results 随 artifact 上传，可在 PR 中联动 Allure 托管（当前因 runner Docker 限制未用 container action）。

## 9. 关键链接

| 内容 | 链接 |
|---|---|
| 演示仓库 | https://github.com/yaoxianda-github/agent-eval-demo |
| 演示 workflow | https://github.com/yaoxianda-github/agent-eval-demo/blob/main/.github/workflows/agent-eval.yml |
| 门禁配置 | https://github.com/yaoxianda-github/agent-eval-demo/blob/main/ci/gate.yaml |
| 演示 README（验证记录） | https://github.com/yaoxianda-github/agent-eval-demo/blob/main/README.md |
| 主仓库 | https://github.com/yaoxianda-github/ai-agent-eval |
| M1 提交 | `f8116a5`（ci 命令）、`8968352`（scripts 目录定位修复） |
