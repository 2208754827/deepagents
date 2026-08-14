#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试用例生成 Agent — 基于 Deep Agents 框架 + LangGraph StateGraph

从需求文档自动生成专业测试用例（XMind/XLSX），具备深度解析、质量自检、自主学习能力。

架构:
  - 主 Agent: 处理用户对话，接收"生成测试用例"指令后调用工作流子 Agent
  - 工作流子 Agent: LangGraph StateGraph 编排，5 个确定性节点
    preprocess → parse → design → review → generate

使用方式:
    conda activate agent
    python agent.py                  # 交互式启动，等待用户输入
    python agent.py --log            # 交互式启动 + 文件日志
    python agent.py --log --debug    # 交互式启动 + 文件日志 + debug
"""

import argparse
import asyncio
import io
import json
import logging
import os
import re
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, Sequence

from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
# 提前导入 ChatOpenAI（下面 MODEL 初始化就要用，原 import 在 330 行太靠后）
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner

from deepagents import (
    CompiledSubAgent,
    ProviderProfile,
    SubAgent,
    create_deep_agent,
    register_provider_profile,
)
from deepagents.backends import FilesystemBackend

# LangGraph
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command

warnings.filterwarnings("ignore", message="Core Pydantic V1 functionality")

# ============================================================
# 项目路径
# ============================================================
BASE_DIR = Path(__file__).parent.resolve()
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


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="测试用例生成 Agent")
    parser.add_argument("--log", action="store_true", help="启用文件日志（写入 logs/ 目录）")
    parser.add_argument("--debug", action="store_true", help="启用 Agent debug 模式")
    parser.add_argument("--input", type=str, default=None, help="命令行传入指令（一次性运行后退出）")
    return parser.parse_args()

# ============================================================
# 加载环境变量
# ============================================================
load_dotenv(BASE_DIR / ".env")

# ============================================================
# Step 1: 主 Agent 模型（官方 DeepSeek，OpenAI 兼容接口）
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
    max_retries=3,
    timeout=120,
)

# 框架 resolve_model() 对 isinstance(model, BaseChatModel) 直接返回，跳过推断
MODEL = MAIN_AGENT
logger.info("Main Agent LLM: %s via %s (official DeepSeek)", DEEPSEEK_MAIN_MODEL, DEEPSEEK_MAIN_URL)

# ============================================================
# Step 1.5: 中转站 Tool Call JSON 修复 Middleware
# ============================================================
class FixToolCallArgsMiddleware(AgentMiddleware):
    """修复中转站返回的 tool_call arguments 格式问题。

    两个修复：
    1. JSON 格式：中转站在 arguments 前面多输出 ``{}``，导致 Extra data 错误
    2. 路径格式：GLM-5.1 等模型会生成 Windows 绝对路径（如 D:\\...），
       框架的 validate_path() 会拒绝。这里自动转换为虚拟路径（/...）

    本 middleware 在模型返回后、工具执行前拦截 AIMessage，统一修正。
    """

    # 项目根目录的实际路径，用于将 Windows 绝对路径转换为虚拟路径
    ROOT_DIR = str(BASE_DIR).replace("\\", "/")

    @classmethod
    def _fix_windows_path(cls, args: dict) -> dict | None:
        """将 tool_call args 中的 Windows 绝对路径转换为虚拟路径。"""
        changed = False
        fixed = {}
        for key, value in args.items():
            if isinstance(value, str) and re.match(r'^[a-zA-Z]:', value):
                posix = value.replace("\\", "/")
                if posix.lower().startswith(cls.ROOT_DIR.lower()):
                    virtual = posix[len(cls.ROOT_DIR):]
                    if not virtual.startswith("/"):
                        virtual = "/" + virtual
                else:
                    virtual = re.sub(r'^[a-zA-Z]:', '', posix)
                    if not virtual.startswith("/"):
                        virtual = "/" + virtual
                fixed[key] = virtual
                changed = True
                logger.debug("Fixed path in args: %s → %s", value[:80], virtual[:80])
            else:
                fixed[key] = value

        return fixed if changed else None

    @staticmethod
    def _fix_args(args_str: str) -> str | None:
        """尝试修复畸形 JSON arguments，返回修复后的字符串或 None。"""
        try:
            json.loads(args_str)
            return None
        except (json.JSONDecodeError, TypeError):
            pass

        cleaned = re.sub(r'^(\{\})+', '', args_str, count=0)
        try:
            json.loads(cleaned)
            return cleaned
        except (json.JSONDecodeError, TypeError):
            pass

        match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', args_str, re.DOTALL)
        if match:
            candidate = match.group(0)
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, TypeError):
                pass

        return None

    def wrap_model_call(self, request, handler):
        response = handler(request)
        return self._fix_response(response)

    async def awrap_model_call(self, request, handler):
        response = await handler(request)
        return self._fix_response(response)

    def _fix_response(self, response):
        if not isinstance(response, ModelResponse):
            return response

        fixed_messages = []
        for msg in response.result:
            if not isinstance(msg, AIMessage):
                fixed_messages.append(msg)
                continue

            needs_rebuild = False

            # --- 1. 修复 invalid_tool_calls 的 JSON 格式 ---
            new_tool_calls = list(msg.tool_calls)
            still_invalid = []
            for itc in msg.invalid_tool_calls:
                raw_args = itc.get("args", "")
                fixed = self._fix_args(raw_args)
                if fixed is not None:
                    new_tool_calls.append({
                        "name": itc.get("name", "unknown"),
                        "args": json.loads(fixed),
                        "id": itc.get("id"),
                        "type": "tool_call",
                    })
                    logger.debug("Fixed tool_call args: %s → %s", raw_args[:80], fixed[:80])
                else:
                    still_invalid.append(itc)

            if new_tool_calls != list(msg.tool_calls) or still_invalid:
                needs_rebuild = True

            # --- 2. 修复 tool_calls 中的 Windows 绝对路径 ---
            path_fixed_tool_calls = []
            for tc in new_tool_calls:
                args = tc.get("args", {})
                if isinstance(args, dict):
                    fixed_args = self._fix_windows_path(args)
                    if fixed_args is not None:
                        tc = {**tc, "args": fixed_args}
                        needs_rebuild = True
                path_fixed_tool_calls.append(tc)

            if not needs_rebuild:
                fixed_messages.append(msg)
            else:
                new_msg = AIMessage(
                    content=msg.content,
                    tool_calls=path_fixed_tool_calls,
                    invalid_tool_calls=still_invalid,
                    additional_kwargs=msg.additional_kwargs,
                    response_metadata=msg.response_metadata,
                    id=msg.id,
                    name=msg.name,
                    usage_metadata=msg.usage_metadata,
                )
                fixed_messages.append(new_msg)

        return ModelResponse(
            result=fixed_messages,
            structured_response=response.structured_response,
        )

logger.info("FixToolCallArgsMiddleware defined")

# ============================================================
# Step 2: 加载参考文档（嵌入 LLM prompt）
# ============================================================
# 参考文档原文很大（总计 ~54K chars），全部塞进 prompt 会导致 API 断连。
# 按节点分配独立上限，只保留各节点最需要的规则定义部分。
#
# 估算：GLM 中转站能承受 ~40K chars prompt（~13K tokens）
#   node_parse:  参考文档 + 记忆(~2K) + 模板(~2K) + 需求文本 → 控制在 ~25K
#   node_design: 参考文档 + 模板(~2K) + 解析结果          → 控制在 ~30K
#   node_review: 模板(~1.5K) + 用例                       → 控制在 ~17K

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

# ============================================================
# Step 3: 创建 LLM 实例（供工作流节点使用）
# ============================================================
from langchain_openai import ChatOpenAI

def _create_llm():
    """创建 LLM 实例，供工作流节点直接调用

    支持通过环境变量切换模型提供商：
    - DEEPSEEK_API_KEY 存在时使用 DeepSeek 官方 API
    - 否则 fallback 到 GLM-5.1 中转站
    """
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com") + "/v1"
        logger.info("Using DeepSeek API: model=%s", model)
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=deepseek_key,
            temperature=0.3,
            max_tokens=8192,
            max_retries=3,
            timeout=120,
        )
    # Fallback: GLM-5.1 中转站
    model = os.environ.get("GLM_MODEL", "GLM-5.1")
    base_url = os.environ.get("GLM_BASE_URL", "https://api.x5m5x.com") + "/v1"
    logger.info("Using GLM proxy: model=%s, base_url=%s", model, base_url)
    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=os.environ.get("GLM_API_KEY"),
        temperature=0.3,
        max_tokens=8192,
        max_retries=3,
        timeout=120,
    )

llm = _create_llm()
logger.info("LLM instance created for workflow nodes")


def _safe_llm_invoke(prompt: str, node_name: str = "") -> str:
    """安全的 LLM 调用：检查 prompt 大小，避免因过大导致 API 断连。

    如果 prompt 超过 MAX_PROMPT_CHARS，在最后一段需求文本处截断后重试。
    """
    if len(prompt) <= MAX_PROMPT_CHARS:
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip() if isinstance(response.content, str) else str(response.content)

    # prompt 过大：尝试在需求文本部分截断
    logger.warning(
        "%s node: prompt too large (%d > %d), attempting truncation",
        node_name, len(prompt), MAX_PROMPT_CHARS,
    )

    # 找到需求文本/解析结果/用例部分并截断
    excess = len(prompt) - MAX_PROMPT_CHARS + 2000  # 留 2000 字符余量
    # 尝试在 "## 需求文本" / "## 解析结果" / "## 测试用例" 标记之后截断
    for marker in ["## 需求文本", "## 解析结果", "## 测试用例"]:
        idx = prompt.rfind(marker)
        if idx >= 0:
            # 从 marker 位置之后截断
            cut_start = idx + len(marker)
            cut_pos = prompt.rfind('\n', 0, len(prompt) - excess)
            if cut_pos > cut_start:
                prompt = prompt[:cut_pos] + "\n\n[... 文本过长，已截断 ...]"
                break

    if len(prompt) > MAX_PROMPT_CHARS:
        # 最终兜底：直接截断到 MAX_PROMPT_CHARS
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n\n[... 文本过长，已截断 ...]"

    logger.warning("%s node: prompt truncated to %d chars", node_name, len(prompt))
    response = llm.invoke([HumanMessage(content=prompt)])
    return response.content.strip() if isinstance(response.content, str) else str(response.content)

# ============================================================
# Step 4: 定义结构化输出模型
# ============================================================
class ReviewChecklistItem(BaseModel):
    item: str
    result: str
    passed: bool

class ReviewIssue(BaseModel):
    severity: str
    category: str
    description: str
    suggestion: str
    affected_cases: list[int] = Field(default_factory=list)

class ReviewResult(BaseModel):
    status: str  # "passed" | "failed"
    checklist: list[ReviewChecklistItem]
    issues: list[ReviewIssue]
    summary: str

# ============================================================
# Step 5: 定义工作流状态
# ============================================================
from typing import TypedDict


def _append_messages(current: list, update: list) -> list:
    """Reducer: 追加消息到列表"""
    if current is None:
        current = []
    if update is None:
        return current
    return current + update


def _append_strings(current: list, update: list) -> list:
    """Reducer: 追加字符串到列表（用于 status_messages）"""
    if current is None:
        current = []
    if update is None:
        return current
    return current + update


class WorkflowState(TypedDict):
    """LangGraph 工作流状态

    每个节点返回一个 dict，LangGraph 自动合并到状态中。
    messages 字段用于 CompiledSubAgent 返回结果给主 Agent。
    """

    # 必需：CompiledSubAgent 要求 state 中有 messages key
    # add_messages reducer: 新消息追加到列表，而不是覆盖
    messages: Annotated[list[AnyMessage], add_messages]

    # 工作流中间数据
    requirement_dir: str          # 需求文档目录（由主 Agent 从 description 解析传入）
    requirement_file: str         # 需求文档文件路径（单个 .docx/.pdf/.md 文件）
    requirement_text: str         # 预处理后的需求文本
    section_range: str            # 用户指定章节范围（数字编号/语义关键词/空）
    extracted_text: str           # 截取后的需求文本（parse 节点读这个）
    chapter_outline: str          # 章节目录 JSON
    parsed_result: str            # 解析结果 JSON
    test_cases: str               # 设计的用例 JSON
    review_result: str            # 审查结果 JSON
    output_files: str             # 生成文件路径 JSON
    retry_count: int              # 审查重试次数
    status_messages: Annotated[list[str], _append_strings]  # 进度消息列表（追加）
    error: str                    # 错误信息

# ============================================================
# Step 5.5: Markdown 标题解析 + 章节截取辅助函数
# ============================================================

# Markdown 标题行正则：# 标题 或 ## 标题 等
_RE_MD_HEADING = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)
# 数字编号标题正则：如 "5.1 企业所得税"、"7.3.1 印花税计算"
_RE_NUM_HEADING = re.compile(r'^(\d+(?:\.\d+)*)\.?\s+(.+)$', re.MULTILINE)

MAX_EXTRACT_CHARS = 30000  # 自动截取时的字符上限
MAX_PARSE_CHARS = 20000    # node_parse 的需求文本上限
MAX_DESIGN_CHARS = 20000   # node_design 的 parsed_result 上限
MAX_REVIEW_CHARS = 20000   # node_review 的 test_cases 上限
MAX_PROMPT_CHARS = 60000   # 单次 LLM invoke 的总 prompt 字符上限


def _parse_markdown_outline(text: str) -> list[dict]:
    """解析 Markdown 文本的标题结构，返回章节目录。

    返回: [{"level": 1, "title": "...", "number": "5.1", "start": 0, "end": 1500}, ...]
    start/end 是该章节内容在原文中的字符位置（含标题行到下一标题行之前）。
    """
    headings: list[dict] = []

    # 1. 收集所有标题行及其位置
    for m in _RE_MD_HEADING.finditer(text):
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append({
            "level": level,
            "title": title,
            "number": "",
            "start": m.start(),
        })

    # 2. 对于 # 标题，尝试从标题文本中提取数字编号
    #    如 "# 5.1 企业所得税" → number="5.1"
    for h in headings:
        num_match = _RE_NUM_HEADING.match(h["title"])
        if num_match:
            h["number"] = num_match.group(1)

    # 3. 计算 end 位置（到下一个同级或更高级标题之前）
    for i, h in enumerate(headings):
        # 找下一个同级或更高级标题
        end = len(text)
        for j in range(i + 1, len(headings)):
            if headings[j]["level"] <= h["level"]:
                end = headings[j]["start"]
                break
        h["end"] = end

    return headings


def _match_section_by_number(outline: list[dict], section_range: str) -> list[dict]:
    """按数字编号匹配章节。

    section_range 格式:
    - "5" → 匹配 number 以 "5" 开头的所有章节（含子节）
    - "5-7" → 匹配 number 在 5~7 范围的一级章节（含子节）
    - "5.1-5.3" → 匹配 number 在 5.1~5.3 范围的章节
    """
    if not section_range or not outline:
        return []

    # 解析范围
    range_match = re.match(r'^(\d+(?:\.\d+)*)\s*[-–]\s*(\d+(?:\.\d+)*)$', section_range)
    if range_match:
        start_num = range_match.group(1)
        end_num = range_match.group(2)
    else:
        start_num = section_range.strip()
        end_num = None

    matched = []
    for h in outline:
        num = h.get("number", "")
        if not num:
            continue

        # 单个编号匹配：前缀匹配（"5" 匹配 "5"、"5.1"、"5.2.3" 等）
        if end_num is None:
            if num == start_num or num.startswith(start_num + "."):
                matched.append(h)
        else:
            # 范围匹配：比较编号前缀
            # "5-7" → 匹配一级编号 5,6,7 及其子节
            # "5.1-5.3" → 匹配 5.1, 5.2, 5.3 及其子节
            start_parts = start_num.split(".")
            end_parts = end_num.split(".")
            num_parts = num.split(".")

            # 比较到范围的最深层级
            depth = max(len(start_parts), len(end_parts))
            in_range = True
            for d in range(depth):
                n_val = int(num_parts[d]) if d < len(num_parts) else 0
                s_val = int(start_parts[d]) if d < len(start_parts) else 0
                e_val = int(end_parts[d]) if d < len(end_parts) else 999

                if n_val < s_val or n_val > e_val:
                    in_range = False
                    break
                # 如果在范围内，继续比较更深层级
                if n_val > s_val and n_val < e_val:
                    # 已经在范围中间，子节都算
                    in_range = True
                    break

            if in_range:
                matched.append(h)

    return matched


def _match_section_by_keyword(outline: list[dict], keyword: str) -> list[dict]:
    """按关键词在标题中模糊匹配章节。

    keyword 会在标题文本中做子串匹配（忽略大小写）。
    返回匹配到的章节（含子节）。
    """
    if not keyword or not outline:
        return []

    keyword_lower = keyword.lower()
    matched = []

    # 找到标题包含关键词的章节
    primary_matches = []
    for h in outline:
        if keyword_lower in h["title"].lower():
            primary_matches.append(h)

    if not primary_matches:
        return []

    # 收集匹配章节及其子节
    for h in primary_matches:
        matched.append(h)
        # 子节：level 更深，start 在当前章节范围内
        for sub in outline:
            if sub["level"] > h["level"] and sub["start"] >= h["start"] and sub["start"] < h["end"]:
                matched.append(sub)

    return matched


def _extract_text_by_headings(text: str, headings: list[dict]) -> str:
    """根据匹配到的标题列表，从原文中截取对应文本。

    合并所有匹配章节的文本范围，去重排序后截取。
    """
    if not headings:
        return ""

    # 合并重叠的范围
    ranges = sorted([(h["start"], h["end"]) for h in headings], key=lambda x: x[0])
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # 截取并拼接
    parts = [text[start:end] for start, end in merged]
    return "\n\n".join(parts).strip()


def _extract_auto(text: str, outline: list[dict], max_chars: int = MAX_EXTRACT_CHARS) -> str:
    """自动截取：在标题边界处截取前 max_chars 字符，保证末章节完整。"""
    if len(text) <= max_chars:
        return text

    # 找到不超过 max_chars 的最后一个标题边界
    cut_pos = max_chars
    for h in outline:
        if h["start"] > max_chars:
            # 回退到前一个标题的开始位置
            cut_pos = h["start"]
            break
    else:
        # 所有标题都在 max_chars 内，直接截取
        cut_pos = max_chars

    # 如果 cut_pos 还是太大（没有标题在 max_chars 之后），用最后一个标题的 end
    if cut_pos > max_chars and outline:
        # 找 start <= max_chars 的最后一个标题
        last_heading = None
        for h in outline:
            if h["start"] <= max_chars:
                last_heading = h
            else:
                break
        if last_heading:
            cut_pos = min(last_heading["end"], len(text))
        else:
            cut_pos = max_chars

    return text[:cut_pos].strip()


def _locate_section_by_ai(outline: list[dict], keyword: str) -> str:
    """AI 兜底：只发目录 JSON 给 LLM，让它定位章节编号。

    返回定位到的 section_range 字符串（如 "6.2.4-6.2.6"），空字符串表示定位失败。
    """
    # 构建精简目录
    outline_items = []
    for h in outline:
        prefix = "  " * (h["level"] - 1)
        num_str = f"{h['number']} " if h["number"] else ""
        outline_items.append(f"{prefix}{num_str}{h['title']}")

    outline_text = "\n".join(outline_items)

    # 截断目录（避免目录本身过长）
    if len(outline_text) > 4000:
        outline_text = outline_text[:4000] + "\n[... 目录过长，已截断 ...]"

    prompt = f"""以下是一份需求文档的章节目录。请找到与"{keyword}"最相关的章节编号。

只输出章节编号范围，格式示例：
- 单个章节：5.1
- 章节范围：6.2.4-6.2.6
- 整章：5

如果找不到相关章节，输出空字符串。

## 章节目录

{outline_text}"""

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        content = response.content.strip() if isinstance(response.content, str) else str(response.content)
        # 提取编号范围
        match = re.match(r'^(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)$', content)
        if match:
            return match.group(1)
        # 尝试从输出中提取
        match = re.search(r'(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)', content)
        if match:
            return match.group(1)
        return ""
    except Exception as e:
        logger.warning("AI section locate failed: %s", e)
        return ""


# ============================================================
# Step 5.6: 指令关键词提取辅助函数
# ============================================================

# 指令动词（复合动词优先匹配，避免"写出"被"出"抢先匹配）
_INSTR_COMPOUND_VERBS = r'(?:生成|写出|做出|跑出|运行)'
_INSTR_SINGLE_VERBS = r'(?:写|做|跑|出)'
_INSTR_SUFFIXES = r'(?:一下|一些|一下下|些)'
_INSTR_TARGETS = r'(?:测试用例|用例|测试|需求文档|文档|测试文档)'


def _extract_section_range(after_path: str) -> str:
    """从路径后的文本中提取章节关键词。

    剥离指令词策略：先删除末尾的"测试用例"等目标词，
    再删除开头/末尾的指令动词，剩余即为关键词。

    Examples:
        "生成一下印花税计算的测试用例" → "印花税计算"
        "印花税计算 生成测试用例" → "印花税计算"
        "写出登录安全部分的测试用例" → "登录安全部分"
        "生成测试用例" → ""
    """
    cleaned = after_path.strip().strip('"').strip()
    if not cleaned:
        return ""

    # 1. 删除末尾的指令目标词（的测试用例 / 测试用例 / 测试）
    cleaned = re.sub(r'(?:的)?' + _INSTR_TARGETS + r'\s*$', '', cleaned)

    # 2. 删除开头的复合指令动词+后缀（生成一下 / 写出 / 做一下）
    cleaned = re.sub(r'^' + _INSTR_COMPOUND_VERBS + _INSTR_SUFFIXES + r'?\s*', '', cleaned)
    # 3. 删除开头的单字指令动词+后缀（写一下 / 做一下）
    cleaned = re.sub(r'^' + _INSTR_SINGLE_VERBS + _INSTR_SUFFIXES + r'?\s*', '', cleaned)

    # 4. 删除末尾的指令动词（印花税计算 生成 → 印花税计算）
    cleaned = re.sub(r'\s*' + _INSTR_COMPOUND_VERBS + _INSTR_SUFFIXES + r'?\s*$', '', cleaned)
    cleaned = re.sub(r'\s*' + _INSTR_SINGLE_VERBS + _INSTR_SUFFIXES + r'?\s*$', '', cleaned)

    # 5. 删除末尾的"的"
    cleaned = re.sub(r'的$', '', cleaned).strip()

    return cleaned if (cleaned and len(cleaned) <= 30) else ""


# ============================================================
# Step 6: 工作流节点实现
# ============================================================

def _run_script(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """运行脚本，返回 (returncode, stdout, stderr)"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            cwd=str(BASE_DIR),
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def node_entry(state: WorkflowState) -> dict:
    """入口节点: 从 messages 中提取需求文档路径和章节范围

    主 Agent 通过 task() 调用工作流时，description 会被放入 messages。
    例如: task(subagent_type="generate-workflow", description="根据 C:/Users/test.docx 第5章生成测试用例")
    本节点解析 description，提取文件路径、目录路径、章节范围，设置到状态中。
    """
    messages = state.get("messages", [])
    description = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            description = msg.content or ""
            break

    requirement_file = ""
    requirement_dir = ""
    section_range = ""

    if description:
        # 尝试提取文件路径（支持 .docx/.doc/.pdf/.md）
        # 路径可能包含中文、空格等字符，用贪婪匹配到已知扩展名
        file_match = re.search(
            r'([a-zA-Z]:[\\\/].+?\.(?:docx|doc|pdf|md))',
            description,
            re.IGNORECASE,
        )
        if file_match:
            requirement_file = file_match.group(1).replace("/", "\\")
        else:
            # 尝试提取目录路径（以 \ 或 / 结尾，或后面跟着空格/中文标点）
            dir_match = re.search(
                r'([a-zA-Z]:[\\\/].+?[\\\/])(?:\s|，|。|$)',
                description,
            )
            if dir_match:
                path = dir_match.group(1)
                if Path(path).is_dir():
                    requirement_dir = path.replace("/", "\\")

        # 提取章节范围
        # 1. 数字编号范围：5-7、5.1-5.3
        range_match = re.search(
            r'(?:第|章节|节)\s*(\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?)',
            description,
        )
        if range_match:
            section_range = range_match.group(1).replace(" ", "")
        else:
            # 2. 单个数字编号：第5章、5.1节
            num_match = re.search(
                r'(?:第\s*(\d+)\s*章|(\d+(?:\.\d+)?)\s*节)',
                description,
            )
            if num_match:
                section_range = num_match.group(1) or num_match.group(2)
            else:
                # 3. 语义关键词：印花税计算章节、价税分离部分
                kw_match = re.search(
                    r'(?:截取|提取|只看|只要|针对|关于|的)\s*["""]?([^"""]+?)["""]?\s*(?:章节|部分|模块|内容|需求)',
                    description,
                )
                if kw_match:
                    section_range = kw_match.group(1).strip()
                else:
                    # 4. 直接跟在路径后面的章节描述
                    #    "生成一下印花税计算的测试用例" → "印花税计算"
                    #    "印花税计算 生成测试用例" → "印花税计算"
                    after_path = description
                    if file_match:
                        after_path = description[file_match.end():]
                    elif dir_match:
                        after_path = description[dir_match.end():]
                    section_range = _extract_section_range(after_path)

    updates = {}
    if requirement_file:
        updates["requirement_file"] = requirement_file
    if requirement_dir:
        updates["requirement_dir"] = requirement_dir
    if section_range:
        updates["section_range"] = section_range

    return updates if updates else {}


def node_init_memory(state: WorkflowState) -> dict:
    """Phase 0: 初始化 .memory/ 目录"""
    memory_dir = BASE_DIR / ".memory"
    if memory_dir.exists() and (memory_dir / "project-context.json").exists():
        return {"status_messages": ["[初始化] .memory/ 已存在，跳过"]}

    rc, stdout, stderr = _run_script(
        [sys.executable, "scripts/memory_manager.py", "--action", "init", "--project", "."],
    )
    if rc == 0:
        return {"status_messages": ["[初始化] .memory/ 创建成功"]}
    else:
        logger.warning("memory_manager init failed: %s", stderr)
        return {"status_messages": [f"[初始化] memory_manager 失败: {stderr[:200]}"]}


def node_preprocess(state: WorkflowState) -> dict:
    """Phase 1: 预处理需求文档

    支持三种输入：
    1. requirement_file: 单个文件路径（.docx/.pdf/.md）
    2. requirement_dir: 目录路径，扫描其中所有文档
    3. 默认: 扫描 requirements/ 目录

    纯 Python 实现：转换非 Markdown 文件、读取文本。
    """
    req_file = state.get("requirement_file", "")
    req_dir = state.get("requirement_dir", "")

    # 优先处理单个文件
    if req_file:
        file_path = Path(req_file)
        if not file_path.exists():
            return {
                "error": f"需求文件不存在: {req_file}",
                "status_messages": [f"[预处理] 错误: 需求文件不存在 {req_file}"],
            }

        # 如果是 .md 文件，直接读取
        if file_path.suffix.lower() == ".md":
            try:
                content = file_path.read_text(encoding="utf-8")
                return {
                    "requirement_text": content.strip(),
                    "status_messages": [f"[预处理] 读取 {file_path.name}，共 {len(content)} 字符"],
                }
            except Exception as e:
                return {
                    "error": f"读取文件失败: {e}",
                    "status_messages": [f"[预处理] 错误: 读取 {req_file} 失败"],
                }

        # 如果是 .docx/.pdf，先转换
        if file_path.suffix.lower() in (".docx", ".doc", ".pdf"):
            # 将文件复制到 requirements/ 目录
            import shutil
            req_dir_path = BASE_DIR / "requirements"
            req_dir_path.mkdir(exist_ok=True)
            dest = req_dir_path / file_path.name
            if not dest.exists():
                shutil.copy2(str(file_path), str(dest))

            # 运行转换脚本
            rc, stdout, stderr = _run_script(
                [sys.executable, "scripts/requirements_preprocessor.py", "--root", str(req_dir_path)],
            )
            if rc != 0:
                logger.warning("preprocessor script failed: %s", stderr)
                return {
                    "error": f"转换文件失败: {stderr[:200]}",
                    "status_messages": [f"[预处理] 错误: 转换 {file_path.name} 失败"],
                }

            # 读取转换后的 .md
            md_file = req_dir_path / (file_path.stem + ".md")
            if md_file.exists():
                content = md_file.read_text(encoding="utf-8")
                return {
                    "requirement_text": content.strip(),
                    "status_messages": [f"[预处理] 转换并读取 {file_path.name}，共 {len(content)} 字符"],
                }
            else:
                return {
                    "error": f"转换后未找到 .md 文件: {md_file}",
                    "status_messages": [f"[预处理] 错误: 转换 {file_path.name} 后未生成 .md"],
                }

        # 其他格式，尝试直接读取
        try:
            content = file_path.read_text(encoding="utf-8")
            return {
                "requirement_text": content.strip(),
                "status_messages": [f"[预处理] 读取 {file_path.name}，共 {len(content)} 字符"],
            }
        except Exception as e:
            return {
                "error": f"不支持的文件格式: {file_path.suffix}",
                "status_messages": [f"[预处理] 错误: 不支持 {file_path.suffix} 格式"],
            }

    # 处理目录
    if not req_dir:
        req_dir = str(BASE_DIR / "requirements")

    req_path = Path(req_dir)
    if not req_path.exists():
        return {
            "error": f"需求目录不存在: {req_dir}",
            "status_messages": [f"[预处理] 错误: 需求目录不存在 {req_dir}"],
        }

    # 1. 转换非 Markdown 文件
    rc, stdout, stderr = _run_script(
        [sys.executable, "scripts/requirements_preprocessor.py", "--root", str(req_path)],
    )
    if rc != 0:
        logger.warning("preprocessor script failed (non-fatal): %s", stderr)

    # 2. 读取所有 .md 文件
    md_files = sorted(req_path.rglob("*.md"))
    if not md_files:
        return {
            "error": f"需求目录中没有 Markdown 文件: {req_dir}",
            "status_messages": [f"[预处理] 错误: 没有找到 .md 文件"],
        }

    texts = []
    for f in md_files:
        try:
            content = f.read_text(encoding="utf-8")
            if content.strip():
                texts.append(content.strip())
        except Exception as e:
            logger.warning("Failed to read %s: %s", f, e)

    full_text = "\n\n---\n\n".join(texts)

    return {
        "requirement_text": full_text,
        "status_messages": [
            f"[预处理] 读取 {len(md_files)} 个需求文件，共 {len(full_text)} 字符"
        ],
    }


def node_extract(state: WorkflowState) -> dict:
    """Phase 1.5: 文档截取节点

    对大文档按章节截取，避免 parse 节点硬截断导致内容丢失。
    - 小文档（≤15000字符）：直接透传
    - 大文档 + 指定章节编号：按编号截取
    - 大文档 + 语义关键词：先关键词匹配标题，匹配不到则 AI 兜底
    - 大文档 + 无指定：自动在标题边界截取前 15000 字符
    """
    requirement_text = state.get("requirement_text", "")
    section_range = state.get("section_range", "")

    if not requirement_text:
        return {
            "error": "没有需求文本可截取",
            "status_messages": ["[截取] 错误: 没有需求文本"],
        }

    # 小文档直接透传
    if len(requirement_text) <= MAX_EXTRACT_CHARS:
        return {
            "extracted_text": requirement_text,
            "status_messages": [f"[截取] 文档较小（{len(requirement_text)}字符），无需截取"],
        }

    # 大文档：解析目录
    outline = _parse_markdown_outline(requirement_text)

    # 生成章节目录 JSON
    outline_summary = []
    for h in outline:
        prefix = "  " * (h["level"] - 1)
        num_str = f"{h['number']} " if h["number"] else ""
        outline_summary.append(f"{prefix}{num_str}{h['title']}")
    chapter_outline = json.dumps(outline_summary, ensure_ascii=False)

    # 如果没有标题结构，fallback 到字符截取
    if not outline:
        extracted = requirement_text[:MAX_EXTRACT_CHARS]
        return {
            "extracted_text": extracted,
            "chapter_outline": "[]",
            "status_messages": [
                f"[截取] 文档无标题结构，截取前 {MAX_EXTRACT_CHARS} 字符（共 {len(requirement_text)} 字符）"
            ],
        }

    # 判断 section_range 类型并截取
    if not section_range:
        # 无指定：自动截取
        extracted = _extract_auto(requirement_text, outline)
        # 统计截取了哪些章节
        covered = [h for h in outline if h["start"] < len(extracted)]
        covered_titles = [f"{h['number']} {h['title']}" if h['number'] else h['title'] for h in covered[:5]]
        more = f" 等{len(covered)}个章节" if len(covered) > 5 else ""
        return {
            "extracted_text": extracted,
            "chapter_outline": chapter_outline,
            "status_messages": [
                f"[截取] 自动截取前 {len(extracted)} 字符（共 {len(requirement_text)} 字符），"
                f"覆盖: {', '.join(covered_titles)}{more}"
            ],
        }

    # 判断是数字编号还是语义关键词
    is_number_range = bool(re.match(r'^\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?$', section_range))

    if is_number_range:
        # 数字编号匹配
        matched = _match_section_by_number(outline, section_range)
        if matched:
            extracted = _extract_text_by_headings(requirement_text, matched)
            titles = [f"{h['number']} {h['title']}" for h in matched if h['number']][:5]
            return {
                "extracted_text": extracted,
                "chapter_outline": chapter_outline,
                "status_messages": [
                    f"[截取] 按编号 '{section_range}' 截取，{len(extracted)} 字符，"
                    f"匹配: {', '.join(titles)}"
                ],
            }
        else:
            # 编号匹配不到，fallback 到自动截取
            extracted = _extract_auto(requirement_text, outline)
            return {
                "extracted_text": extracted,
                "chapter_outline": chapter_outline,
                "status_messages": [
                    f"[截取] 编号 '{section_range}' 未匹配到章节，自动截取前 {len(extracted)} 字符"
                ],
            }
    else:
        # 语义关键词匹配
        matched = _match_section_by_keyword(outline, section_range)
        if matched:
            extracted = _extract_text_by_headings(requirement_text, matched)
            titles = [f"{h['number']} {h['title']}" for h in matched if h['number']][:5]
            return {
                "extracted_text": extracted,
                "chapter_outline": chapter_outline,
                "status_messages": [
                    f"[截取] 按关键词 '{section_range}' 截取，{len(extracted)} 字符，"
                    f"匹配: {', '.join(titles)}"
                ],
            }

        # 关键词匹配不到，AI 兜底
        ai_result = _locate_section_by_ai(outline, section_range)
        if ai_result:
            ai_matched = _match_section_by_number(outline, ai_result)
            if ai_matched:
                extracted = _extract_text_by_headings(requirement_text, ai_matched)
                titles = [f"{h['number']} {h['title']}" for h in ai_matched if h['number']][:5]
                return {
                    "extracted_text": extracted,
                    "chapter_outline": chapter_outline,
                    "status_messages": [
                        f"[截取] AI 定位 '{section_range}' → '{ai_result}'，{len(extracted)} 字符，"
                        f"匹配: {', '.join(titles)}"
                    ],
                }

        # AI 也定位不到，fallback 到自动截取
        extracted = _extract_auto(requirement_text, outline)
        return {
            "extracted_text": extracted,
            "chapter_outline": chapter_outline,
            "status_messages": [
                f"[截取] 关键词 '{section_range}' 未匹配到章节，自动截取前 {len(extracted)} 字符。"
                f"可指定章节编号重试（目录见日志）"
            ],
        }


def node_parse(state: WorkflowState) -> dict:
    """Phase 2: 用 LLM 解析需求文本

    将需求文本 + 解析规范一起发给 LLM，获取结构化解析结果。
    如果需求文本过长，截断到合理长度以避免 API 超时。
    """
    # 优先使用 extract 节点截取后的文本，fallback 到原始需求文本
    requirement_text = state.get("extracted_text", "") or state.get("requirement_text", "")
    if not requirement_text:
        return {
            "error": "没有需求文本可解析",
            "status_messages": ["[解析] 错误: 没有需求文本"],
        }

    # 安全截断：extract 截出的章节仍可能过大，避免 API 超时/断连
    truncation_notice = ""
    if len(requirement_text) > MAX_PARSE_CHARS:
        cutoff = requirement_text.rfind('\n', 0, MAX_PARSE_CHARS)
        if cutoff < MAX_PARSE_CHARS * 0.8:
            cutoff = MAX_PARSE_CHARS  # 找不到合适换行符时硬截
        original_len = len(requirement_text)
        requirement_text = requirement_text[:cutoff]
        truncation_notice = (
            f"\n\n[注意：原文档此章节共 {original_len} 字符，"
            f"因长度限制已截断至 {cutoff} 字符。"
            f"如需完整解析请指定更小的章节范围。]"
        )
        logger.warning(
            "Parse node: text still too large after extract (%d > %d), truncated to %d",
            original_len, MAX_PARSE_CHARS, cutoff,
        )

    # 读取记忆文件
    memory_context = ""
    memory_dir = BASE_DIR / ".memory"
    for fname in ["terminology.json", "ambiguity-decisions.json", "generation-history.json", "user-preferences.json"]:
        fpath = memory_dir / fname
        if fpath.exists():
            try:
                content = fpath.read_text(encoding="utf-8")
                memory_context += f"\n### {fname}\n{content}\n"
            except Exception:
                pass

    # 截断记忆上下文（避免 prompt 过大）
    if len(memory_context) > 3000:
        memory_context = memory_context[:3000] + "\n\n[... 记忆过长，已截断 ...]"

    prompt = f"""你是需求解析 Agent，从需求文本中提取结构化信息。

## 参考规范
{PARSING_RULES_MD}
{MEMORY_SCHEMA_MD}

## 已有记忆
{memory_context}

## 你的目标

把需求文本解析为结构化 JSON，包含模块、需求ID、测试要素、歧义等。

## 步骤

1. 识别需求ID（REQ-xxx/F1.2/US_042 等模式，无则生成 MOD_{{缩写}}_{{序号}}）
2. 从标题层级提取功能模块
3. 提取验收条件、业务规则
4. 提取测试要素：等价类/边界值/场景流/错误推测的触发点
5. 深层扫描：派生字段、状态机、数据权限（仅需求明确提到时）
6. 歧义检测
7. 检查 user-preferences.json 的 default_tag，扫描需求中的平台声明

## 输出格式（JSON）

```json
{{{{
  "status": "ok",
  "project_name": "",
  "modules": [
    {{{{
      "name": "",
      "requirements": [{{{{"id": "", "text": "", "type": "functional"}}}}],
      "acceptance_criteria": [],
      "business_rules": [],
      "test_elements": {{{{
        "equivalence_partitions": [],
        "boundary_values": [],
        "scenario_flows": {{{{"basic_flow": [], "alternative_flows": [], "exception_flows": []}}}},
        "error_guessing_triggers": [],
        "derived_fields": [],
        "state_machines": [],
        "data_permissions": []
      }}}}
    }}}}
  ],
  "ambiguities": [],
  "tag_info": {{{{"from_doc": null, "from_memory": null, "need_ask": false}}}},
  "business_rule_matches": [],
  "statistics": {{{{"total_modules": 0, "total_requirements": 0, "total_rules": 0}}}}
}}}}
```

如果出错，返回 {{{{"status": "error", "message": "错误描述"}}}}。

只输出 JSON，不要输出其他内容。

## 需求文本

{requirement_text}{truncation_notice}
"""

    try:
        content = _safe_llm_invoke(prompt, "parse")

        # 尝试提取 JSON
        parsed = _extract_json(content)
        if parsed is None:
            logger.warning("Parse node: LLM output is not valid JSON, storing raw text")
            parsed = content

        return {
            "parsed_result": parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False),
            "status_messages": ["[解析] 需求解析完成"],
        }
    except Exception as e:
        logger.exception("Parse node failed")
        return {
            "error": f"解析失败: {e}",
            "status_messages": [f"[解析] 错误: {e}"],
        }


def node_design(state: WorkflowState) -> dict:
    """Phase 3: 用 LLM 设计测试用例

    将解析结果 + 设计规范一起发给 LLM，生成测试用例。
    """
    parsed_result = state.get("parsed_result", "")
    if not parsed_result:
        return {
            "error": "没有解析结果可设计用例",
            "status_messages": ["[设计] 错误: 没有解析结果"],
        }

    # 如果 review 失败重试，附加 review issues
    review_feedback = ""
    review_result_str = state.get("review_result", "")
    retry_count = state.get("retry_count", 0)
    if retry_count > 0 and review_result_str:
        try:
            review_data = json.loads(review_result_str) if isinstance(review_result_str, str) else review_result_str
            if isinstance(review_data, dict) and review_data.get("issues"):
                issues_text = json.dumps(review_data["issues"], ensure_ascii=False, indent=2)
                review_feedback = f"""

## 审查反馈（第 {retry_count} 次修正）

上一次审查未通过，以下是需要修正的问题：

{issues_text}

请根据以上问题修正测试用例，重新输出完整的用例 JSON。
"""
        except (json.JSONDecodeError, TypeError):
            pass

    # 截断解析结果（避免 prompt 过大导致 API 断连）
    if len(parsed_result) > MAX_DESIGN_CHARS:
        parsed_result = parsed_result[:MAX_DESIGN_CHARS] + "\n\n[... 解析结果过长，已截断 ...]"
        logger.info("Design node: parsed_result truncated to %d chars", MAX_DESIGN_CHARS)

    prompt = f"""你是测试用例设计 Agent，按规范设计完整测试用例。

## 参考规范
{TEST_DESIGN_MD}
{BUSINESS_RULES_MD}
{TEST_PRIORITY_MD}
{TRACEABILITY_MD}

## 你的目标

根据解析结果设计测试用例，覆盖所有需求，满足质量标准。

## 设计方法（按需使用）

- **EP 等价类划分**: 有效类合并1条，每个无效类单独1条
- **BVA 边界值**: 上点/离点/内点各1条
- **ST 场景法**: 基本流→备选流→异常流
- **EG 错误推测**: 特殊字符/极端值/并发/空值
- **业务规则**: 匹配 business_rule_matches 补充
- **派生字段**: 正向推导+编辑性控制+源字段变更联动+空值容错
- **状态机**: 按钮活性+字段可编辑性+合法转移+非法拦截
- **去重**: 状态变体去重+场景修饰词去重+EP≈EG合并

## 写作规范

- 标题: "动作+对象+条件/场景"，不用"正常""正确"
- 操作列: 仅用户物理动作，不用 `->` 符号
- 预期列: 仅系统断言，不重复用户操作
- 前置数据准备写入 `前置条件` 字段
- 序号单层 N. 格式
- 模块末级按功能区域分组: 列表/新增/编辑/详情/删除/导出/导入/权限

## 输出格式（JSON）

```json
{{{{
  "status": "ok",
  "cases": [
    {{{{
      "模块": ["模块名", "功能区域"],
      "用例标题": "",
      "优先级": "P0",
      "需求ID": "",
      "设计方法": ["EP"],
      "前置条件": "",
      "步骤": [{{{{"操作": "1. ", "预期": "1. "}}}}],
      "标签": ""
    }}}}
  ],
  "statistics": {{{{
    "total_cases": 0,
    "by_priority": {{{{"P0": 0, "P1": 0, "P2": 0, "P3": 0}}}},
    "by_method": {{{{"EP": 0, "BVA": 0, "ST": 0, "EG": 0}}}},
    "by_module": {{{{}}}},
    "coverage": {{{{"total_requirements": 0, "covered_requirements": 0, "coverage_rate": "0%", "uncovered": []}}}}
  }}}},
  "coverage_matrix": []
}}}}
```

## 质量目标

- 用例数 35-55 条
- P0: 10-15%, P1: 30-40%, P2: 30-40%, P3: 10-20%
- 覆盖率 ≥ 95%
- Scope 完整度 = 100%

如果出错，返回 {{{{"status": "error", "message": "错误描述"}}}}。

只输出 JSON，不要输出其他内容。
{review_feedback}

## 解析结果

{parsed_result}
"""

    try:
        content = _safe_llm_invoke(prompt, "design")

        parsed = _extract_json(content)
        if parsed is None:
            parsed = content

        return {
            "test_cases": parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False),
            "status_messages": ["[设计] 测试用例设计完成"],
        }
    except Exception as e:
        logger.exception("Design node failed")
        return {
            "error": f"设计失败: {e}",
            "status_messages": [f"[设计] 错误: {e}"],
        }


def node_review(state: WorkflowState) -> dict:
    """Phase 4: 用 LLM 进行质量自检

    返回 passed/failed，如果 failed 则附带 issues 列表。
    """
    test_cases = state.get("test_cases", "")
    if not test_cases:
        return {
            "error": "没有用例可审查",
            "status_messages": ["[审查] 错误: 没有用例"],
        }

    # 截断过长用例（避免 prompt 过大）
    if len(test_cases) > MAX_REVIEW_CHARS:
        test_cases = test_cases[:MAX_REVIEW_CHARS] + "\n\n[... 用例过长，已截断 ...]"
        logger.info("Review node: test_cases truncated to %d chars", MAX_REVIEW_CHARS)

    prompt = f"""你是质量审查 Agent，对测试用例进行质量自检。

## 自检清单

1. 覆盖率 ≥ 95%
2. P0 占比 10-15%
3. P1 占比 30-40%
4. P2 占比 30-40%
5. P3 占比 10-20%
6. 每条需求至少关联 1 种设计方法
7. 复杂需求至少关联 2 种设计方法
8. 无需求外编造的场景
9. 无语义重复用例
10. Scope 完整度 = 100%（无遗漏子模块）
11. 派生字段（如有）已生成推导校验 + 编辑性控制用例
12. 状态机（如有）已覆盖所有状态的按钮活性 + 字段可编辑性矩阵
13. 反向操作（如有）已生成冲销/撤销留痕校验断言

## 输出格式（JSON）

```json
{{{{
  "status": "passed",
  "checklist": [{{{{"item": "", "result": "", "passed": true}}}}],
  "issues": [{{{{"severity": "high", "category": "", "description": "", "suggestion": "", "affected_cases": []}}}}],
  "summary": ""
}}}}
```

- status: passed=全部通过 / failed=有问题需修正
- issues 精确到具体用例索引
- 不修改用例，只做检查报告

只输出 JSON，不要输出其他内容。

## 测试用例

{test_cases}
"""

    try:
        content = _safe_llm_invoke(prompt, "review")

        parsed = _extract_json(content)
        if parsed is None:
            parsed = content

        result_str = parsed if isinstance(parsed, str) else json.dumps(parsed, ensure_ascii=False)

        # 判断审查结果
        status_msg = "[审查] 质量自检完成"
        try:
            result_data = json.loads(result_str) if isinstance(result_str, str) else result_str
            if isinstance(result_data, dict):
                if result_data.get("status") == "passed":
                    status_msg = "[审查] ✅ 质量自检通过"
                else:
                    issue_count = len(result_data.get("issues", []))
                    status_msg = f"[审查] ❌ 质量自检未通过，{issue_count} 个问题需修正"
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "review_result": result_str,
            "status_messages": [status_msg],
        }
    except Exception as e:
        logger.exception("Review node failed")
        return {
            "error": f"审查失败: {e}",
            "status_messages": [f"[审查] 错误: {e}"],
        }


def node_generate(state: WorkflowState) -> dict:
    """Phase 5: 生成 XMind/XLSX 文件 + 更新记忆

    纯 Python 实现：保存 JSON、调用脚本、更新记忆。
    """
    test_cases = state.get("test_cases", "")
    if not test_cases:
        return {
            "error": "没有用例可生成文件",
            "status_messages": ["[生成] 错误: 没有用例"],
        }

    # 1. 确保 tmp/ 和 test-docs/ 目录存在
    (BASE_DIR / "tmp").mkdir(exist_ok=True)
    (BASE_DIR / "test-docs").mkdir(exist_ok=True)

    # 2. 获取时间戳
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    # 3. 保存用例 JSON
    cases_file = BASE_DIR / "tmp" / f"cases_{timestamp}.json"
    try:
        # 尝试美化 JSON
        cases_data = json.loads(test_cases) if isinstance(test_cases, str) else test_cases
        if isinstance(cases_data, dict) and "cases" in cases_data:
            cases_list = cases_data["cases"]
        elif isinstance(cases_data, list):
            cases_list = cases_data
        else:
            cases_list = test_cases

        cases_file.write_text(
            json.dumps(cases_list, ensure_ascii=False, indent=2) if isinstance(cases_list, list) else str(cases_list),
            encoding="utf-8",
        )
    except (json.JSONDecodeError, TypeError):
        cases_file.write_text(str(test_cases), encoding="utf-8")

    # 4. 生成 XMind
    xmind_file = BASE_DIR / "test-docs" / f"testcases_{timestamp}.xmind"
    rc_xmind, stdout_xmind, stderr_xmind = _run_script(
        [sys.executable, "scripts/generate_xmind.py",
         "-f", str(cases_file), "-o", str(xmind_file)],
        timeout=30,
    )

    # 5. 生成 XLSX
    xlsx_file = BASE_DIR / "test-docs" / f"testcases_{timestamp}.xlsx"
    rc_xlsx, stdout_xlsx, stderr_xlsx = _run_script(
        [sys.executable, "scripts/generate_xlsx.py",
         "-f", str(cases_file), "-o", str(xlsx_file)],
        timeout=30,
    )

    # 6. 更新记忆
    record = {
        "timestamp": timestamp,
        "total_cases": len(cases_list) if isinstance(cases_list, list) else 0,
        "source_files": [],
    }
    record_json = json.dumps(record, ensure_ascii=False)
    _run_script(
        [sys.executable, "scripts/memory_manager.py",
         "--action", "add-record", "--project", ".", "--data", record_json],
        timeout=15,
    )

    # 构建输出结果
    output = {
        "json_file": str(cases_file),
        "xmind_file": str(xmind_file) if rc_xmind == 0 else None,
        "xlsx_file": str(xlsx_file) if rc_xlsx == 0 else None,
        "xmind_error": stderr_xmind if rc_xmind != 0 else None,
        "xlsx_error": stderr_xlsx if rc_xlsx != 0 else None,
    }

    # 构建摘要
    summary_parts = [f"[生成] 用例 JSON: {cases_file.name}"]
    if rc_xmind == 0:
        summary_parts.append(f"[生成] XMind: {xmind_file.name}")
    else:
        summary_parts.append(f"[生成] XMind 失败: {stderr_xmind[:100]}")
    if rc_xlsx == 0:
        summary_parts.append(f"[生成] XLSX: {xlsx_file.name}")
    else:
        summary_parts.append(f"[生成] XLSX 失败: {stderr_xlsx[:100]}")

    # 构建最终 AIMessage — CompiledSubAgent 框架从 messages 中提取最后一个
    # 有文本的 AIMessage 作为 ToolMessage 内容返回给主 Agent
    final_text = build_final_message(state)
    ai_message = AIMessage(content=final_text)

    return {
        "output_files": json.dumps(output, ensure_ascii=False),
        "status_messages": summary_parts,
        "messages": [ai_message],  # CompiledSubAgent 结果载体
    }


# ============================================================
# Step 7: JSON 提取辅助函数 + 结果消息构建
# ============================================================
def _extract_json(text: str) -> dict | list | None:
    """从 LLM 输出中提取 JSON

    尝试：
    1. 直接解析整个文本
    2. 提取 ```json ... ``` 代码块
    3. 提取第一个 { ... } 或 [ ... ] 对象
    """
    if not text:
        return None

    # 1. 直接解析
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 提取 ```json ... ``` 代码块
    match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. 提取第一个 { ... } 对象
    brace_count = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if brace_count == 0:
                start = i
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if brace_count == 0 and start >= 0:
                try:
                    return json.loads(text[start:i+1])
                except (json.JSONDecodeError, TypeError):
                    start = -1

    # 4. 提取第一个 [ ... ] 数组
    bracket_count = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '[':
            if bracket_count == 0:
                start = i
            bracket_count += 1
        elif ch == ']':
            bracket_count -= 1
            if bracket_count == 0 and start >= 0:
                try:
                    return json.loads(text[start:i+1])
                except (json.JSONDecodeError, TypeError):
                    start = -1

    return None


# ============================================================
# Step 8: 构建工作流图
# ============================================================
def build_workflow_graph() -> StateGraph:
    """构建测试用例生成工作流的 LangGraph StateGraph

    节点流程:
    init_memory → preprocess → extract → parse → design → review → (conditional) → generate

    条件路由:
    review → status=="passed" → generate
    review → status=="failed" && retry_count < 2 → design (重试)
    review → status=="failed" && retry_count >= 2 → generate (强制通过)
    """

    def route_after_review(state: WorkflowState) -> str:
        """审查后的条件路由"""
        review_result_str = state.get("review_result", "")
        retry_count = state.get("retry_count", 0)

        # 解析审查结果
        try:
            result_data = json.loads(review_result_str) if isinstance(review_result_str, str) else review_result_str
            if isinstance(result_data, dict) and result_data.get("status") == "passed":
                return "generate"
        except (json.JSONDecodeError, TypeError):
            # 无法解析，当作通过
            return "generate"

        # failed: 重试还是强制通过？
        if retry_count < 2:
            return "design"  # 回到设计节点重试
        else:
            return "generate"  # 强制生成

    # 构建图
    graph = StateGraph(WorkflowState)

    # 添加节点
    graph.add_node("entry", node_entry)
    graph.add_node("init_memory", node_init_memory)
    graph.add_node("preprocess", node_preprocess)
    graph.add_node("extract", node_extract)
    graph.add_node("parse", node_parse)
    graph.add_node("design", node_design)
    graph.add_node("review", node_review)
    graph.add_node("generate", node_generate)

    # 增加重试计数的中间节点
    def review_with_retry_tracking(state: WorkflowState) -> dict:
        """在路由到 design 前，增加 retry_count"""
        retry_count = state.get("retry_count", 0)
        return {"retry_count": retry_count + 1}

    graph.add_node("increment_retry", review_with_retry_tracking)

    # 添加边
    graph.add_edge(START, "entry")
    graph.add_edge("entry", "init_memory")
    graph.add_edge("init_memory", "preprocess")
    graph.add_edge("preprocess", "extract")
    graph.add_edge("extract", "parse")
    graph.add_edge("parse", "design")
    graph.add_edge("design", "review")

    # 条件路由: review 之后
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {
            "generate": "generate",
            "design": "increment_retry",
        },
    )

    # increment_retry → design（重试）
    graph.add_edge("increment_retry", "design")

    # generate → END
    graph.add_edge("generate", END)

    return graph


def create_workflow_runnable():
    """编译工作流图，返回可执行的 Runnable"""
    graph = build_workflow_graph()
    compiled = graph.compile()
    logger.info("Workflow graph compiled")
    return compiled


# ============================================================
# Step 9: 构建结果消息（给主 Agent 的返回值）
# ============================================================
def build_final_message(state: WorkflowState) -> str:
    """根据工作流状态构建最终返回给主 Agent 的消息"""
    status_msgs = state.get("status_messages", [])
    error = state.get("error", "")

    if error:
        return f"生成失败: {error}\n\n" + "\n".join(status_msgs)

    # 解析统计信息
    test_cases_str = state.get("test_cases", "")
    output_files_str = state.get("output_files", "")

    summary_parts = ["测试用例生成完成！\n"]

    # 用例统计
    try:
        cases_data = json.loads(test_cases_str) if isinstance(test_cases_str, str) else test_cases_str
        if isinstance(cases_data, dict) and "statistics" in cases_data:
            stats = cases_data["statistics"]
            total = stats.get("total_cases", 0)
            by_priority = stats.get("by_priority", {})
            coverage = stats.get("coverage", {})
            summary_parts.append(f"📊 总用例数: {total}")
            summary_parts.append(f"📊 优先级分布: P0={by_priority.get('P0', 0)}, P1={by_priority.get('P1', 0)}, P2={by_priority.get('P2', 0)}, P3={by_priority.get('P3', 0)}")
            summary_parts.append(f"📊 覆盖率: {coverage.get('coverage_rate', 'N/A')}")
    except (json.JSONDecodeError, TypeError):
        pass

    # 输出文件
    try:
        output_data = json.loads(output_files_str) if isinstance(output_files_str, str) else output_files_str
        if isinstance(output_data, dict):
            if output_data.get("json_file"):
                summary_parts.append(f"📄 JSON: {output_data['json_file']}")
            if output_data.get("xmind_file"):
                summary_parts.append(f"🧠 XMind: {output_data['xmind_file']}")
            if output_data.get("xlsx_file"):
                summary_parts.append(f"📊 XLSX: {output_data['xlsx_file']}")
    except (json.JSONDecodeError, TypeError):
        pass

    # 进度消息
    if status_msgs:
        summary_parts.append("\n执行进度:")
        for msg in status_msgs:
            summary_parts.append(f"  {msg}")

    return "\n".join(summary_parts)


# ============================================================
# Step 10: 注册工作流子 Agent + 创建主 Agent
# ============================================================
def create_test_case_agent(debug: bool = False):
    """创建测试用例生成 Agent

    主 Agent 负责用户对话，工作流子 Agent 负责确定性执行生成流程。
    """
    # 编译工作流图
    workflow_runnable = create_workflow_runnable()

    # 注册为 CompiledSubAgent
    workflow_subagent = CompiledSubAgent(
        name="generate-workflow",
        description="执行测试用例生成工作流：预处理需求文档 → 解析 → 设计用例 → 审查 → 生成 XMind/XLSX 文件。当用户要求生成测试用例时使用。",
        runnable=workflow_runnable,
    )

    # 主 Agent 的系统提示词
    ORCHESTRATOR_PROMPT = """你是测试用例生成助手。当用户要求生成测试用例时，你必须立即调用 task 工具，不要做其他操作。

## 调用方式

task(subagent_type="generate-workflow", description="任务描述")

## 重要规则

1. 用户说"生成测试用例"或类似指令时，立即调用 task(subagent_type="generate-workflow", description="...")
2. 不要读取文件、不要搜索目录、不要使用其他工具，只调用 task
3. 如果用户指定了文档路径，把路径写在 description 里
4. task 返回后，把结果展示给用户

## description 格式

- 默认: "生成测试用例"
- 指定路径: "根据 C:/Users/xxx/test.docx 生成测试用例"
- 指定目录: "根据 D:/docs/ 目录生成测试用例"

## 对话风格

简洁中文。只在 task 返回后展示结果。"""

    agent = create_deep_agent(
        model=MODEL,
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=[],
        subagents=[workflow_subagent],
        memory=["./AGENTS.md"],
        middleware=[FixToolCallArgsMiddleware()],
        backend=FilesystemBackend(root_dir=BASE_DIR, virtual_mode=False),
        name="test-case-generator",
        debug=debug,
    )
    logger.info("Deep agent created with model=%s + workflow subagent", MODEL)
    return agent


# ============================================================
# Step 11: 运行时显示
# ============================================================
class AgentDisplay:
    """管理 Agent 运行时的终端显示。

    终端只显示 AI 消息（绿色 Panel）+ spinner 状态。
    工具调用/结果不打印，仅更新 spinner 状态文本。
    当文件日志开启时，工具详情写入日志文件。
    """

    def __init__(self, file_logging: bool = False):
        self.printed_count = 0
        self.current_status = ""
        self.spinner = Spinner("dots", text="思考中...")
        self.file_logging = file_logging

    def reset(self):
        """重置显示状态，为新的一轮对话做准备"""
        self.printed_count = 0
        self.current_status = ""
        self.spinner = Spinner("dots", text="思考中...")

    def update_status(self, status: str):
        self.current_status = status
        self.spinner = Spinner("dots", text=status)

    def print_message(self, msg):
        """处理消息：AI 消息显示到终端，工具调用仅更新 spinner + 记日志"""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        if isinstance(msg, HumanMessage):
            # 用户自己输入的，终端已显示，不重复打印
            pass

        elif isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = "\n".join(text_parts)

            if content and content.strip():
                console.print(Panel(Markdown(content), title="Agent", border_style="green"))
                if self.file_logging:
                    tool_logger.debug("AI: %s", content[:2000])

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "unknown")
                    args = tc.get("args", {})

                    if name == "task":
                        desc = args.get("description", "处理中...")
                        subagent = args.get("subagent_type", "unknown")
                        self.update_status(f"子 Agent [{subagent}]: {desc[:40]}...")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])
                    elif name == "execute":
                        cmd = args.get("command", "")
                        self.update_status(f"执行: {cmd[:40]}...")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])
                    elif name == "write_file":
                        path = args.get("file_path", "file")
                        self.update_status(f"写入: {path}")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])
                    elif name == "read_file":
                        path = args.get("file_path", "file")
                        self.update_status(f"读取: {path}")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])
                    elif name == "glob":
                        pattern = args.get("pattern", "")
                        self.update_status(f"搜索: {pattern}")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])
                    elif name == "grep":
                        pattern = args.get("pattern", "")
                        self.update_status(f"查找: {pattern}")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])
                    else:
                        self.update_status(f"调用: {name}")
                        if self.file_logging:
                            tool_logger.debug("TOOL CALL: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:500])

        elif isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "")
            content_str = str(msg.content) if msg.content else ""
            if name == "task":
                self.update_status("子 Agent 已完成")
            elif name == "execute":
                self.update_status("执行已完成")
            elif name == "write_file":
                self.update_status("写入已完成")
            elif name == "read_file":
                self.update_status("读取已完成")
            else:
                self.update_status("工具已完成")

            if self.file_logging:
                preview = content_str[:500] if content_str else ""
                tool_logger.debug("TOOL RESULT: %s → %s", name, preview)


# ============================================================
# Step 12: 主入口
# ============================================================
async def main():
    """运行测试用例生成 Agent（交互式）"""
    # Fix Windows GBK encoding for stdout/stderr
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    args = parse_args()

    # 文件日志
    log_file = None
    if args.log:
        log_file = setup_file_logging()
        logger.info("文件日志已启用: %s", log_file)

    console.print()
    console.print("[bold blue]测试用例生成 Agent[/]")
    console.print(f"[dim]模型: {MODEL}[/]")
    console.print(f"[dim]项目目录: {BASE_DIR}[/]")
    if log_file:
        console.print(f"[dim]日志文件: {log_file}[/]")
    console.print("[dim]架构: 主 Agent + LangGraph 工作流子 Agent[/]")
    console.print("[dim]输入指令开始（如：生成测试用例），quit 退出[/]")
    console.print()

    agent = create_test_case_agent(debug=args.debug)
    display = AgentDisplay(file_logging=bool(args.log))

    # 指令来源：--input 参数 或 交互式 stdin
    if args.input:
        user_input_queue = [args.input.strip(), "quit"]
    else:
        user_input_queue = None

    # 交互循环
    while True:
        if user_input_queue:
            try:
                user_input = user_input_queue.pop(0).strip()
            except IndexError:
                break
        else:
            try:
                user_input = input("你: ").strip()
            except (EOFError, KeyboardInterrupt):
                break

        if not user_input:
            continue
        if user_input.lower() == "quit":
            break

        display.reset()

        try:
            with Live(display.spinner, console=console, refresh_per_second=10, transient=True) as live:
                async for chunk in agent.astream(
                    {"messages": [("user", user_input)]},
                    config={"configurable": {"thread_id": "test-case-gen"}},
                    stream_mode="values",
                ):
                    if "messages" in chunk:
                        messages = chunk["messages"]
                        if len(messages) > display.printed_count:
                            live.stop()
                            for msg in messages[display.printed_count:]:
                                display.print_message(msg)
                            display.printed_count = len(messages)
                            live.start()
                            live.update(display.spinner)
        except KeyboardInterrupt:
            console.print("\n[yellow]已中断[/]")
            continue
        except Exception as e:
            console.print(f"\n[red]错误: {e}[/]")
            logger.exception("Agent run failed")
            continue

    console.print()
    console.print("[bold green]再见！[/]")


if __name__ == "__main__":
    asyncio.run(main())
