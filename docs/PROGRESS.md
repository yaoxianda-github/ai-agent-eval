# 项目进度存档

> 通用 AI Agent 评测框架（可分享、可复用）· 一周 MVP 冲刺
> 仓库：https://github.com/yaoxianda-github/ai-agent-eval
> 最后更新：2026-09-03（**MVP 收官，7/7 完成**）

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

- b19dd1b Day 1-3 · 4361d35 Day 4-5 · d0626b4 Day 6 · 87d7786 Day 7（已全部推送）

## 下一步（V2 方向）

- 复盘整个 MVP（哪些值、哪些留坑）→ 建议落成 docs/RETROSPECTIVE.md
- 路线图：Langfuse 可观测、黑盒采集桌面端（豆包工作/WorkBuddy）、LLM-as-a-Judge、Docker 沙箱隔离、更多后端横向基准
