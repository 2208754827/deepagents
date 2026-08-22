# Patterns & Decisions

<!-- MACHINE_BLOCK_START -->
## 技术决策

| 日期 | 决策 | 原因 |
|------|------|------|
| 2026-08-22 | 主 Agent + 2 个 LangGraph 子 Agent（`generate-workflow` + `demo-gate`） | 主 Agent 负责用户对话，子 Agent 隔离上下文窗口、分阶段处理 |
| 2026-08-22 | `FilesystemBackend(virtual_mode=False)` 支持外部路径 | 允许读写项目外的需求文档和输出文件 |
| 2026-08-22 | 主 Agent 带 MemorySaver checkpointer | 支持子图 interrupt 断点恢复 |

## 架构选择

- **主 Agent（agent.py）**：命令行参数解析 + 交互循环 + re-export 测试兼容
- **工作流（workflow/）**：节点/状态/提示词/图/中间件拆分到独立模块
- **子 Agent（workflow/subagents/）**：`generate-workflow`（测试用例生成）、`demo-gate`（确认门）
- **分层文档（docs/）**：架构 `architecture.md`、排障 `troubleshooting.md`、技术决策 `tech-decisions.md`

## 代码约定

- 测试兼容 re-export：`agent.py` 末尾 re-export 关键符号，保证 `tests/test_extract.py` 的 patch 生效
- Windows GBK 编码：`main()` 中修复 stdout/stderr 编码
- 日志：`--log` 写入 `logs/` 目录，支持 `--debug` 开启 Agent debug

<!-- MACHINE_BLOCK_END -->

<!-- USER_BLOCK_START -->
## 补充约定
{用户自由编辑区}
<!-- USER_BLOCK_END -->