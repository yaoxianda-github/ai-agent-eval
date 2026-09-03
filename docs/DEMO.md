# MVP 演示脚本（Day 7）

> 用途：面试 / 分享 / 社区展示。目标 5 分钟讲清"这是什么、怎么做到的、我踩过的坑"。
> 原则：**现场真实跑命令 + 下钻证据**，不靠 PPT 堆概念。

## 0. 电梯陈述（30 秒，背熟）

> "我搭建了一个**通用的 AI Agent 评测框架**：同一套任务包，可以评测任何 Agent 后端（自研的、第三方的），产出**可复现、可追溯、可对比**的评测报告。
> 核心设计三点：**任务即契约**（任务作者只写 spec，不碰框架）、**后端即插件**（注册表扩展）、**判定看产物不看自报**（Agent 说没完成不重要，校验点说了算）。"

## 1. 仓库与结构（30 秒）

打开 <https://github.com/yaoxianda-github/ai-agent-eval>，指三个目录：

```
tasks/            # 任务包：manifest + <id>/spec.yaml + fixtures（任务即契约）
src/agent_eval/
  ├─ backends/    # 后端注册表：base / minimal_react / aider（后端即插件）
  └─ runner / verifiers / scoring / reporter   # 流水线：执行→判定→评分→报告
```

话术："任务作者和框架作者解耦——**想评测一个任务，只写 YAML**；想评测一个 Agent，只加一个 Backend 类。"

## 2. 任务包（30 秒）

```bash
agent-eval list-tasks
```
展示 5 个任务 L1→L5（日期格式化 → 数据汇总 → EXIF 归档 → 缺陷修复 → 周报总结）。
打开 `tasks/T102/spec.yaml` 讲契约：`description + fixtures + ground_truth.checkpoints`，判定点声明式描述（"汇总结果与原始数据重算一致"）。

## 3. 白盒基线：跑一个任务（1 分钟）

```bash
agent-eval run --task T001 --agent minimal-react
```
- 展示 CLI 输出：verdict 4/4、score 1.0；
- 打开 `results/runs/<id>/run.json` 下钻**步骤级轨迹**：list_dir → read → write → finish，每步 tool/args/observation/ts。
- 话术："这是我自己写的 200 行 ReAct Agent，当作**白盒基线**——每步都在我掌握里，用它验证评测链路本身是对的。"

## 4. 双后端对比 + 报告（1.5 分钟，核心）

```bash
agent-eval run --task T001 --agent aider
agent-eval report && start reports\report.html
```
- 展示得分矩阵：**T001 两后端都 1.0；T102 minimal-react 1.2、aider 0（超时）**；
- 指报告底部"适配度观察"，讲核心发现：

> "aider 是结对编程 Agent，擅长**改已有代码**（T001 通过）；但在**从零生成数据产物**的任务（T102）上，黑盒调用陷入'要不要新建文件'的循环而超时。**评测框架的价值就是暴露：没有银弹，Agent × 任务类型有适配度差异。**"

- 顺手讲黑盒/白盒差异："minimal-react 是步骤级轨迹，aider 是黑盒单次调用——报告里明确标注了轨迹口径。"

## 5. 判定与证据链（1 分钟）

- 话术："**判定看产物、不看 Agent 自报**。有一次 Agent 没调 finish（max_steps），但产物全对，照样 4/4。"
- 再加"**谁来验证验证器**"：我自己的判定器出过 bug——相对路径解析错位导致 Agent 做对却判 FAIL。靠 run.json 证据链定位修复。这正是框架比'跑个分'强的地方：**分数可下钻到原始证据**。

## 6. 收尾（30 秒）

- 已交付：`agent-eval` 三条命令、5 任务 × 2 后端、自包含 HTML 报告、GitHub 开源（MIT）；
- 路线图：Langfuse 可观测、黑盒采集桌面端（豆包工作/WorkBuddy）、LLM-as-a-Judge、Docker 沙箱。

## 面试追问预案

| 问题 | 回答要点 |
|---|---|
| 为什么不用 Langfuse？ | 我了解过。Langfuse 是**追踪/可观测**平台，不是评测框架；MVP 先做任务契约+判定，预留了轨迹结构化输出，后续可接 Langfuse 做 trace 分析 |
| 判定怎么保证公平？ | 确定性校验点 + 产物判定 + 多 run 取最好 + 判定器自身可单测；不给 Agent 自报任何权重 |
| Agent 非确定性？ | 多 run 保留全部证据，报告取最好成绩；自研基线 temperature=0 + 解析重试降低方差 |
| 新增 Agent 要多麻烦？ | Backend 子类 + 注册表注册即可。aider 是最难的案例：黑盒 + 需 git baseline + 需 `--file` 显式传文件 + 需 `stdin=DEVNULL`——都封装在适配层 |
| 新增任务要多麻烦？ | 写 spec.yaml + fixtures + 判定脚本，不碰框架代码 |
| 局限？ | MVP 只有 5 任务 2 后端；未做 Langfuse、桌面端黑盒采集、LLM-Judge、Docker 沙箱隔离 |

## 真实故事素材（讲出来加分，全是有证据的）

1. **非确定性**：T001 同 Agent 三次运行三种结果——10 步成功 / 中途坏 JSON 输出被框架记录 / 20 步没调 finish 但产物正确仍 4/4。
2. **验证验证器**：判定器 cmd 路径 bug，Agent 做对却被误判 FAIL，靠证据链定位修复。
3. **适配度**：aider 改代码类任务通过、从零生成数据任务循环超时——不是 bug，是 Agent 能力画像。
