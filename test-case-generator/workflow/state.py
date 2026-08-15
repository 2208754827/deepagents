"""工作流状态定义：reducers、WorkflowState、字符上限常量、Review 数据模型。

无内部依赖，是 workflow 包的依赖叶子模块。
"""

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from pydantic import BaseModel, Field


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


MAX_EXTRACT_CHARS = 30000  # 自动截取时的字符上限
MAX_PARSE_CHARS = 20000    # node_parse 的需求文本上限
MAX_DESIGN_CHARS = 20000   # node_design 的 parsed_result 上限
MAX_REVIEW_CHARS = 20000  # node_review 的 test_cases 上限
MAX_PROMPT_CHARS = 60000   # 单次 LLM invoke 的总 prompt 字符上限


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
