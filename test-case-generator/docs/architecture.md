# 项目架构

## 整体架构

```
主 Agent (create_deep_agent)
  │
  ├── 用户对话（deepseek-v4-flash + WorkflowTriggerFallbackMiddleware 后备）
  │   （RELAY_FIX_TOOLCALL=true 时再加 FixToolCallArgsMiddleware）
  │
  └── task("generate-workflow") → CompiledSubAgent
        │
        └── LangGraph StateGraph (确定性编排)
              │
              ├── init_memory     [Python] 初始化 .memory/
              ├── preprocess      [Python] 扫描/转换/读取需求文档
              ├── extract         [Python] 大文档章节截取（关键词/AI兜底/自动边界）
              ├── parse           [LLM]    解析需求文本 → 结构化 JSON
              ├── design          [LLM]    设计测试用例 → 用例 JSON
              ├── review          [LLM]    质量自检 → passed/failed
              │     │
              │     ├── passed → generate
              │     └── failed + retry < 2 → increment_retry → design (重试)
              │     └── failed + retry ≥ 2 → generate (强制通过)
              │
              └── generate        [Python] JSON→XMind/XLSX + 更新记忆
                    │
                    └── 返回 AIMessage(摘要) → 主 Agent 展示给用户
```

### 子 Agent 注册

`create_deep_agent` 通过 `subagents` 参数注册子 Agent：

| 子 Agent | name | 用途 |
|---------|------|------|
| 工作流子 Agent | `generate-workflow` | 测试用例生成主流程 |
| 演示子 Agent | `demo-gate` | 阶段确认门演示 |

所有子 Agent 经主 Agent 的 `task` 工具触发，非直接工具调用。

### CompiledSubAgent 结果返回机制

1. 子图末节点在 `messages` 中追加一条 `AIMessage(content=摘要)`
2. 框架 `_return_command_with_state_update()` 从 `result["messages"]` 反向查找最后一条有文本的 AIMessage
3. 该文本作为 `ToolMessage` 内容返回给主 Agent
4. 主 Agent 的 LLM 看到 ToolMessage，将摘要展示给用户

## 工作流状态 (WorkflowState)

| 字段 | 类型 | 说明 |
|------|------|------|
| `messages` | `list[AnyMessage]` | 必需：CompiledSubAgent 结果载体 |
| `requirement_dir` | `str` | 需求文档目录 |
| `requirement_text` | `str` | 预处理后的需求文本 |
| `section_range` | `str` | 用户指定章节范围 |
| `extracted_text` | `str` | 截取后的需求文本 |
| `chapter_outline` | `str` | 章节目录 JSON |
| `parsed_result` | `str` | 解析结果 JSON |
| `test_cases` | `str` | 设计的用例 JSON |
| `review_result` | `str` | 审查结果 JSON |
| `output_files` | `str` | 生成文件路径 JSON |
| `retry_count` | `int` | 审查重试次数 |
| `status_messages` | `list[str]` | 进度消息列表（追加式） |
| `error` | `str` | 错误信息 |

### 节点实现方式

| 节点 | 实现 | 说明 |
|------|------|------|
| `init_memory` | Python | 调用 `scripts/memory_manager.py` |
| `preprocess` | Python | 文件系统操作 + `scripts/requirements_preprocessor.py` |
| `extract` | Python | 正则截取（关键词→AI兜底→自动边界） |
| `parse` | LLM | 需求文本 + 解析规范 → 结构化 JSON |
| `design` | LLM | 解析结果 + 设计规范 → 测试用例 JSON |
| `review` | LLM | 测试用例 + 审查清单 → passed/failed |
| `generate` | Python | 调用 `scripts/generate_xmind.py` / `generate_xlsx.py` |

## 阶段确认门

在 `workflow/gates.py` 提供 `make_confirm_gate()` 工厂函数，可在任意子图节点插入。

**机制**: 节点执行 → `interrupt()` 暂停 → 展示阶段产出 → 用户确认/取消 → `Command(resume)` 从断点恢复或终止。

**关键约束**:
- 子图 `compile()` 不传 checkpointer，运行时继承主 Agent 的 MemorySaver
- 展示数据只在 gate 节点内部提取并放入 interrupt 载荷
- `user_decision`/`last_gate_stage` 是子图局部状态，不自动上浮到主 Agent
- 父 Agent 需感知审批结果 → 子图末节点显式写入 AIMessage 摘要

参考实现: `workflow/subagents/demo_gate.py`