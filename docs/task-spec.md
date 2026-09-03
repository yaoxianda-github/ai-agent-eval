# Task Spec 规范（task-spec@v1）

Task Spec 是任务包与评测框架之间的**契约**：
- 任务作者只需编写 `tasks/<id>/spec.yaml` + `fixtures/`，不碰框架代码；
- 框架据此加载任务、执行后端 Agent、运行判定器。

## 目录结构

```
tasks/
├── manifest.yaml        # 任务包清单
├── T001/
│   ├── spec.yaml        # 任务卡
│   └── fixtures/        # 初始状态文件（运行时会复制进干净工作目录）
```

## manifest.yaml

```yaml
name: agent-eval-basic      # 任务包名
version: 0.1.0              # 任务包版本（随任务变更升级）
schema: task-spec@v1        # 引用的 Task Spec 版本
defaults:                   # 全包默认值（可被单任务覆盖）
  timeout_s: 300
  cost_budget_usd: 0.5
tasks: [T001, T102, T205, T401, T502]
```

## spec.yaml 字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `id` | ✅ | str | 任务唯一 ID，如 `T001` |
| `title` | ✅ | str | 任务标题 |
| `level` | ✅ | L1–L5 | 难度分层：L1 单步 / L2 多步 / L3 跨工具 / L4 故障注入 / L5 开放式 |
| `description` | ✅ | str | 任务描述（给 Agent 的指令主体） |
| `tags` | 否 | list[str] | 标签，便于检索 |
| `fixtures.source` | ✅ | str | 初始文件目录（相对 `tasks/<id>/`） |
| `ground_truth.checkpoints` | ✅ | list | 校验点（见下） |
| `verifier` | 否 | `deterministic` / `llm_judge` | 判定方式，默认 `deterministic` |
| `rubric` | 否 | str | 仅 `verifier: llm_judge` 使用：评分标准（缺省用框架内置默认标准） |
| `weight` | 否 | float | 任务权重，默认 1.0 |
| `cost_budget_usd` | 否 | float | 预估成本上限 |
| `timeout_s` | 否 | int | 超时秒数 |

## 校验点（Checkpoint）类型

| type | 判定逻辑 | 关键字段 |
|---|---|---|
| `file_exists` | 文件/目录存在即通过 | `path`（支持 glob） |
| `file_not_exists` | 不存在即通过 | `path` |
| `content_contains` | 内容匹配正则即通过 | `path` + `pattern` |
| `content_not_contains` | 内容不含该正则即通过 | `path` + `pattern` |
| `cmd_exit_zero` | 命令退出码为 0 即通过 | `cmd`（`cwd=工作目录`；`@scripts/` 前缀引用仓库 `scripts/` 目录） |

> 约定：`path` 均相对工作目录；`cmd` 中的 `@scripts/xxx.py` 会被替换为仓库 `scripts/` 的绝对路径，
> 避免把判定脚本暴露给 Agent 篡改。

## 判定与评分

- 任务通过 ⇔ 所有校验点通过（MVP 采用全过制，后续可加 `required`/`optional` 区分）；
- 维度分 / 综合分由 scorer 基于任务 `weight` 聚合（MVP 先做"成功率 + 效率"两个维度）。

## LLM-as-a-Judge（V2.2，`verifier: llm_judge`）

开放任务（如 T502 周报总结）无法用纯确定性校验点衡量"质量好坏"。设置 `verifier: llm_judge` 后，
框架在确定性校验点之外追加一次 LLM 语义判分：

- 输入：任务 `description` + `rubric`（评分标准）+ Agent 产物（`output/` 目录全部文本文件）；
- 输出：verdict `{id:"judge", type:"llm_judge", passed, score(0-1), detail, reasoning}`，与确定性校验点同构，
  计入 pass_rate 与任务得分（权重 × 通过率）；
- `rubric` 缺省使用内置默认标准（完整性 / 准确性 / 结构 / 语言）；
- 无 API Key（`DEEPSEEK_API_KEY` / `LLM_API_KEY`）、判分调用失败、产物缺失 → 该 verdict 记为不通过，
  **不中断评测**（诚实降级）。

示例：

```yaml
verifier: llm_judge
rubric: |
  1. 内容完整性（30 分）：四章节齐全；
  2. 信息准确性（25 分）：关键信息正确归类；
  3. 组织清晰度（25 分）：无重复冗余；
  4. 语言表达（20 分）：通顺专业。
```
