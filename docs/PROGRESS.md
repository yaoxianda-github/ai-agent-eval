# 项目进度存档

> 通用 AI Agent 评测框架（可分享、可复用）· 一周 MVP 冲刺
> 仓库：https://github.com/yaoxianda-github/ai-agent-eval
> 最后更新：2026-09-03（Day 4/5 完成，双后端对比数据齐）

## 冲刺进度

| 天 | 内容 | 状态 |
|---|---|---|
| Day 1 | 项目骨架 + 任务包契约（task-spec@v1）+ 5 个任务 + fixtures + 判定脚本 | ✅ 已推送 |
| Day 2 | Runner 编排 + 自建 ReAct 后端（minimal-react v0.2.0） | ✅ 已验证 |
| Day 3 | 判定器（5 种校验点）+ 评分器；Agent 健壮性加固；判定器路径 bug 修复 | ✅ 已验证 |
| Day 4 | 接入 Aider（第三方 CLI）双后端对比 | ✅ T001 通过；T102 收窄为适配度限制 |
| Day 5 | 报告生成器（reporter.py + agent-eval report） | ✅ 已生成对比报告 |
| Day 6 | 稳定化 + README 打磨 | ⏳ |
| Day 7 | 演示 + 缓冲 | ⏳ |

## 已验证结果

- `agent-eval list-tasks` ✅ 5 个任务（T001/T102/T205/T401/T502）
- **minimal-react（自研基线）**：
  - T001 多次运行 ✅ 4/4（1.0）；T102 非确定性 ⚠️ 有 3/3 也有 0/3（LLM 非确定性，报告取最好成绩）
- **aider（第三方 CLI）**：
  - T001 ✅ 4/4（1.0，54.6s）
  - T102 ⚠️ 黑盒循环 → 超时（记 0 分），收窄为适配度限制
- `agent-eval report` ✅ 生成自包含 HTML 对比报告（后端×任务得分矩阵 + 汇总卡 + 通过率条形 + 口径/适配度说明）

## 关键结论（过程资产 & 面试素材）

1. **LLM Agent 非确定性**：同 Agent 同任务多次运行结果不同。→ 加固解析 + temperature=0 + 报告取最好成绩、保留多 run 证据。
2. **判定看产物、不看自报**：一次 max_steps（未调 finish）的运行照样 4/4。
3. **判定器自身也会错**："谁来验证验证器"——cmd 路径解析 bug 已修，失败详情保留退出码+stderr。
4. **Agent × 任务类型适配度（Day 4 新发现）**：aider（结对编程 Agent）在"改已有代码"类任务（T001）通过，但在"从零生成数据产物"类任务（T102）上黑盒循环超时。→ 评测框架能暴露"没有银弹"——不同 Agent 适配不同任务类型。
5. **后端适配层**：不同 Agent 接口/机制不同（aider 需 --file 显式给文件、需 git baseline、黑盒），框架用 Backend 注册表 + 适配层隔离差异。

## 过程中修复的技术点

- aider 0.70 无 `--no-color`（用 `--no-pretty`）；subprocess 需 `stdin=DEVNULL` 防等待输入；
- aider 超时用 Popen.communicate 保留部分输出做诊断（发现死循环的关键）；
- aider 默认 timeout_s 300（T001 需 ~55s，60s 会偶发超时）。

## Git 状态（待办）

- 已推送：Day 1-3（b19dd1b）
- **未提交（工作树）**：Day 4/5 全部：backends/aider.py、reporter.py、cli.py report 命令、pyproject aider 依赖、docs/PROGRESS.md
- 下次提交：
  ```powershell
  cd "D:\xdyao\DoubaoWork\projects\ai-agent-eval"
  git add -A
  git commit -m "feat: Day 4-5 双后端对比（aider 后端 + 报告生成器）"
  git push
  ```

## 下一步

1. 跑 `agent-eval report` + `start reports\report.html` 看最终对比报告
2. 提交推送
3. Day 6：稳定化（README 打磨、可选加 --timeout 参数）+ 准备 Day 7 演示
