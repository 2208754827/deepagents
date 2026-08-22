"""带确认门的示例子 Agent：3 节点 + 中间 gate。

演示套用通用模式：
1. 定义 DemoState（含 messages + user_decision + last_gate_stage + prepared）
2. 在需确认位置插 make_confirm_gate(...)
3. 条件路由：confirm=False → END，confirm=True → produce
4. 末节点显式透出审批结果给父 Agent（约束3）
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from workflow.gates import make_confirm_gate

# 模块级计数器，用于验证断点续跑不重跑前置节点（约束4）
_prepare_call_count = 0


class DemoState(TypedDict):
    """示例子 Agent 状态。"""
    messages: Annotated[list[AnyMessage], add_messages]  # CompiledSubAgent 结果载体
    prepared: str           # 准备阶段产出
    user_decision: dict     # 子图局部状态，不上浮父 Agent（约束3）
    last_gate_stage: str    # 子图局部状态，不上浮父 Agent（约束3）


def node_prepare(state: DemoState) -> dict:
    """准备阶段：生成演示数据。"""
    global _prepare_call_count
    _prepare_call_count += 1
    return {
        "prepared": f"准备阶段完成。已加载需求文档，提取 3 个核心功能模块。（调用次数: {_prepare_call_count}）",
    }


def node_produce(state: DemoState) -> dict:
    """产出阶段：生成最终结果，显式透出审批结果给父 Agent（约束3）。"""
    decision = state.get("user_decision", {})
    confirm = decision.get("confirm", False)
    feedback = decision.get("feedback")

    parts = ["已生成最终产出。"]
    if confirm:
        parts.append("审批通过。")
    if feedback:
        parts.append(f"用户反馈: {feedback}")

    result = "\n".join(parts)
    return {"messages": [AIMessage(content=result)]}


def reset_prepare_count():
    """重置调用计数器（测试用）。"""
    global _prepare_call_count
    _prepare_call_count = 0


def get_prepare_count() -> int:
    """获取调用计数器（测试用）。"""
    return _prepare_call_count


def build_demo_graph() -> StateGraph:
    """构建 demo 子图（含确认门），返回未编译的 StateGraph。"""
    g = StateGraph(DemoState)

    g.add_node("prepare", node_prepare)
    g.add_node("confirm", make_confirm_gate(
        stage="prepare_done",
        question="准备阶段完成，是否继续生成？",
        payload=lambda s: {"summary": s.get("prepared", "")},
    ))
    g.add_node("produce", node_produce)

    g.add_edge(START, "prepare")
    g.add_edge("prepare", "confirm")
    g.add_conditional_edges(
        "confirm",
        lambda s: "produce" if s.get("user_decision", {}).get("confirm") else END,
        {"produce": "produce", END: END},
    )
    g.add_edge("produce", END)

    return g


def get_demo_runnable():
    """编译 demo 子图（无 checkpointer — 约束1），返回 CompiledStateGraph。"""
    from workflow.config import logger
    runnable = build_demo_graph().compile()
    logger.info("Demo gate subgraph compiled (no checkpointer)")
    return runnable