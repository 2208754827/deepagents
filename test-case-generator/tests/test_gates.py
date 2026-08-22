"""测试阶段确认门：gate interrupt/resume/abort + task 穿透 + 上下文隔离。

依赖：pytest, langgraph.checkpoint.memory.MemorySaver, langgraph.types.Command。
"""

from typing import Any, Optional

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.callbacks import CallbackManagerForLLMRun

from workflow.subagents.demo_gate import (
    build_demo_graph,
    reset_prepare_count,
    get_prepare_count,
)


# ============================================================
# Mock model: 支持 bind_tools 的 fake ChatModel
# ============================================================
class _MockToolCallModel(BaseChatModel):
    """返回预定响应的 mock 模型，支持 bind_tools（覆盖默认的 NotImplementedError）。"""

    responses: list[BaseMessage]
    _call_count: int = 0

    def __init__(self, responses: list[BaseMessage]):
        super().__init__(responses=responses)  # Pydantic BaseModel 需要字段传入
        self._call_count = 0

    def bind_tools(
        self,
        tools,
        *,
        tool_choice=None,
        **kwargs,
    ):
        """覆盖 BaseChatModel 的 NotImplementedError。返回 self 即可——mock 模型不关心实际 tool binding。"""
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = self._call_count % len(self.responses)
        self._call_count += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])

    @property
    def _llm_type(self) -> str:
        return "mock-tool-call-model"


# ============================================================
# 测试 1：confirm_gate interrupt 基础
# ============================================================
def test_confirm_gate_interrupts():
    """编译含 gate 的最小图 + MemorySaver（外层），invoke 后 assert interrupts[0].value["stage"] == "prepare_done"."""
    reset_prepare_count()
    graph = build_demo_graph()
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "test-1"}}

    # 首次 invoke 应命中 interrupt
    app.invoke({"messages": []}, config=config)
    state = app.get_state(config)

    assert state.interrupts is not None
    assert len(state.interrupts) > 0
    assert state.interrupts[0].value["stage"] == "prepare_done"
    assert state.interrupts[0].value["question"] == "准备阶段完成，是否继续生成？"
    assert "summary" in state.interrupts[0].value["payload"]
    assert "准备阶段完成" in state.interrupts[0].value["payload"]["summary"]


# ============================================================
# 测试 2：resume 确认 → 继续执行
# ============================================================
def test_resume_continues():
    """Command(resume={"confirm": True}) → 图到 END、produce 执行、prepare 不重跑（约束4）。"""
    reset_prepare_count()
    graph = build_demo_graph()
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "test-2"}}

    # 首次 invoke → interrupt
    app.invoke({"messages": []}, config=config)
    assert get_prepare_count() == 1, "prepare 应执行 1 次"

    # resume 确认
    result = app.invoke(Command(resume={"confirm": True, "feedback": None}), config=config)

    # 约束4: prepare 不重跑
    assert get_prepare_count() == 1, "resume 后 prepare 不应重跑"

    # produce 应执行，messages 中有结果
    assert "messages" in result
    assert len(result["messages"]) > 0
    final_text = result["messages"][-1].content if hasattr(result["messages"][-1], "content") else str(result["messages"][-1])
    assert "已生成最终产出" in final_text
    assert "审批通过" in final_text


# ============================================================
# 测试 3：resume 取消 → 中止
# ============================================================
def test_resume_aborts():
    """confirm=False → 图中止、produce 不执行。"""
    reset_prepare_count()
    graph = build_demo_graph()
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "test-3"}}

    # 首次 invoke → interrupt
    app.invoke({"messages": []}, config=config)

    # resume 取消
    result = app.invoke(Command(resume={"confirm": False, "feedback": None}), config=config)

    # 图中止，produce 不应执行，messages 应无内容
    assert "messages" in result
    assert len(result["messages"]) == 0 or (
        isinstance(result["messages"][-1], AIMessage)
        and "已生成" not in result["messages"][-1].content
    )


# ============================================================
# 测试 4：interrupt 经 task 穿透到主 Agent
# ============================================================
def test_interrupt_propagates_through_task():
    """构造主 Agent（create_deep_agent + checkpointer + CompiledSubAgent 含 gate）→ invoke → 断言 interrupts 非空。"""
    from deepagents import CompiledSubAgent, create_deep_agent
    from workflow.subagents.demo_gate import get_demo_runnable

    reset_prepare_count()

    # Mock model: 第1次返回 tool_call 触发 task 工具，第2次返回完成文本
    fake_model = _MockToolCallModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "name": "task",
                "args": {"subagent_type": "demo-gate", "description": "test"},
                "id": "1",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="完成"),
    ])

    subagent = CompiledSubAgent(
        name="demo-gate",
        description="test",
        runnable=get_demo_runnable(),
    )

    checkpointer = MemorySaver()
    agent = create_deep_agent(
        model=fake_model,
        system_prompt="测试助手。当用户请求时，调用 task 工具执行 demo-gate。",
        tools=[],
        subagents=[subagent],
        checkpointer=checkpointer,
        name="test-agent",
    )

    config = {"configurable": {"thread_id": "test-propagate"}}
    agent.invoke({"messages": [("user", "run demo")]}, config=config)

    state = agent.get_state(config)
    assert state.interrupts is not None, "主 Agent 应捕获到子图 interrupt"
    assert len(state.interrupts) > 0
    assert state.interrupts[0].value["stage"] == "prepare_done"
    assert "summary" in state.interrupts[0].value["payload"]


# ============================================================
# 测试 5：上下文隔离 — 子图私有键不上浮
# ============================================================
def test_context_isolation_on_interrupt():
    """暂停时顶层 get_state().values 不含子图私有键；只能从 interrupt.value 取 payload。"""
    from deepagents import CompiledSubAgent, create_deep_agent
    from workflow.subagents.demo_gate import get_demo_runnable

    reset_prepare_count()

    fake_model = _MockToolCallModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "name": "task",
                "args": {"subagent_type": "demo-gate", "description": "test"},
                "id": "1",
                "type": "tool_call",
            }],
        ),
        AIMessage(content="完成"),
    ])

    subagent = CompiledSubAgent(
        name="demo-gate",
        description="test",
        runnable=get_demo_runnable(),
    )

    checkpointer = MemorySaver()
    agent = create_deep_agent(
        model=fake_model,
        system_prompt="测试助手。当用户请求时，调用 task 工具执行 demo-gate。",
        tools=[],
        subagents=[subagent],
        checkpointer=checkpointer,
        name="test-agent",
    )

    config = {"configurable": {"thread_id": "test-isolation"}}
    agent.invoke({"messages": [("user", "run demo")]}, config=config)

    state = agent.get_state(config)

    # 约束3: 顶层 state.values 不应含子图私有键
    forbidden_keys = {"user_decision", "last_gate_stage", "prepared"}
    for key in forbidden_keys:
        assert key not in state.values, f"子图私有键 '{key}' 不应出现在主 Agent state.values 中"

    # 只能从 interrupt.value["payload"] 取预设数据
    assert state.interrupts is not None
    assert len(state.interrupts) > 0
    payload = state.interrupts[0].value.get("payload", {})
    assert "summary" in payload
    assert "准备阶段完成" in payload["summary"]