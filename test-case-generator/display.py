"""Agent 运行时终端显示。

对应 examples/deep_research/utils.py 的职责：管理终端显示。
终端只显示 AI 消息（绿色 Panel）+ spinner 状态；工具调用/结果不打印，
仅更新 spinner 状态文本；文件日志开启时工具详情写入日志文件。

依赖：workflow.config（console/tool_logger）。
"""

import json

from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner

from workflow.config import console, tool_logger


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
    # 阶段确认门交互方法（约束2：只读 interrupt_value["payload"]）
    # ============================================================

    def ask(self, interrupt_value: dict):
        """展示阶段确认门：stage + question + payload，提示用户确认。

        约束2: 只读 interrupt_value["payload"]，绝不触碰子图 state（上下文隔离）。
        """
        stage = interrupt_value.get("stage", "unknown")
        question = interrupt_value.get("question", "是否继续？")
        payload = interrupt_value.get("payload", {})

        payload_display = json.dumps(payload, ensure_ascii=False, indent=2) if payload else "(无)"

        console.print()
        console.print(Panel(
            f"[bold]阶段: {stage}[/]\n\n"
            f"{question}\n\n"
            f"[dim]产出摘要:[/]\n"
            f"[dim]{payload_display}[/]",
            title="🛑 阶段确认门",
            border_style="yellow",
        ))
        console.print()

    def read_user_decision(self) -> dict:
        """读用户输入 → {"confirm": bool, "feedback": str|None}。

        y/yes/继续/是 → confirm=True, feedback=None
        n/no/取消/否 → confirm=False, feedback=None
        其它文本 → confirm=True, feedback=文本（用户附反馈后继续）
        """
        while True:
            try:
                text = input("继续? (y/n, 或输入反馈后继续): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return {"confirm": False, "feedback": None}

            if not text:
                continue

            if text in ("y", "yes", "继续", "是"):
                return {"confirm": True, "feedback": None}
            elif text in ("n", "no", "取消", "否"):
                return {"confirm": False, "feedback": None}
            else:
                # 任意其它文本 → 视为确认 + 附带反馈
                return {"confirm": True, "feedback": text}

    def abort(self):
        """展示用户取消/流程终止。"""
        console.print()
        console.print(Panel(
            "[bold yellow]用户已取消，流程终止。[/]",
            title="阶段确认门",
            border_style="red",
        ))
        console.print()
