"""LangGraph 工作流图构建 + 主 Agent 装配。

依赖：workflow.config（MODEL/BASE_DIR/logger）+ workflow.middleware（两类中间件）
+ workflow.nodes（全部节点）+ workflow.prompts（ORCHESTRATOR_PROMPT）
+ workflow.state（WorkflowState）+ deepagents 框架符号。
"""

import json
import os

from deepagents import CompiledSubAgent, create_deep_agent
from deepagents.backends import FilesystemBackend
from langgraph.graph import END, START, StateGraph

from workflow.config import BASE_DIR, MODEL, logger
from workflow.middleware import FixToolCallArgsMiddleware, WorkflowTriggerFallbackMiddleware
from workflow.nodes import (
    node_design,
    node_entry,
    node_extract,
    node_generate,
    node_init_memory,
    node_parse,
    node_preprocess,
    node_review,
)
from workflow.prompts import ORCHESTRATOR_PROMPT
from workflow.state import WorkflowState


# ============================================================
# Step 10.1: 构建工作流图
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
# Step 10.2: 注册工作流子 Agent + 创建主 Agent
# ============================================================
def create_test_case_agent(debug: bool = False):
    """创建测试用例生成 Agent

    主 Agent 负责用户对话，工作流子 Agent 负责确定性执行生成流程。

    middleware 策略：
    - WorkflowTriggerFallbackMiddleware 始终注册（增强 3 后备机制）
    - FixToolCallArgsMiddleware 默认不注册（官方 DeepSeek 无 {} 前缀畸形 tool_call）；
      切回中转站时设 RELAY_FIX_TOOLCALL=true 启用
    """
    # 编译工作流图
    workflow_runnable = create_workflow_runnable()

    # 注册为 CompiledSubAgent
    workflow_subagent = CompiledSubAgent(
        name="generate-workflow",
        description="执行测试用例生成工作流：预处理需求文档 → 解析 → 设计用例 → 审查 → 生成 XMind/XLSX 文件。当用户要求生成测试用例时使用。",
        runnable=workflow_runnable,
    )

    # 组装 middleware 列表
    middleware_list = [WorkflowTriggerFallbackMiddleware()]
    if os.environ.get("RELAY_FIX_TOOLCALL") == "true":
        middleware_list.insert(0, FixToolCallArgsMiddleware())
        logger.info("FixToolCallArgsMiddleware enabled (RELAY_FIX_TOOLCALL=true)")
    else:
        logger.info("FixToolCallArgsMiddleware disabled (official API, no {} prefix issue)")

    agent = create_deep_agent(
        model=MODEL,
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=[],
        subagents=[workflow_subagent],
        memory=["./AGENTS.md"],
        middleware=middleware_list,
        backend=FilesystemBackend(root_dir=BASE_DIR, virtual_mode=False),
        name="test-case-generator",
        debug=debug,
    )
    logger.info("Deep agent created with model=%s + workflow subagent", MODEL)
    return agent
