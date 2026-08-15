"""项目配置：路径、日志、模型实例、参考文档加载、安全 LLM 调用。

集中管理所有共享的单例资源，供 workflow 其余模块与 display/agent 复用。
依赖：workflow.state（仅 MAX_PROMPT_CHARS）。
"""

import logging
import os
import warnings
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from rich.console import Console

from workflow.state import MAX_PROMPT_CHARS

warnings.filterwarnings("ignore", message="Core Pydantic V1 functionality")

# ============================================================
# 项目路径
# ============================================================
BASE_DIR = Path(__file__).parent.parent.resolve()
console = Console()

# ============================================================
# 日志配置
# ============================================================
log_level = os.environ.get("DEEPAGENTS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test-case-generator")
tool_logger = logging.getLogger("test-case-generator.tools")

logging.getLogger("deepagents").setLevel(
    getattr(logging, os.environ.get("DEEPAGENTS_FRAMEWORK_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
)
logging.getLogger("langgraph").setLevel(
    getattr(logging, os.environ.get("LANGGRAPH_LOG_LEVEL", "WARNING").upper(), logging.WARNING)
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def setup_file_logging():
    """启用文件日志：创建 logs/ 目录，添加 FileHandler 和 tool_logger"""
    logs_dir = BASE_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    log_file = logs_dir / f"agent_{timestamp}.log"

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    logger.setLevel(logging.DEBUG)

    # tool_logger: 专门记录工具调用详情，不传播到终端
    tool_logger.addHandler(fh)
    tool_logger.setLevel(logging.DEBUG)
    tool_logger.propagate = False

    return log_file


# ============================================================
# 加载环境变量
# ============================================================
load_dotenv(BASE_DIR / ".env")

# ============================================================
# 主 Agent 模型（官方 DeepSeek，OpenAI 兼容接口）
# ============================================================
# 主 agent（编排器）和工作流节点都用官方 deepseek-v4-flash。
# 用 ChatOpenAI 直接实例化，resolve_model 对 BaseChatModel 实例直接返回，
# 彻底绕过 provider 推断（避免模型名带 "deepseek" 触发 langchain-deepseek 包缺失）。
DEEPSEEK_MAIN_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
if not DEEPSEEK_MAIN_URL.endswith("/v1"):
    DEEPSEEK_MAIN_URL += "/v1"
DEEPSEEK_MAIN_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_MAIN_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

MAIN_AGENT = ChatOpenAI(
    model=DEEPSEEK_MAIN_MODEL,
    base_url=DEEPSEEK_MAIN_URL,
    api_key=DEEPSEEK_MAIN_KEY,
    temperature=0.3,
    max_tokens=8192,
    max_retries=2,
    timeout=90,
)

# 框架 resolve_model() 对 isinstance(model, BaseChatModel) 直接返回，跳过推断
MODEL = MAIN_AGENT
logger.info("Main Agent LLM: %s via %s (official DeepSeek)", DEEPSEEK_MAIN_MODEL, DEEPSEEK_MAIN_URL)

# ============================================================
# 加载参考文档（嵌入 LLM prompt）
# ============================================================
# 参考文档原文很大（总计 ~54K chars），全部塞进 prompt 会导致 API 断连。
# 按节点分配独立上限，只保留各节点最需要的规则定义部分。
def load_ref(name: str, max_chars: int = 0) -> str:
    """加载参考文档，超限时在段落边界裁剪（优先保留前面的规则定义部分）"""
    p = BASE_DIR / "skills" / "generate-test-cases" / "references" / name
    if not p.exists():
        return ""
    content = p.read_text(encoding="utf-8")
    if max_chars > 0 and len(content) > max_chars:
        cut = content.rfind("\n\n", 0, max_chars)
        if cut < max_chars * 0.8:
            cut = max_chars
        content = content[:cut] + "\n\n[... 文档过长，已截断 ...]"
        logger.info("Reference doc %s truncated: %d -> %d chars", name, len(p.read_text(encoding="utf-8")), cut)
    return content

# parse 节点：PARSING_RULES 是核心，MEMORY_SCHEMA 辅助
PARSING_RULES_MD  = load_ref("PARSING-RULES.md",  8000)
MEMORY_SCHEMA_MD  = load_ref("MEMORY-SCHEMA.md",  4000)

# design 节点：4 个参考文档，TEST_DESIGN + BUSINESS_RULES 优先
TEST_DESIGN_MD    = load_ref("TEST-DESIGN-METHODS.md", 8000)
BUSINESS_RULES_MD = load_ref("BUSINESS-RULES.md", 6000)
TRACEABILITY_MD   = load_ref("TRACEABILITY.md",   3000)
TEST_PRIORITY_MD  = load_ref("TEST-PRIORITY.md",  3000)

# 三节点共享术语对齐表（增强 1）
SHARED_GLOSSARY_MD = load_ref("SHARED-GLOSSARY.md", 2000)


# ============================================================
# 创建 LLM 实例（供工作流节点使用）
# ============================================================
def _create_llm():
    """创建 LLM 实例，供工作流节点直接调用

    支持通过环境变量切换模型提供商：
    - DEEPSEEK_API_KEY 存在时使用 DeepSeek 官方 API
    - 否则 fallback 到 GLM-5.1 中转站
    """
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        logger.info("Using DeepSeek API: model=%s, base_url=%s", model, base_url)
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=deepseek_key,
            temperature=0.3,
            max_tokens=16384,
            max_retries=1,
            timeout=300,
        )
    # Fallback: GLM-5.1 中转站
    model = os.environ.get("GLM_MODEL", "GLM-5.1")
    base_url = os.environ.get("GLM_BASE_URL", "https://api.x5m5x.com")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    logger.info("Using GLM proxy: model=%s, base_url=%s", model, base_url)
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=os.environ.get("GLM_API_KEY"),
        temperature=0.3,
        max_tokens=16384,
        max_retries=1,
        timeout=300,
    )


llm = _create_llm()
logger.info("LLM instance created for workflow nodes")


def _safe_llm_invoke(prompt: str, node_name: str = "") -> str:
    """安全的 LLM 调用：检查 prompt 大小并在超大时截断，节点级重试。

    增强 2：失败或空响应时重试 1 次，每次记录 warning；最终失败返回空串并记录 error。
    """
    if len(prompt) > MAX_PROMPT_CHARS:
        logger.warning(
            "%s node: prompt too large (%d > %d), attempting truncation",
            node_name, len(prompt), MAX_PROMPT_CHARS,
        )
        excess = len(prompt) - MAX_PROMPT_CHARS + 2000  # 留 2000 字符余量
        for marker in ["## 需求文本", "## 解析结果", "## 测试用例"]:
            idx = prompt.rfind(marker)
            if idx >= 0:
                cut_start = idx + len(marker)
                cut_pos = prompt.rfind('\n', 0, len(prompt) - excess)
                if cut_pos > cut_start:
                    prompt = prompt[:cut_pos] + "\n\n[... 文本过长，已截断 ...]"
                    break
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n[... 文本过长，已截断 ...]"
        logger.warning("%s node: prompt truncated to %d chars", node_name, len(prompt))

    # 节点级重试：初始 + 1 次重试；超时类错误不重试（重试只会再等一轮，无意义）
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            response = llm.invoke([HumanMessage(content=prompt)])
            content = response.content.strip() if isinstance(response.content, str) else str(response.content)
            if content:
                return content
            logger.warning("[%s] 第%d次重试: 空响应", node_name, attempt)
        except Exception as e:
            last_err = e
            # 超时类错误（openai.APITimeoutError 等）不重试：重试只会再等一轮，快速失败更可观测
            if "Timeout" in type(e).__name__ or isinstance(e, TimeoutError):
                logger.error("[%s] LLM 超时（不重试）: %s", node_name, e)
                break
            logger.warning("[%s] 第%d次重试: %s: %s", node_name, attempt, type(e).__name__, e)

    if last_err is not None:
        logger.error("[%s] LLM 调用最终失败: %s", node_name, last_err)
    else:
        logger.error("[%s] LLM 连续返回空响应", node_name)
    return ""
