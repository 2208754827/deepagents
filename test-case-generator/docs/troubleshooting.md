# 已知问题与解决方案

> 按问题类型组织，遇到对应错误时查阅。

## 中转站兼容

### 1. tool_call arguments 格式错误（仅中转站）

**问题**: 中转站在 `tool_call.function.arguments` 前多输出 `{}`，导致 JSON 解析失败。

**解决**: `FixToolCallArgsMiddleware`（`workflow/middleware.py`），`RELAY_FIX_TOOLCALL=true` 启用。

### 2. base_url `/v1` 后缀重复（404 NOT_FOUND）

**问题**: `.env` 的 `*_BASE_URL` 已带 `/v1`，`_create_llm()` 又追加 `/v1` → 路径不存在。

**解决**: 追加前先 `endswith("/v1")` 判空（`workflow/config.py`）。

**排查**: `python -c "from workflow.config import llm; print(llm.openai_api_base)"` 应为单层 `/v1`。

### 3. 模型名称大小写敏感（GLM 中转站）

**问题**: 小写 `glm-5.1` 返回 404。

**解决**: 改大写 `GLM-5.1`。

### 4. API 速率限制

**问题**: 中转站触发 `429 DAILY_LIMIT_EXCEEDED`。

**应对**: 等待配额重置或切官方 API。

## LLM 输出问题

### 5. max_tokens 截断 → JSON 不完整

**问题**: `max_tokens` 不够，JSON 被硬截断 → `_extract_json` 三级兜底全失败。

**解决**: `_create_llm()` 工作流节点 `max_tokens` = 16384（`workflow/config.py`）。

### 6. generate 节点 cases_list 未定义崩溃

**问题**: `node_generate` 解析失败时 `except` 分支未给 `cases_list` 赋值 → `UnboundLocalError`。

**解决**: `except` 分支补 `cases_list = test_cases`（`workflow/nodes.py`）。

### 7. design 节点 LLM 超时

**问题**: 大 prompt + 大输出在中转站慢，`timeout=90` 不够。

**解决**（`workflow/config.py`）:
- `timeout` 90 → 300
- `max_retries` 2 → 1
- `_safe_llm_invoke` 超时类错误不重试

## 主 Agent 行为

### 8. task 后多余工具调用 → 429

**问题**: 工作流返回后主 Agent 又去 `ls`/`glob`/`read_file` 反复查。

**解决**: `ORCHESTRATOR_PROMPT` 强化禁止（`workflow/prompts.py`）。

## 基础设施

### 9. 文件日志系统

`--log` 参数控制，日志写入 `logs/` 目录。

### 10. 终端输出优化

`AgentDisplay` 仅显示 AI 消息 + spinner，工具详情写入日志。

### 11. 外部文件访问

`FilesystemBackend(virtual_mode=False)`，支持外部路径。

### 12. 旧架构 task() 返回空字符串

**已解决**: 改用 LangGraph StateGraph，节点直接 `llm.invoke()`。

### 13. Windows GBK 编码错误

**已解决**: `main()` 中重新配置 stdout/stderr 为 UTF-8（`agent.py`）。