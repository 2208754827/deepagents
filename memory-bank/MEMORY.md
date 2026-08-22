# Project Memory

<!-- MACHINE_BLOCK_START -->
<!-- MEMORY_BANK_TEMPLATE:v7.1 -->

## Project Snapshot
- **结论**: 基于 LangChain Deep Agents 框架的测试用例生成 Agent，从需求文档（Markdown/Word/PDF）自动生成测试用例（XMind/XLSX）
- **边界**: 本文件只保留摘要、快照、路由、当前焦点；详细内容在 `memory-bank/details/`
- **指针**: 核心实现 `test-case-generator/`；技术栈 `memory-bank/details/tech.md`
- **仓库**: `D:\deepagent\deepagents`（git 主分支：main）

## Current Focus
> 更新于: 2026-08-22

- **当前焦点**: gates + 子 Agent 机制（代码已写，未提交）
- **下一步**:
  - [ ] 提交未跟踪改动：`workflow/gates.py`、`workflow/subagents/demo_gate.py`、`tests/test_gates.py`、`docs/`，及 modified `agent.py`/`display.py`/`workflow/graph.py`
- **阻塞项**: 无

## Decision Highlights (Still Binding)

> 只保留"仍影响当前实现"的决策。完整历史见 `memory-bank/details/patterns.md`。

| 决策 | 日期 | 对实现的直接约束 |
|------|------|-----------------|

## Routing Rules（意图驱动）

按"你想做什么"选择 1-3 个最相关的文件读取。

### 通用意图

| 意图 | 目标文件 |
|------|----------|
| 了解技术栈/命令/环境/端口 | `memory-bank/details/tech.md` |
| 查看技术决策/架构选择/编码约定 | `memory-bank/details/patterns.md` |
| 查看进度/任务清单/阻塞项 | `memory-bank/details/progress.md` |
| 查找踩坑经验/解决方案 | `memory-bank/details/learnings/` |
| 查找需求文档 | `memory-bank/details/requirements/` |
| 查找设计文档 | `memory-bank/details/design/` |
| 不确定文件名 | 先 `glob("memory-bank/details/**/*.md")` 再读 |

## Drill-Down Protocol

1. **先用 MEMORY.md 给出可执行结论**；需要证据/细节时再按路由 drill-down
2. **默认 direct read 1-3 个 details/ 文件**（读够就停）
3. **升级条件**：需要证据链/冲突检测/跨文件汇总 → 调用 `memory-reader`
4. **反幻觉**：未读到/未写明的信息 = 未知，不要补全
5. **回答时必须给引用指针**（至少 1-2 个文件路径）

## Write Safety Rules

- 主 agent 可直接写 `memory-bank/`，仅限 `.md` 文件（Plugin 强制）
- 写入前必须 Proposal → 用户确认
- 禁止写入任何敏感信息（API key、token、密码、私钥）
- learnings/ 仅存放已解决的踩坑，不存进行中的任务

## Capacity Limits（硬约束）

- **Decision Highlights**: ≤ 20 条，超出归档到 `details/archive/`
- **Top Quick Answers**: ≤ 8 条，过期就删
- **Current Focus**: 完成的清掉，只留当前焦点
- **learnings/**: 只存已解决，不存进行中；命名 `YYYYMMDD_简短问题标题.md`
- **MEMORY.md 本身**: 只放摘要/快照/路由/焦点，不写长篇细节

## Workflow Conventions

### Plan 存档 + 执行流程

当用户说"存进记忆然后执行" / "存了直接干" / "归档并执行" 等组合指令时，按以下顺序：

1. **存记忆**：读取当前 plan 内容，按类型归档：
   - 架构/设计决策 → `memory-bank/details/design/*.md`
   - 需求规格/验收标准 → `memory-bank/details/requirements/REQ-*.md`
   - 技术决策/约定 → `memory-bank/details/patterns.md`
   - 同步在 MEMORY.md 的 Routing Rules / Decision Highlights 加条目
2. **执行**：按 plan 步骤改代码，每完成一步更新 `memory-bank/details/progress.md`

### Plan 文件位置提醒

Plan 模式生成的指导 md 存在 `C:\Users\jie哥\.claude\plans\`（会话级临时文件，不在仓库）。若需长期保留，必须手动迁移到 memory-bank。

## Top Quick Answers

> 最多 8 条；必须可验证；每条给文件指针。过期就删。

1. Q: 怎么运行测试用例生成 Agent？
   A: `cd test-case-generator && python agent.py`（交互式），`--log` 启用文件日志 → 详见 `memory-bank/details/tech.md`

<!-- MACHINE_BLOCK_END -->

<!-- USER_BLOCK_START -->
## 用户笔记
{用户自由编辑区}
<!-- USER_BLOCK_END -->