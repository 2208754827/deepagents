"""测试用例生成 Agent — 主入口。

对应 examples/deep_research/agent.py 的职责：命令行参数解析 + 交互循环。
主 Agent 装配、工作流节点、提示词、状态、中间件等均拆到 workflow/ package；
运行时终端显示在 display.py。

文件末尾的 re-export 块仅为兼容 tests/test_extract.py 的
`from agent import X` 与 `patch("agent.X")`——使被 patch 的符号在 agent
命名空间可解析；nodes.py 内部通过 sys.modules.get("agent") 路由调用，
保证 patch 生效。
"""

import argparse
import asyncio
import io
import sys

from rich.live import Live

from display import AgentDisplay
from workflow.config import BASE_DIR, MODEL, console, logger, setup_file_logging
from workflow.graph import create_test_case_agent

# ============================================================
# 测试兼容 re-export（tests/test_extract.py 依赖，勿删）
# ============================================================
from workflow.state import (MAX_DESIGN_CHARS, MAX_EXTRACT_CHARS, MAX_PARSE_CHARS,
                            MAX_PROMPT_CHARS, MAX_REVIEW_CHARS, WorkflowState)
from workflow.parsing import (_extract_auto, _extract_section_range,
                              _extract_text_by_headings, _locate_section_by_ai,
                              _match_section_by_keyword, _match_section_by_number,
                              _parse_markdown_outline)
from workflow.config import _safe_llm_invoke
from workflow.nodes import node_extract, node_parse


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="测试用例生成 Agent")
    parser.add_argument("--log", action="store_true", help="启用文件日志（写入 logs/ 目录）")
    parser.add_argument("--debug", action="store_true", help="启用 Agent debug 模式")
    parser.add_argument("--input", type=str, default=None, help="命令行传入指令（一次性运行后退出）")
    return parser.parse_args()


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
