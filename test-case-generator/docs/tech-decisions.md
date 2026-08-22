# 关键技术决策

| 决策 | 原因 |
|------|------|
| `use_responses_api=False` | DeepSeek/中转站走 OpenAI Chat Completions，不支持 Responses API |
| `FixToolCallArgsMiddleware` 默认不注册 | 官方 API 无 `{}` 前缀畸形；仅 `RELAY_FIX_TOOLCALL=true` 时启用 |
| `WorkflowTriggerFallbackMiddleware` 始终注册 | 主 Agent 未触发工作流时注入提醒强制再调 task |
| base_url 追加 `/v1` | DeepSeek/中转站 API 路径与 OpenAI SDK 默认不匹配 |
| 模型名 `deepseek-v4-flash` 小写 | 官方模型名小写正常；切回 GLM 中转站须改大写 `GLM-5.1` |
| `FilesystemBackend(virtual_mode=False)` | 允许访问外部路径文件，支持前端上传需求文档 |
| LangGraph StateGraph 替代 5 个 SubAgent | 解决 `task()` 返回空字符串问题，确定性流程编排 |
| `CompiledSubAgent` 注册工作流 | 将 LangGraph 图作为子 Agent 集成到 Deep Agents 框架 |
| 工作流节点直接 `llm.invoke()` | 不经过框架的 tool_call 机制，避免中转站兼容问题 |
| 多文件拆分（agent/display/workflow/） | 对齐 examples/deep_research 范式，单体 2077 行 → 入口 + package |
| prompt 用 f-string builder 函数 | .md/.memory JSON 含大量 `{}`，`.format()` 会报 KeyError |
| `extract` 节点正则截取 | 零 API 调用，关键词匹配标题→AI兜底→自动边界 |
| `node_parse` 读 `extracted_text` | 移除硬截断，由 extract 节点统一处理大文档 |
| `WorkflowState` 使用 `TypedDict` | LangGraph 要求状态类型为 TypedDict |
| `status_messages` 使用追加式 reducer | 每个节点的进度消息累加，不覆盖 |
| `debug` 参数化 | `create_test_case_agent(debug=args.debug)`，`--debug` CLI 控制 |

## 阶段确认门决策

| 决策 | 原因 |
|------|------|
| 主 Agent 默认带 MemorySaver checkpointer | 子图 interrupt 暂停需要 checkpoint 才能断点恢复 |
| 子图 `compile()` 不传 checkpointer | per-invocation 语义，运行时继承父 checkpointer |
| `enable_confirm_gate` 已移除 | 简化：主 Agent 始终带 checkpointer，子图节点未调用 `interrupt()` 时行为与无 checkpointer 时一致，零退化 |
| 每轮新 `thread_id` (uuid) | 跨轮隔离，避免 WorkflowTriggerFallbackMiddleware 全历史检查误判 |
| `user_decision`/`last_gate_stage` 子图局部 | 上下文隔离，父 Agent 不直接暴露子图私有状态 |
| 末节点显式透出审批结果 | 父 Agent 感知审批结果唯一通道：AIMessage 摘要 → ToolMessage |

## 依赖

- `deepagents` >= 0.7.3
- `langchain` / `langchain-core` / `langgraph`
- `langchain-openai`（ChatOpenAI）
- `pydantic`
- `rich`（终端显示）
- `python-dotenv`
- `openpyxl` / `xmind-sdk`（XLSX/XMind 生成）