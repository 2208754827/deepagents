# Tech Stack

<!-- MACHINE_BLOCK_START -->
## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.12.13（conda env: `agent`，`E:/anaconda/envs/agent/`） |
| 框架 | LangChain 1.3+, LangGraph 1.2+, Deep Agents ≥0.7.3 |
| LLM | DeepSeek API（默认 `deepseek-v4-flash`，可切换 GLM 中转站） |
| 结构化输出 | Pydantic ≥2.0.0 |
| 终端美化 | Rich ≥13.0.0 |
| XLSX 生成 | openpyxl ≥3.1.0（`scripts/generate_xlsx.py`） |
| PDF 解析 | 可选：pypdf / pdfplumber（当前注释未启用） |
| 环境变量 | python-dotenv ≥1.0.0 |

## 常用命令

```bash
# 进入项目
cd test-case-generator
conda activate agent

# 运行
python agent.py                  # 交互式，输入指令，quit 退出
python agent.py --log            # + 文件日志（写入 logs/）
python agent.py --log --debug    # + Agent debug 模式

# 测试
python -m pytest tests/

# 测试用例导出
python scripts/generate_xlsx.py
```

## 环境要求

- Python 3.12.13（conda env `agent`）
- `.env` 中配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`
- `RELAY_FIX_TOOLCALL`：切回中转站时设 `true` 启用 `FixToolCallArgsMiddleware`

## .env 配置项

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | API Key |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com`，代码自动追加 `/v1` |
| `DEEPSEEK_MODEL` | 默认 `deepseek-v4-flash` |
| `RELAY_FIX_TOOLCALL` | 默认 `false`；切回中转站设 `true` |
| `DEEPAGENTS_LOG_LEVEL` | 默认 INFO |

<!-- MACHINE_BLOCK_END -->

<!-- USER_BLOCK_START -->
## 补充说明
{用户自由编辑区}
<!-- USER_BLOCK_END -->