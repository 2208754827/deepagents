"""测试用例生成工作流 package。

统一 re-export 各子模块的公开符号，使 `from workflow import X` 可用。
对齐 examples/deep_research/research_agent/__init__.py 范式。
"""

from workflow.config import (
    BASE_DIR,
    MODEL,
    console,
    logger,
    setup_file_logging,
    tool_logger,
    load_ref,
    _safe_llm_invoke,
    PARSING_RULES_MD,
    MEMORY_SCHEMA_MD,
    TEST_DESIGN_MD,
    BUSINESS_RULES_MD,
    TRACEABILITY_MD,
    TEST_PRIORITY_MD,
    SHARED_GLOSSARY_MD,
    llm,
)
from workflow.middleware import (
    FixToolCallArgsMiddleware,
    WorkflowTriggerFallbackMiddleware,
)
from workflow.state import (
    WorkflowState,
    MAX_EXTRACT_CHARS,
    MAX_PARSE_CHARS,
    MAX_DESIGN_CHARS,
    MAX_REVIEW_CHARS,
    MAX_PROMPT_CHARS,
    ReviewChecklistItem,
    ReviewIssue,
    ReviewResult,
)
from workflow.parsing import (
    _parse_markdown_outline,
    _match_section_by_number,
    _match_section_by_keyword,
    _extract_text_by_headings,
    _extract_auto,
    _extract_section_range,
    _locate_section_by_ai,
)
from workflow.prompts import (
    build_parse_prompt,
    build_design_prompt,
    build_review_prompt,
    ORCHESTRATOR_PROMPT,
)
from workflow.nodes import (
    node_entry,
    node_init_memory,
    node_preprocess,
    node_extract,
    node_parse,
    node_design,
    node_review,
    node_generate,
    _run_script,
    _extract_json,
    build_final_message,
)
from workflow.graph import (
    build_workflow_graph,
    create_workflow_runnable,
    create_test_case_agent,
)

__all__ = [
    # config
    "BASE_DIR", "MODEL", "console", "logger", "tool_logger", "setup_file_logging",
    "load_ref", "_safe_llm_invoke",
    "PARSING_RULES_MD", "MEMORY_SCHEMA_MD", "TEST_DESIGN_MD", "BUSINESS_RULES_MD",
    "TRACEABILITY_MD", "TEST_PRIORITY_MD", "SHARED_GLOSSARY_MD", "llm",
    # middleware
    "FixToolCallArgsMiddleware", "WorkflowTriggerFallbackMiddleware",
    # state
    "WorkflowState", "MAX_EXTRACT_CHARS", "MAX_PARSE_CHARS", "MAX_DESIGN_CHARS",
    "MAX_REVIEW_CHARS", "MAX_PROMPT_CHARS",
    "ReviewChecklistItem", "ReviewIssue", "ReviewResult",
    # parsing
    "_parse_markdown_outline", "_match_section_by_number", "_match_section_by_keyword",
    "_extract_text_by_headings", "_extract_auto", "_extract_section_range", "_locate_section_by_ai",
    # prompts
    "build_parse_prompt", "build_design_prompt", "build_review_prompt", "ORCHESTRATOR_PROMPT",
    # nodes
    "node_entry", "node_init_memory", "node_preprocess", "node_extract", "node_parse",
    "node_design", "node_review", "node_generate", "_run_script", "_extract_json",
    "build_final_message",
    # graph
    "build_workflow_graph", "create_workflow_runnable", "create_test_case_agent",
]
