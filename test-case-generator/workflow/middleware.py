"""中间件：tool_call 修复（默认不注册）+ 工作流触发后备。

依赖：workflow.config（BASE_DIR/logger）+ langchain 中间件框架。
"""

from __future__ import annotations

import json
import re
from typing import Any, TYPE_CHECKING

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    hook_config,
)
from langchain_core.messages import AIMessage, HumanMessage

from workflow.config import BASE_DIR, logger

if TYPE_CHECKING:
    from langgraph.runtime import Runtime


# ============================================================
# Step 1.5: 中转站 Tool Call JSON 修复 Middleware（默认不注册）
# ============================================================
class FixToolCallArgsMiddleware(AgentMiddleware):
    """修复中转站返回的 tool_call arguments 格式问题。

    两个修复：
    1. JSON 格式：中转站在 arguments 前面多输出 ``{}``，导致 Extra data 错误
    2. 路径格式：GLM-5.1 等模型会生成 Windows 绝对路径（如 D:\\...），
       框架的 validate_path() 会拒绝。这里自动转换为虚拟路径（/...）

    本 middleware 在模型返回后、工具执行前拦截 AIMessage，统一修正。
    官方 DeepSeek API 不会产生 ``{}`` 前缀，故默认不注册；切回中转站时
    设 ``RELAY_FIX_TOOLCALL=true`` 启用（见 graph.py）。
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

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:  # type: ignore[override]
        response = handler(request)
        return self._fix_response(response)

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:  # type: ignore[override]
        response = await handler(request)
        return self._fix_response(response)

    def _fix_response(self, response) -> ModelResponse:
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
# 增强 3：工作流触发后备 Middleware（始终注册）
# ============================================================
FALLBACK_MESSAGE_SOURCE = "workflow_fallback"
_MAX_NUDGES = 2

_TRIGGER_VERBS = ("生成", "写", "做", "跑", "设计", "创建", "编写")
_TRIGGER_NOUNS = ("测试用例", "用例")


def _last_real_user_message(messages: list) -> tuple[int, HumanMessage] | None:
    """返回 (index, msg) 最近一条非中间件注入的 HumanMessage，无则 None。"""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, HumanMessage) and msg.additional_kwargs.get("lc_source") != FALLBACK_MESSAGE_SOURCE:
            return i, msg
    return None


def _is_generation_request(content: str) -> bool:
    """是否包含生成测试用例的动词+名词组合。"""
    return any(v in content for v in _TRIGGER_VERBS) and any(n in content for n in _TRIGGER_NOUNS)


def _agent_triggered_workflow(messages: list) -> bool:
    """主 Agent 是否曾在消息历史中调用 task(generate-workflow)。"""
    for msg in messages:
        if not isinstance(msg, AIMessage) or not msg.tool_calls:
            continue
        for tc in msg.tool_calls:
            name = tc.get("name") or ""
            args = tc.get("args") or {}
            if not isinstance(args, dict):
                continue
            sub_type = str(args.get("subagent_type", ""))
            desc = str(args.get("description", ""))
            if (
                "generate" in name.lower()
                or "generate-workflow" in sub_type
                or "generate_workflow" in sub_type
                or (name == "task" and ("generate" in sub_type.lower() or "测试用例" in desc or "生成" in desc))
            ):
                return True
    return False


def _nudges_since(messages: list, user_idx: int) -> int:
    """统计某条用户消息之后已注入的 nudge 数。"""
    return sum(
        1 for m in messages[user_idx + 1:]
        if isinstance(m, HumanMessage) and m.additional_kwargs.get("lc_source") == FALLBACK_MESSAGE_SOURCE
    )


class WorkflowTriggerFallbackMiddleware(AgentMiddleware):
    """后备机制：用户请求生成测试用例但主 Agent 未触发工作流时，注入提醒并回到模型。

    修复"主 Agent 不触发工作流"的致命 bug：增强版 ORCHESTRATOR_PROMPT
    已要求立即调用 task 工具，但模型偶尔仍会输出寒暄/复述而非工具调用。
    本中间件在主 Agent 自然停止（无后续 tool_call）时检查：
      - 若用户最近一次真实请求含"生成/写/做/设计...测试用例"
      - 且消息历史中主 Agent 从未调用 task(generate-workflow)
      - 且本轮已注入的提醒 < _MAX_NUDGES
    则注入一条 HumanMessage 提醒 + ``jump_to="model"``，强制再次触发模型。

    机制参照 RubricMiddleware.after_agent（非已废弃的 before_model）。
    """

    @hook_config(can_jump_to=["model"])
    def after_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        return self._compute_fallback(state)

    async def aafter_agent(self, state: AgentState, runtime: Runtime) -> dict[str, Any] | None:  # noqa: ARG002
        return self._compute_fallback(state)

    def _compute_fallback(self, state) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None

        last = messages[-1]
        # after_agent 在自然停止点触发：最后一条应为无 tool_call 的 AIMessage
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None

        # 已触发过工作流 → 不干预
        if _agent_triggered_workflow(messages):
            return None

        # 最近真实用户消息是否请求生成
        found = _last_real_user_message(messages)
        if found is None:
            return None
        user_idx, user_msg = found
        content = user_msg.content if isinstance(user_msg.content, str) else str(user_msg.content)
        if not _is_generation_request(content):
            return None

        # 本轮已注入的提醒次数
        nudges = _nudges_since(messages, user_idx)
        if nudges >= _MAX_NUDGES:
            logger.warning(
                "WorkflowTriggerFallback: 用户请求生成测试用例但主 Agent %d 次仍未触发工作流，放弃",
                nudges,
            )
            return None

        logger.info(
            "WorkflowTriggerFallback: 检测到生成请求但主 Agent 未触发工作流，注入第 %d 次提醒",
            nudges + 1,
        )
        return {
            "messages": [
                HumanMessage(
                    content=self._nudge_prompt(),
                    name=FALLBACK_MESSAGE_SOURCE,
                    additional_kwargs={"lc_source": FALLBACK_MESSAGE_SOURCE},
                )
            ],
            "jump_to": "model",
        }

    @staticmethod
    def _nudge_prompt() -> str:
        return (
            "【系统提醒】你刚才的回复没有调用工作流工具。当用户要求生成、写、做、设计测试用例时，"
            "你必须立即调用 task 工具，不要寒暄、不要复述需求、不要追问细节。\n"
            "调用方式：task(subagent_type=\"generate-workflow\", description=\"生成测试用例\")"
        )


logger.info("WorkflowTriggerFallbackMiddleware defined")
