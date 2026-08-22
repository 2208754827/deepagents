"""确认门节点工厂：make_confirm_gate。

任意子图任意位置插入阶段确认门，interrupt() 暂停 + 展示阶段产出 + 收用户回复。
依赖：langgraph.types.interrupt — 无需 deepagents 框架或 workflow 其他模块。
"""

from __future__ import annotations

from typing import Callable

from langgraph.types import interrupt


def make_confirm_gate(
    stage: str,
    question: str,
    payload: Callable[[dict], dict] | None = None,
):
    """生成可在子图任意位置插入的确认门节点。

    约束2：审批展示数据只在此 gate 内部主动提取放入 interrupt 载荷。
    display.ask 只读 interrupt.value["payload"]，绝不触碰子图 state。

    约束3：user_decision / last_gate_stage 是子图局部状态，不上浮父 Agent。
    父 Agent 需感知审批结果 → 子图末节点显式写进返回的 AIMessage 摘要。

    Args:
        stage: 阶段名（如 "parse_done"、"prepare_done"）
        question: 提示语（如 "准备阶段完成，是否继续生成？"）
        payload: 从当前 state 提取阶段产出供展示的函数（如 lambda s: {"summary": s.get("prepared")}）
    """

    def gate(state: dict) -> dict:
        # 在 gate 内部提取展示数据 → 约束2
        body = payload(state) if payload else {}

        # 暂停点；GraphInterrupt 冒泡到主 Agent
        answer = interrupt({
            "stage": stage,
            "question": question,
            "payload": body,
        })

        # answer 来自 agent.py 的 Command(resume=answer)
        # 约定 {"confirm": bool, "feedback": str | None}
        # 约束3: 子图局部状态，不上浮父 Agent
        return {"user_decision": answer, "last_gate_stage": stage}

    return gate